"""AI-assisted authoring: a description becomes a plan, an approved plan
becomes a private experiment package that passes alhazen's static checks.

The provider writes only what needs judgement: the plan, the task and test
modules, the trial timeline and stimulus schematic, and the prose. Everything
with one right answer is assembled here from the plan (run.py, pyproject.toml,
configs/task.yaml, the documentation descriptor's frame and parameter list,
LICENSE, the manifest), so defaults appear once and the documentation cannot
disagree with the params file it describes.

Generated code is never imported or executed. Validation reads it: every
``.py`` is compiled to check its syntax (the code object is discarded), its
syntax tree is searched for what an experiment never needs (network,
subprocesses, eval, file writes, undeclared imports, alhazen names that do not
exist), its params model is compared with the params file, the files are
packaged with :func:`alhazen.hub.packages.build_bundle` and read back with
:func:`~alhazen.hub.packages.inspect_bundle`, and the documentation is
resolved by :func:`alhazen.hub.documentation.read_documentation`. A failure
gets one repair round with the validator's report; a second failure raises.

The only I/O is the provider client, plus a private temporary folder that
:func:`bundle_archive` packages in (``build_bundle`` works on files). Provider
errors propagate unchanged: the caller maps them to its own responses.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib
import inspect
import json
import keyword
import re
import sys
import tempfile
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Protocol

import yaml
from pydantic import BaseModel

from alhazen._scaffold import TEMPLATE_ROOT, task_class_name
from alhazen.hub.ai import prompts
from alhazen.hub.ai.schemas import (
    PLAN_SCHEMA,
    SOURCE_SCHEMA,
    for_provider,
    problems,
)
from alhazen.hub.documentation import DocumentationError, global_guide, read_documentation
from alhazen.hub.packages import (
    MANIFEST_NAME,
    PackageError,
    PackageInfo,
    build_bundle,
    inspect_bundle,
    safe_relative,
)

# ---------------------------------------------------------------------------
# The provider seam
# ---------------------------------------------------------------------------


class CompletionLike(Protocol):
    """What a provider answers: the text, its token usage, the model that
    actually answered (``alhazen.hub.ai.providers.Completion``)."""

    @property
    def text(self) -> str: ...

    @property
    def usage(self) -> dict[str, Any]: ...

    @property
    def model(self) -> str: ...


class ProviderClient(Protocol):
    """One provider and model, with the user's key (``providers.py``).

    ``json_schema`` is a bare JSON schema whose ``title`` names it; a client
    that supports structured output passes it on, any other relies on the
    schema printed in the prompt. Errors raise ``ProviderError``, which this
    module never catches."""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> CompletionLike: ...


# Called after every provider answer, before anything else is sent: the job
# runner records usage there, and raises to cancel before a repair round.
OnCompletion = Callable[[CompletionLike], None]

PLAN_MAX_TOKENS = 8_000
SOURCE_MAX_TOKENS = 24_000
TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# Validation reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One named check and what it found; ``ok`` when nothing."""

    name: str
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class ValidationReport:
    """Every check run on one answer, in order, and the files it produced
    (path, bytes). The browser shows it as is; the repair round sends its
    problems back to the model."""

    checks: tuple[Check, ...]
    files: tuple[tuple[str, int], ...] = ()

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def problems(self) -> list[str]:
        return [f"{check.name}: {line}" for check in self.checks for line in check.problems]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "problems": list(check.problems),
                    "notes": list(check.notes),
                }
                for check in self.checks
            ],
            "files": [{"path": path, "size": size} for path, size in self.files],
        }


class AuthoringInvalid(Exception):
    """The model's answer failed validation twice. ``report`` says why;
    ``output`` is the last answer as received (for diagnosis, never shown to
    other users)."""

    def __init__(self, message: str, report: ValidationReport, output: str) -> None:
        super().__init__(message)
        self.report = report
        self.output = output


class PlanInvalid(AuthoringInvalid):
    """The plan failed its schema or its rules after the repair round."""


class SourceInvalid(AuthoringInvalid):
    """The generated source failed static validation after the repair round.
    ``files`` holds what was assembled from the last answer."""

    def __init__(
        self, message: str, report: ValidationReport, output: str, files: dict[str, bytes]
    ) -> None:
        super().__init__(message, report, output)
        self.files = files


# ---------------------------------------------------------------------------
# The authoring context: what alhazen tells a provider about itself
# ---------------------------------------------------------------------------

_RELEASE = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
EXAMPLE_ROOT = Path(__file__).parent / "example"
EXAMPLE_NAME = "fixation_demo"  # the scaffold the example documentation documents

# Start-from sources: only text the model can use, within a budget, so a
# large repository cannot fill the request. Anything left out is recorded.
START_SUFFIXES = (".py", ".yaml", ".yml", ".toml", ".md", ".json", ".txt", ".cfg")
MAX_START_FILE_BYTES = 64 * 1024
MAX_START_BYTES = 256 * 1024

# Public alhazen names a task module uses, by the module that defines them.
# Signatures and summaries are read from the installed code at build time.
_API: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "alhazen",
        (
            "Task",
            "TrialPlan",
            "TrialSetup",
            "Condition",
            "CircleRegion",
            "Duration",
            "outcomes",
            "Model",
            "SubjectParams",
            "SchedulerConfig",
            "RewardPolicy",
            "RewardPulses",
            "InputFrame",
            "Screen",
        ),
    ),
    ("alhazen.core.events", ("EventSchema",)),
    ("alhazen.task.phases", ()),  # every name in its __all__
    ("alhazen.stimuli.fixation", ("make_fixation",)),
    ("alhazen.stimuli.base", ("Stimulus", "NullStimulus")),
    ("alhazen.modes.demo", ("DemoView",)),
    ("alhazen.modes.movie", ("MovieClip",)),
    ("alhazen.modes.simulation", ("Simulation",)),
    ("alhazen.devices.automated", ("AutomatedGazeTracker",)),
    ("alhazen.testing", ()),
)
_TASK_METHODS = (
    "default_params",
    "instructions",
    "conditions",
    "build_trial",
    "demo_views",
    "movie_clips",
    "simulation",
)


def release_of(version: str) -> str:
    """``MAJOR.MINOR.PATCH`` of an alhazen version (``2.13.0.dev1`` → ``2.13.0``)."""
    match = _RELEASE.match(version)
    if match is None:
        raise ValueError(f"not an alhazen version: {version[:40]!r}")
    return match.group(0)


@dataclass(frozen=True)
class StartFrom:
    """A version the user may read, to start from: its files as text."""

    experiment_title: str
    version: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class AuthoringContext:
    """What a provider is told besides the user's words, as titled texts.

    ``plan_items`` go with the plan request, ``source_items`` with the source
    request; both are public alhazen material except the ``start/`` items,
    which are the start-from version's files."""

    alhazen_version: str
    plan_items: tuple[tuple[str, str], ...]
    source_items: tuple[tuple[str, str], ...]
    start: StartFrom | None = None
    start_files: tuple[tuple[str, int], ...] = ()
    start_omitted: tuple[tuple[str, str], ...] = ()

    def disclosure(self, step: str) -> dict[str, Any]:
        """What the ``plan`` or ``source`` request sends besides the user's
        prompt or plan: the context item names and sizes and, separately, the
        start-from files (path, bytes) and those left out (path, reason)."""
        items = {"plan": self.plan_items, "source": self.source_items}[step]
        context = [
            {"name": name, "bytes": len(text.encode("utf-8"))}
            for name, text in items
            if not name.startswith("start/")
        ]
        start = None
        if self.start is not None:
            start = {
                "experiment": self.start.experiment_title,
                "version": self.start.version,
                "files": [{"path": path, "bytes": size} for path, size in self.start_files],
                "bytes": sum(size for _, size in self.start_files),
                "omitted": [{"path": path, "reason": why} for path, why in self.start_omitted],
            }
        return {
            "context": context,
            "context_bytes": sum(item["bytes"] for item in context),
            "start_from": start,
        }


def _summary(obj: Any) -> str:
    text = inspect.getdoc(obj) or ""
    first = text.split("\n\n")[0].replace("\n", " ").strip()
    return first[:400]


def _signature(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return ""


def api_summary() -> str:
    """The public API a task uses, as signatures and one-line summaries read
    from the installed alhazen. A module that cannot be imported here (a
    renderer dependency missing) is named rather than described."""
    lines: list[str] = []
    for module_name, wanted in _API:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            lines.append(f"## {module_name}: not importable here ({error.name or 'dependency'})")
            continue
        names = wanted or tuple(getattr(module, "__all__", ()))
        lines.append(f"## {module_name}")
        for name in names:
            obj = getattr(module, name, None)
            if obj is None:
                continue
            lines.append(f"- {name}{_signature(obj)}")
            summary = _summary(obj)
            if summary:
                lines.append(f"  {summary}")
            if name == "Task":
                for method in _TASK_METHODS:
                    member = getattr(obj, method, None)
                    if member is not None:
                        lines.append(f"  - Task.{method}{_signature(member)}: {_summary(member)}")
    return "\n".join(lines)


def scaffold_files(name: str = EXAMPLE_NAME) -> dict[str, str]:
    """The files ``alhazen new <name>`` writes, rendered in memory, by path."""
    substitutions = {
        "name": name,
        "package": name.replace("-", "_"),
        "task_class": task_class_name(name),
        "task_name": name.replace("_", "-"),
    }
    files: dict[str, str] = {}
    for source in sorted(TEMPLATE_ROOT.rglob("*")):
        if source.is_dir():
            continue
        relative = Template(source.relative_to(TEMPLATE_ROOT).as_posix()).substitute(substitutions)
        files[relative.removesuffix(".template")] = Template(
            source.read_text(encoding="utf-8")
        ).substitute(substitutions)
    return files


def _example_documentation() -> list[tuple[str, str]]:
    return [
        (f"example: docs/{path.relative_to(EXAMPLE_ROOT).as_posix()}", path.read_text("utf-8"))
        for path in sorted(EXAMPLE_ROOT.rglob("*"))
        if path.is_file()
    ]


def _start_items(start: StartFrom) -> tuple[list[tuple[str, str]], list[tuple[str, int]], list[tuple[str, str]]]:
    def priority(path: str) -> tuple[int, str]:
        order = ("run.py", "configs/", "src/", "docs/", "README", "pyproject.toml")
        for rank, prefix in enumerate(order):
            if path.startswith(prefix):
                return rank, path
        return len(order), path

    items: list[tuple[str, str]] = []
    sent: list[tuple[str, int]] = []
    omitted: list[tuple[str, str]] = []
    total = 0
    for path in sorted(start.files, key=priority):
        text = start.files[path]
        try:
            safe_relative(path)
        except PackageError:
            omitted.append((path[:200], "not a package path"))
            continue
        size = len(text.encode("utf-8"))
        if not path.endswith(START_SUFFIXES):
            omitted.append((path, "not source text"))
        elif size > MAX_START_FILE_BYTES:
            omitted.append((path, "larger than 64 KiB"))
        elif total + size > MAX_START_BYTES:
            omitted.append((path, "over the 256 KiB budget"))
        else:
            total += size
            items.append((f"start/{path}", text))
            sent.append((path, size))
    return items, sent, omitted


def build_context(alhazen_version: str, start: StartFrom | None = None) -> AuthoringContext:
    """The authoring context for ``alhazen_version`` (the running alhazen).

    Public material only, read from the code: the mode guide, the API
    summary, the scaffold and the documented scaffold example; plus, when the
    caller (who checked the user may read it) passes ``start``, that
    version's source text within the start-from budget."""
    release = release_of(alhazen_version)
    guide = json.dumps(global_guide(), ensure_ascii=False, separators=(",", ":"))
    scaffold = scaffold_files()
    package = EXAMPLE_NAME
    core = [
        (f"alhazen {release}: modes and protections (generated guide)", guide),
        (f"alhazen {release}: public API used by tasks", api_summary()),
    ]
    plan_scaffold = [
        (f"scaffold: src/{package}/task.py", scaffold[f"src/{package}/task.py"]),
        ("scaffold: configs/task.yaml", scaffold["configs/task.yaml"]),
    ]
    source_scaffold = [
        ("scaffold: run.py", scaffold["run.py"]),
        *plan_scaffold,
        ("scaffold: tests/test_task.py", scaffold["tests/test_task.py"]),
    ]
    start_items: list[tuple[str, str]] = []
    sent: list[tuple[str, int]] = []
    omitted: list[tuple[str, str]] = []
    if start is not None:
        start_items, sent, omitted = _start_items(start)
    return AuthoringContext(
        alhazen_version=release,
        plan_items=tuple(core + plan_scaffold + start_items),
        source_items=tuple(core + source_scaffold + _example_documentation() + start_items),
        start=start,
        start_files=tuple(sent),
        start_omitted=tuple(omitted),
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

_PLAN_FIELDS = tuple(PLAN_SCHEMA["properties"])
# Keys the assembler owns in the params file.
_RESERVED_PARAMETERS = frozenset({"subject_kind"})


@dataclass(frozen=True)
class Plan:
    """A validated plan: the fields of ``schemas.PLAN_SCHEMA``. Built only
    by :meth:`from_dict`, so every instance passed its schema and rules."""

    title: str
    slug: str
    summary: str
    paradigm: str
    subject_kind: str
    license: str
    hardware: dict[str, bool]
    design: list[dict[str, Any]]
    stimuli: list[dict[str, Any]]
    measures: list[dict[str, Any]]
    parameters: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    notes: str

    @classmethod
    def from_dict(cls, value: Any) -> Plan:
        """Parse ``value`` (the browser's or a stored plan); PlanInvalid if
        it breaks the schema or the plan rules."""
        report = _plan_report(value)
        if not report.ok:
            raise PlanInvalid("the plan is not valid", report, "")
        data = json.loads(json.dumps(value))  # a deep, JSON-only copy
        return cls(**{name: data[name] for name in _PLAN_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps({name: getattr(self, name) for name in _PLAN_FIELDS}))

    def edited(self, *, title: str | None = None, notes: str | None = None) -> Plan:
        """This plan with the person's edits applied, validated again."""
        data = self.to_dict()
        if title is not None:
            data["title"] = title
        if notes is not None:
            data["notes"] = notes
        return Plan.from_dict(data)


def _is_duration(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and set(value) <= {"ms", "frames"}


def _plan_rules(plan: dict[str, Any]) -> list[str]:
    """The rules a schema cannot say: references, consistency, ownership."""
    found: list[str] = []
    parameters = plan["parameters"]
    by_name: dict[str, dict[str, Any]] = {}
    for index, parameter in enumerate(parameters):
        name = parameter["name"]
        where = f"parameters[{index}] ({name})"
        if name in by_name:
            found.append(f"{where}: named twice")
        by_name[name] = parameter
        top = name.split(".")[0]
        if top in _RESERVED_PARAMETERS:
            found.append(f"{where}: {top} is written from subject_kind; leave it out")
        if keyword.iskeyword(top) or top.startswith(("model_", "_")):
            found.append(f"{where}: {top!r} cannot be a field of a params model")
        kind, default = parameter["type"], parameter["default"]
        if kind == "duration" and not _is_duration(default):
            found.append(f'{where}: a duration default is {{"ms": n}} or {{"frames": n}}')
        if kind != "duration" and _is_duration(default):
            found.append(f"{where}: an {{ms}} default needs type duration")
        choices = parameter["constraints"]["choices"]
        if kind == "choice" and not choices:
            found.append(f"{where}: a choice parameter lists its choices")
        if choices and default not in choices:
            found.append(f"{where}: the default is not one of the choices")
        low, high = parameter["constraints"]["min"], parameter["constraints"]["max"]
        if low is not None and high is not None and low > high:
            found.append(f"{where}: min is greater than max")
        magnitude = default.get("ms") if isinstance(default, dict) else default
        if isinstance(magnitude, (int, float)) and not isinstance(magnitude, bool):
            if (low is not None and magnitude < low) or (high is not None and magnitude > high):
                found.append(f"{where}: the default is outside min..max")
    names = set(by_name)
    for name in names:
        parts = name.split(".")
        for cut in range(1, len(parts)):
            if ".".join(parts[:cut]) in names:
                found.append(f"parameters ({name}): {'.'.join(parts[:cut])} is also a value")
    for required in ("paradigm.kind", "paradigm.n_per_condition"):
        if required not in names:
            found.append(f"parameters: {required} is required (the scheduler)")
    for index, phase in enumerate(plan["timeline"]):
        where = f"timeline[{index}] ({phase['phase']})"
        reference = phase["parameter"]
        if reference is not None:
            target = by_name.get(reference)
            if target is None:
                found.append(f"{where}: parameter {reference!r} is not in parameters")
            elif target["type"] != "duration":
                found.append(f"{where}: parameter {reference!r} is not a duration")
        if phase["duration"] == "parameterized" and reference is None:
            found.append(f"{where}: a parameterized phase names its duration parameter")
        if phase["duration"] == "event-driven" and not (phase["until"] or "").strip():
            found.append(f"{where}: an event-driven phase says what it waits until")
        if phase["duration"] == "instant" and reference is not None:
            found.append(f"{where}: an instant phase has no duration parameter")
    rewards = [name for name in names if name.split(".")[0] == "reward"]
    if plan["subject_kind"] == "human":
        if plan["hardware"]["reward"]:
            found.append("hardware.reward: a human session never pays reward")
        if rewards:
            found.append("parameters: reward.* is for monkey sessions only")
    else:
        if not plan["hardware"]["reward"]:
            found.append("hardware.reward: a monkey session pays reward")
        if not any(name.startswith("reward.by_outcome.") for name in rewards):
            found.append("parameters: a monkey plan says what pays (reward.by_outcome.*)")
    if plan["slug"].replace("-", "_") in sys.stdlib_module_names | {"alhazen", "numpy"}:
        found.append(f"slug: {plan['slug']!r} would shadow a module the experiment imports")
    return found


def _plan_report(value: Any) -> ValidationReport:
    shape = problems(value, PLAN_SCHEMA, root="plan")
    checks = [Check("schema", tuple(shape))]
    if not shape:
        checks.append(Check("rules", tuple(_plan_rules(value))))
    return ValidationReport(tuple(checks))


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"the key {key[:60]!r} appears twice")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def parse_json_answer(text: str) -> Any:
    """The JSON object a model answered with. One surrounding Markdown code
    fence is tolerated (some providers add one); anything else around the
    object, duplicate keys and NaN/Infinity are refused (ValueError)."""
    match = _FENCE.match(text)
    body = match.group(1) if match else text
    try:
        value = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except json.JSONDecodeError as error:
        raise ValueError(f"not valid JSON: {error.msg} at line {error.lineno}") from None
    if not isinstance(value, dict):
        raise ValueError("the answer must be one JSON object")
    return value


def _answer_report(text: str, schema: dict[str, Any], root: str) -> tuple[Any, ValidationReport]:
    try:
        value = parse_json_answer(text)
    except ValueError as error:
        return None, ValidationReport((Check("answer", (str(error),)),))
    shape = problems(value, schema, root=root)
    return value, ValidationReport((Check("answer"), Check("schema", tuple(shape))))


def _complete(
    client: ProviderClient,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_tokens: int,
    on_completion: OnCompletion | None,
) -> CompletionLike:
    completion = client.complete(
        messages,
        json_schema=for_provider(schema),
        max_tokens=max_tokens,
        temperature=TEMPERATURE,
    )
    if on_completion is not None:
        on_completion(completion)
    return completion


def _plan_attempt(text: str) -> tuple[Plan | None, ValidationReport]:
    value, report = _answer_report(text, PLAN_SCHEMA, "plan")
    if not report.ok:
        return None, report
    rules = _plan_rules(value)
    report = ValidationReport((*report.checks, Check("rules", tuple(rules))))
    if not report.ok:
        return None, report
    return Plan.from_dict(value), report


def plan(
    client: ProviderClient,
    prompt: str,
    ctx: AuthoringContext,
    *,
    on_completion: OnCompletion | None = None,
) -> Plan:
    """Ask for a plan of the experiment ``prompt`` describes.

    One request, and one repair request if the answer breaks the plan schema
    or rules; PlanInvalid if the repaired answer still does."""
    if not prompt.strip():
        raise ValueError("describe the experiment first")
    messages = prompts.plan_messages(prompt, ctx.plan_items, PLAN_SCHEMA)
    completion = _complete(client, messages, PLAN_SCHEMA, PLAN_MAX_TOKENS, on_completion)
    result, report = _plan_attempt(completion.text)
    if result is not None:
        return result
    messages = prompts.repair_messages(messages, completion.text, report.problems)
    completion = _complete(client, messages, PLAN_SCHEMA, PLAN_MAX_TOKENS, on_completion)
    result, report = _plan_attempt(completion.text)
    if result is None:
        raise PlanInvalid("the plan failed validation after one repair", report, completion.text)
    return result


# ---------------------------------------------------------------------------
# Assembly: the files with one right answer
# ---------------------------------------------------------------------------

PACKAGE_VERSION = "0.1.0"
PARAMS_PATH = "configs/task.yaml"
DESCRIPTOR_PATH = "docs/experiment.json"
METHODS_PATH = "docs/methods.md"
PROVENANCE_PATH = "docs/ai-provenance.json"
_DOC_TASK_KEYS = ("title", "summary", "outcomes", "events", "timeline", "diagram")


@dataclass(frozen=True)
class Names:
    """The identifiers a plan fixes for its package."""

    package: str
    task_name: str
    task_class: str
    params_class: str

    @property
    def task_module(self) -> str:
        return f"src/{self.package}/task.py"

    @property
    def task_doc(self) -> str:
        return f"docs/tasks/{self.task_name}.md"

    def for_prompt(self) -> dict[str, str]:
        return {
            "package (import name)": self.package,
            "task name (Task.name, --task)": self.task_name,
            "task class": self.task_class,
            "params model class": self.params_class,
            "task module path": self.task_module,
            "test module path": "tests/test_task.py",
            "params file": PARAMS_PATH,
            "task documentation id": self.task_name,
        }


def names_of(plan: Plan) -> Names:
    package = plan.slug.replace("-", "_")
    task_class = task_class_name(package)
    return Names(
        package=package,
        task_name=plan.tasks[0]["name"],
        task_class=task_class,
        params_class=f"{task_class}Params",
    )


def params_tree(plan: Plan) -> dict[str, Any]:
    """The params file's content: subject_kind, then every plan parameter's
    default at its dotted path, in plan order."""
    tree: dict[str, Any] = {"subject_kind": plan.subject_kind}
    for parameter in plan.parameters:
        *parents, leaf = parameter["name"].split(".")
        node = tree
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = parameter["default"]
    return tree


def params_yaml(plan: Plan, names: Names) -> str:
    body = yaml.safe_dump(
        params_tree(plan), sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return (
        f"# {plan.title}: the task's parameters, written from the reviewed plan.\n"
        f"# Every key is validated against {names.params_class} (src/{names.package}/task.py),\n"
        "# so a typo fails at load naming this file. Durations are {ms: n} or {frames: n}.\n"
        f"{body}"
    )


_RUN_PY = Template(
    '''"""Run $title, in any of alhazen's modes, on any rig.

    python run.py --task $task_name --mode demo
    python run.py --task $task_name --mode simulate --seed 1 --headless
    python run.py --task $task_name --mode test     --sub dev --ses 1 --initials DEV
    python run.py --task $task_name --mode movie    --out movies
    python run.py --task $task_name --rig <rig>     --sub s01 --ses 1 --initials AB

With no ``--rig`` a session starts on the rig named ``laptop`` (this
experiment's configs/rig-laptop.yaml if it has one, else alhazen's shared
laptop), a development rig on which run mode refuses to record. A real
session names its lab's rig: ``alhazen rigs --project .`` lists the rigs a
name can reach. Rig files are each lab's own and are never packaged.

Generated by alhazen's AI authoring from a reviewed plan; read the code
before running it. Everything generic (the argument parser, run numbering,
the rehearsal paths) is alhazen's ``run_experiment``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alhazen.cli.modes import run_experiment

from $package.task import $task_class

# This file's folder, so the params path below is absolute wherever the
# command is typed.
HERE = Path(__file__).parent

# The experiment workspace's Task parameters menu (alhazen dashboard): display
# name -> (task, params file). A module-level dict literal, because the
# workspace reads it out of this file without running it.
PARAMETERS = {
    $parameters_key: ("$task_name", HERE / "configs" / "task.yaml"),
}

# No training ladders: add LADDERS beside PARAMETERS (and pass ladders=LADDERS)
# when a monkey version is trained up to this task in stages.

if __name__ == "__main__":
    raise SystemExit(
        run_experiment(
            task_class=$task_class,
            default_rig="laptop",
            argv=sys.argv[1:],
        )
    )
'''
)

_MIT = """MIT License

Copyright (c) the authors of {title}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

_BSD3 = """BSD 3-Clause License

Copyright (c) the authors of {title}

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

_APACHE = """Copyright (c) the authors of {title}

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this software except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

_CC_BY = """Copyright (c) the authors of {title}

This work is licensed under the Creative Commons Attribution 4.0
International License (CC BY 4.0). To view a copy of this license, visit
https://creativecommons.org/licenses/by/4.0/
"""

_PROPRIETARY = """Copyright (c) the authors of {title}. All rights reserved.

No licence is granted to copy, modify or distribute this software.
"""

_LICENSE_TEXTS = {
    "MIT": _MIT,
    "BSD-3-Clause": _BSD3,
    "Apache-2.0": _APACHE,
    "CC-BY-4.0": _CC_BY,
    "Proprietary": _PROPRIETARY,
}


def license_text(license_id: str, title: str) -> str:
    """The LICENSE file for one of ``schemas.LICENSES``."""
    return _LICENSE_TEXTS[license_id].format(title=title)


def _pyproject(plan: Plan, names: Names) -> str:
    rendered = scaffold_files(names.package)["pyproject.toml"]
    # The scaffold names its task after the package; this task has the
    # plan's name, and its version is the package's.
    scaffold_task = names.package.replace("_", "-")
    replacements = (
        ('description = "An alhazen experiment"', f"description = {json.dumps(plan.title)}"),
        (f"\n{scaffold_task} = ", f"\n{names.task_name} = "),
        (f"--task {scaffold_task} ", f"--task {names.task_name} "),
        (f'version = "0.1.0"', f'version = "{PACKAGE_VERSION}"'),
    )
    for old, new in replacements:
        if old not in rendered:
            raise RuntimeError(f"the scaffold's pyproject.toml no longer contains {old!r}")
        rendered = rendered.replace(old, new)
    return rendered


def _descriptor_parameter(parameter: dict[str, Any], model: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": parameter["name"],
        "label": parameter["label"],
        "group": parameter["group"],
        "type": parameter["type"],
    }
    if parameter["unit"]:
        entry["unit"] = parameter["unit"]
    entry["default"] = parameter["default"]
    constraints = {key: value for key, value in parameter["constraints"].items() if value}
    if parameter["constraints"]["min"] == 0:
        constraints["min"] = 0
    if parameter["constraints"]["max"] == 0:
        constraints["max"] = 0
    if constraints:
        entry["constraints"] = constraints
    entry["meaning"] = parameter["meaning"]
    entry["source"] = {"model": model}
    return entry


def descriptor(
    plan: Plan, names: Names, task_part: Mapping[str, Any], references: Sequence[str]
) -> dict[str, Any]:
    """docs/experiment.json: the frame and parameters from the plan, the
    task's outcomes, events, timeline and diagram from the model."""
    model = f"{names.package}.task:{names.params_class}"
    parameters = [
        {
            "name": "subject_kind",
            "label": "Subject",
            "group": "Design",
            "type": "choice",
            "default": plan.subject_kind,
            "constraints": {"choices": ["human", "monkey"]},
            "meaning": "Who this params file is for. A human session never pays reward; "
            "a monkey session pays what its reward block says.",
            "source": {"model": "alhazen.task.subject_kind:SubjectParams"},
        },
        *(_descriptor_parameter(parameter, model) for parameter in plan.parameters),
    ]
    task: dict[str, Any] = {
        "id": names.task_name,
        "title": task_part.get("title", plan.title),
        "summary": task_part.get("summary", ""),
        "description": names.task_doc,
        "parameters_file": PARAMS_PATH,
        "parameters": parameters,
    }
    for key in ("outcomes", "events", "timeline", "diagram"):
        if task_part.get(key) is not None:
            task[key] = task_part[key]
    return {
        "schema": "alhazen-documentation",
        "schema_version": 1,
        "title": plan.title,
        "summary": plan.summary,
        "methods": METHODS_PATH,
        "references": list(references),
        "tasks": [task],
    }


def _task_part(text: str) -> tuple[dict[str, Any], list[str]]:
    """The model's part of the task documentation, and what is wrong with it."""
    try:
        value = parse_json_answer(text)
    except ValueError as error:
        return {}, [f"task_documentation_json: {error}"]
    found = [
        f"task_documentation_json: unexpected key {key[:40]!r} (allowed: {', '.join(_DOC_TASK_KEYS)})"
        for key in value
        if key not in _DOC_TASK_KEYS
    ]
    found += [
        f"task_documentation_json: missing key {key!r}"
        for key in _DOC_TASK_KEYS
        if key not in value
    ]
    return {key: value[key] for key in _DOC_TASK_KEYS if key in value}, found


def manifest_metadata(plan: Plan, alhazen_release: str, references: Sequence[str]) -> dict[str, Any]:
    """The manifest fields of a generated package (``build_bundle`` adds the
    file list and fills python_min and platforms)."""
    return {
        "name": plan.slug,
        "version": PACKAGE_VERSION,
        "title": plan.title,
        "description": plan.summary,
        "hardware": dict(plan.hardware),
        "license": plan.license,
        "documentation": DESCRIPTOR_PATH,
        "alhazen_min": alhazen_release,
        "citations": list(references),
    }


def _provenance(
    plan: Plan, ctx: AuthoringContext, model: str
) -> dict[str, Any]:
    disclosed = ctx.disclosure("source")
    start = disclosed["start_from"]
    return {
        "schema": "alhazen-ai-provenance",
        "schema_version": 1,
        "ai_assisted": True,
        "model": model,
        "alhazen_version": ctx.alhazen_version,
        "plan_sha256": hashlib.sha256(
            json.dumps(plan.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "started_from": (
            None
            if start is None
            else {
                "experiment": start["experiment"],
                "version": start["version"],
                "files_sent": len(start["files"]),
                "bytes_sent": start["bytes"],
            }
        ),
        "note": "Drafted by a language model from a reviewed plan and checked statically "
        "(syntax, forbidden patterns, package rules, documentation). It has not been run: "
        "read it, then simulate it on your rig before collecting data.",
    }


def assemble(
    plan: Plan, answer: Mapping[str, Any], ctx: AuthoringContext, model: str
) -> tuple[dict[str, bytes], list[str]]:
    """Every file of the package (manifest excluded), from the plan and a
    schema-valid source answer; plus the problems found while assembling."""
    names = names_of(plan)
    task_part, found = _task_part(answer["task_documentation_json"])
    references = list(answer["references"])
    files_text: dict[str, str] = {
        "run.py": _RUN_PY.substitute(
            title=plan.title.replace('"""', "'''").replace("\\", "/"),
            task_name=names.task_name,
            package=names.package,
            task_class=names.task_class,
            parameters_key=repr(plan.title),
        ),
        "pyproject.toml": _pyproject(plan, names),
        ".gitignore": scaffold_files(names.package)[".gitignore"],
        f"src/{names.package}/__init__.py": scaffold_files(names.package)[
            f"src/{names.package}/__init__.py"
        ],
        names.task_module: answer["task_module"],
        "tests/test_task.py": answer["test_module"],
        PARAMS_PATH: params_yaml(plan, names),
        DESCRIPTOR_PATH: json.dumps(
            descriptor(plan, names, task_part, references), indent=2, ensure_ascii=False
        )
        + "\n",
        METHODS_PATH: answer["methods_markdown"],
        names.task_doc: answer["task_markdown"],
        "README.md": answer["readme_markdown"],
        "LICENSE": license_text(plan.license, plan.title),
        PROVENANCE_PATH: json.dumps(_provenance(plan, ctx, model), indent=2, ensure_ascii=False)
        + "\n",
    }
    files = {
        path: (text if text.endswith("\n") else text + "\n").encode("utf-8")
        for path, text in files_text.items()
    }
    return files, found


# ---------------------------------------------------------------------------
# Static validation: read the code, never run it
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# Standard-library modules experiment code may import. Anything else of the
# standard library is refused by name; the network, process and dynamic-code
# modules get their own reason.
_STDLIB_ALLOWED = frozenset(
    {
        "__future__", "abc", "bisect", "collections", "contextlib", "copy", "dataclasses",
        "datetime", "enum", "fractions", "functools", "heapq", "itertools", "json", "logging",
        "math", "numbers", "operator", "pathlib", "random", "re", "statistics", "string",
        "textwrap", "time", "types", "typing", "warnings",
    }
)
_FORBIDDEN_MODULES = {
    **dict.fromkeys(
        ("socket", "ssl", "urllib", "http", "ftplib", "smtplib", "telnetlib", "xmlrpc",
         "requests", "httpx", "aiohttp", "websocket", "websockets", "asyncio"),
        "network access",
    ),
    **dict.fromkeys(("subprocess", "multiprocessing", "pty", "signal"), "starting processes"),
    **dict.fromkeys(("os", "shutil", "tempfile", "glob"), "operating-system and file access"),
    **dict.fromkeys(
        ("importlib", "pickle", "marshal", "ctypes", "code", "codeop", "runpy", "builtins"),
        "dynamic code",
    ),
}
_FORBIDDEN_CALLS = {
    "eval": "eval",
    "exec": "exec",
    "compile": "compiling code at run time",
    "__import__": "dynamic imports",
    "breakpoint": "a debugger breakpoint",
    "input": "reading the terminal",
    "globals": "rewriting module state",
    "getattr": None,  # only with a non-literal name; see below
}
_WRITE_METHODS = frozenset(
    {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch", "symlink_to",
     "hardlink_to", "chmod"}
)
_THIRD_PARTY = frozenset({"numpy", "alhazen"})
_TEST_THIRD_PARTY = frozenset({"pytest"})


def _module_root(name: str) -> str:
    return name.split(".")[0]


def _is_test(path: str) -> bool:
    return path.startswith("tests/")


def _safety(path: str, tree: ast.AST) -> list[str]:
    """What an experiment never needs: network, processes, dynamic code,
    environment secrets, and (outside tests) writing files."""
    found: list[str] = []
    test = _is_test(path)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                if func.id == "getattr":
                    if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                        found.append(f"{path}:{line}: getattr with a computed name")
                else:
                    found.append(f"{path}:{line}: {_FORBIDDEN_CALLS[func.id]} ({func.id}())")
            if isinstance(func, ast.Name) and func.id == "open" and not test:
                mode = node.args[1] if len(node.args) > 1 else next(
                    (kw.value for kw in node.keywords if kw.arg == "mode"), None
                )
                if mode is not None and not (
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and not set(mode.value) & set("wax+")
                ):
                    found.append(f"{path}:{line}: opens a file for writing (alhazen writes all data)")
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _WRITE_METHODS
                and not test
            ):
                found.append(f"{path}:{line}: {func.attr}() writes to disk (alhazen writes all data)")
        elif isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__"}:
            found.append(f"{path}:{line}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "system", "popen"}:
            found.append(f"{path}:{line}: .{node.attr} (environment or shell access)")
    return found


def _imported_modules(tree: ast.AST) -> list[tuple[int, str, list[str], int]]:
    """(line, module, names, level) for every import in ``tree``."""
    found: list[tuple[int, str, list[str], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, [], 0))
        elif isinstance(node, ast.ImportFrom):
            found.append(
                (node.lineno, node.module or "", [alias.name for alias in node.names], node.level)
            )
    return found


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names


def _alhazen_module(name: str) -> tuple[Any, str | None]:
    """The trusted alhazen module ``name``, or why it could not be read."""
    try:
        return importlib.import_module(name), None
    except ImportError as error:
        missing = error.name or ""
        if missing == name or missing.startswith("alhazen"):
            return None, "missing"
        return None, f"needs {missing}"


def _imports(
    path: str, tree: ast.Module, package: str, own: Mapping[str, ast.Module]
) -> tuple[list[str], list[str]]:
    """Imports outside the allowed set, and alhazen names that do not exist
    in the running alhazen. ``own`` maps this package's module names to their
    syntax trees, so a test importing a class the task module lacks fails."""
    found: list[str] = []
    notes: list[str] = []
    allowed = _THIRD_PARTY | ({package} | (_TEST_THIRD_PARTY if _is_test(path) else set()))
    module_aliases: dict[str, Any] = {}
    for line, module, names, level in _imported_modules(tree):
        where = f"{path}:{line}"
        if level:
            found.append(f"{where}: relative import; import {package}.<module> instead")
            continue
        root = _module_root(module)
        if root in _FORBIDDEN_MODULES:
            found.append(f"{where}: imports {module} ({_FORBIDDEN_MODULES[root]})")
            continue
        if root in sys.stdlib_module_names:
            if root not in _STDLIB_ALLOWED:
                found.append(f"{where}: imports {module}, which experiment code does not need")
            continue
        if root not in allowed:
            found.append(f"{where}: imports {module}, which the package does not depend on")
            continue
        if root == package:
            target = own.get(module)
            if target is None:
                found.append(f"{where}: {module} is not a module of this package")
            else:
                missing = sorted(set(names) - _top_level_names(target) - {"*"})
                for name in missing:
                    found.append(f"{where}: {module} defines no {name}")
            continue
        if root != "alhazen":
            continue
        if module == "alhazen.cli" or module.startswith("alhazen.cli."):
            found.append(f"{where}: task code does not import alhazen.cli")
            continue
        loaded, why = _alhazen_module(module)
        if loaded is None:
            if why == "missing":
                found.append(f"{where}: alhazen has no module {module}")
            else:
                notes.append(f"{where}: {module} not checked here ({why})")
            continue
        for name in names:
            if name == "*":
                found.append(f"{where}: import * hides which names are used")
            elif hasattr(loaded, name):
                value = getattr(loaded, name)
                if inspect.ismodule(value):
                    module_aliases[name] = value
            else:
                sub, _ = _alhazen_module(f"{module}.{name}")
                if sub is None:
                    found.append(f"{where}: {module} has no {name}")
                else:
                    module_aliases[name] = sub
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
            and not hasattr(module_aliases[node.value.id], node.attr)
        ):
            module_name = module_aliases[node.value.id].__name__
            found.append(f"{path}:{node.lineno}: {module_name} has no {node.attr}")
    return found, notes


def _class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name),
        None,
    )


def _base_names(node: ast.ClassDef) -> set[str]:
    return {
        base.id if isinstance(base, ast.Name) else base.attr
        for base in node.bases
        if isinstance(base, (ast.Name, ast.Attribute))
    }


def _class_assign(node: ast.ClassDef, name: str) -> ast.expr | None:
    for item in node.body:
        if isinstance(item, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in item.targets
        ):
            return item.value
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            if item.target.id == name and item.value is not None:
                return item.value
    return None


def _declared(task: ast.ClassDef) -> tuple[list[str] | None, list[str] | None]:
    """The event names and outcome names the Task class declares as
    literals, or None where they are not literal."""
    events: list[str] | None = None
    value = _class_assign(task, "events")
    if isinstance(value, ast.Call) and value.args:
        first = value.args[0]
        if isinstance(first, (ast.Tuple, ast.List)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in first.elts
        ):
            events = [e.value for e in first.elts if isinstance(e, ast.Constant)]
    outcome_names: list[str] | None = None
    value = _class_assign(task, "outcomes")
    if isinstance(value, ast.Call) and not value.args:
        outcome_names = [kw.arg for kw in value.keywords if kw.arg is not None]
    return events, outcome_names


_INHERITED_FIELDS = frozenset({"subject_kind", "reward"})
_STAND_INS = frozenset({"NullStimulus"})


def _structure(
    names: Names, tree: ast.Module
) -> tuple[list[str], ast.ClassDef | None, ast.ClassDef | None]:
    found: list[str] = []
    path = names.task_module
    params = _class(tree, names.params_class)
    task = _class(tree, names.task_class)
    if params is None:
        found.append(f"{path}: no class {names.params_class} (the params model)")
    elif "SubjectParams" not in _base_names(params):
        found.append(f"{path}: {names.params_class} must subclass SubjectParams")
    else:
        for item in params.body:
            target = item.target if isinstance(item, ast.AnnAssign) else None
            if isinstance(target, ast.Name) and target.id in _INHERITED_FIELDS:
                found.append(
                    f"{path}:{item.lineno}: {names.params_class} redeclares {target.id}, "
                    "which SubjectParams already declares with its checks"
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("alhazen.testing"):
            found.append(f"{path}:{node.lineno}: task code imports alhazen.testing (test doubles)")
        elif isinstance(node, ast.Name) and node.id in _STAND_INS:
            found.append(
                f"{path}:{node.lineno}: {node.id} is a stand-in that draws nothing; "
                "draw a real stimulus (make_fixation draws a disc anywhere)"
            )
    if task is None:
        found.append(f"{path}: no class {names.task_class} (the task)")
        return found, params, task
    if "Task" not in _base_names(task):
        found.append(f"{path}: {names.task_class} must subclass Task")
    value = _class_assign(task, "name")
    if not (isinstance(value, ast.Constant) and value.value == names.task_name):
        found.append(f'{path}: {names.task_class}.name must be "{names.task_name}"')
    value = _class_assign(task, "params_model")
    if not (isinstance(value, ast.Name) and value.id == names.params_class):
        found.append(f"{path}: {names.task_class}.params_model must be {names.params_class}")
    methods = {item.name for item in task.body if isinstance(item, ast.FunctionDef)}
    for required in ("default_params", "instructions", "conditions", "build_trial"):
        if required not in methods:
            found.append(f"{path}: {names.task_class} defines no {required}()")
    events, outcome_names = _declared(task)
    if events is None:
        found.append(f"{path}: declare events as EventSchema((\"NAME\", ...)) with literal names")
    if outcome_names is None:
        found.append(f"{path}: declare outcomes as outcomes(NAME=dict(...), ...)")
    return found, params, task


def _literal(node: ast.expr) -> tuple[bool, Any]:
    """A default written as a literal: numbers, text, booleans, lists, and
    Duration(ms=)/Duration(frames=) as the params file writes them."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        ok, value = _literal(node.operand)
        return (ok, -value) if ok and isinstance(value, (int, float)) else (False, None)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool)):
        return True, node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_literal(item) for item in node.elts]
        return (True, [v for _, v in items]) if all(ok for ok, _ in items) else (False, None)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Duration"
        and not node.args
        and len(node.keywords) == 1
        and node.keywords[0].arg in {"ms", "frames"}
    ):
        ok, value = _literal(node.keywords[0].value)
        return (ok, {node.keywords[0].arg: value}) if ok else (False, None)
    return False, None


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _alhazen_model(name: str) -> type[BaseModel] | None:
    """An alhazen params model by its public name (SchedulerConfig...)."""
    import alhazen

    value = getattr(alhazen, name, None)
    return value if isinstance(value, type) and issubclass(value, BaseModel) else None


def _models_in(annotation: Any) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
    """(model, dict value model) inside a field annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, None
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is dict and len(args) == 2:
        inner, _ = _models_in(args[1])
        return None, inner
    for arg in args:
        model, mapping = _models_in(arg)
        if model is not None or mapping is not None:
            return model, mapping
    return None, None


def _unknown_keys(tree: Any, model: type[BaseModel], where: str) -> list[str]:
    """Keys the params file sets that ``model`` (trusted alhazen) refuses."""
    if not isinstance(tree, dict):
        return []
    found: list[str] = []
    for key, value in tree.items():
        if key not in model.model_fields:
            found.append(f"{PARAMS_PATH}: {where}{key} is not a field of {model.__name__}")
            continue
        inner, mapping = _models_in(model.model_fields[key].annotation)
        if inner is not None:
            found += _unknown_keys(value, inner, f"{where}{key}.")
        elif mapping is not None and isinstance(value, dict):
            for name, item in value.items():
                found += _unknown_keys(item, mapping, f"{where}{key}.{name}.")
    return found


def _defaults(names: Names, params: ast.ClassDef, tree: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The params model against the params file: every key a field, every
    literal default equal to the file's value, every required field set."""
    found: list[str] = []
    notes: list[str] = []
    from alhazen import SubjectParams

    fields: dict[str, ast.AnnAssign] = {
        item.target.id: item
        for item in params.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }
    inherited = set(SubjectParams.model_fields)
    where = f"{names.task_module}: {names.params_class}"
    for key, value in tree.items():
        if key in inherited and key not in fields:
            found += _unknown_keys({key: value}, SubjectParams, "")
            continue
        field_node = fields.get(key)
        if field_node is None:
            found.append(f"{where} has no field {key}, which {PARAMS_PATH} sets")
            continue
        annotation = field_node.annotation
        model_name = annotation.id if isinstance(annotation, ast.Name) else None
        model = _alhazen_model(model_name) if model_name else None
        if model is not None:
            found += _unknown_keys(value, model, f"{key}.")
        if field_node.value is None:
            continue
        ok, default = _literal(field_node.value)
        if ok:
            if not _same(default, value):
                found.append(
                    f"{where}.{key} defaults to {json.dumps(default)} but {PARAMS_PATH} "
                    f"sets {json.dumps(value)}"
                )
        elif isinstance(field_node.value, ast.Call) and isinstance(value, dict):
            for kw in field_node.value.keywords:
                if kw.arg in value:
                    ok, inner = _literal(kw.value)
                    if ok and not _same(inner, value[kw.arg]):
                        found.append(
                            f"{where}.{key}.{kw.arg} defaults to {json.dumps(inner)} but "
                            f"{PARAMS_PATH} sets {json.dumps(value[kw.arg])}"
                        )
        else:
            notes.append(f"{where}.{key}: default not a literal; not compared")
    for key, field_node in fields.items():
        if field_node.value is None and key not in tree:
            found.append(f"{where}.{key} has no default and {PARAMS_PATH} does not set it")
    return found, notes


def _timeline_problems(documentation: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for task in documentation["tasks"]:
        timeline = task.get("timeline") or {}
        for phase in timeline.get("phases", []):
            timing = phase["timing"]
            for kind in (timing, timing.get("inner") or {}):
                if kind.get("kind") == "fixed" and (kind.get("ms") or 0) > 0:
                    found.append(
                        f"{DESCRIPTOR_PATH}: phase {phase['id']!r} has a fixed duration; "
                        "use a duration parameter (kind parameter) or kind event"
                    )
    return found


def _declared_vs_documented(
    task: ast.ClassDef | None, documentation: dict[str, Any] | None
) -> list[str]:
    if task is None or documentation is None:
        return []
    events, outcome_names = _declared(task)
    documented = documentation["tasks"][0]
    found: list[str] = []
    doc_events = sorted(event["name"] for event in documented["events"])
    doc_outcomes = sorted(outcome["name"] for outcome in documented["outcomes"])
    if events is not None and sorted(events) != doc_events:
        found.append(
            f"events: the task declares {sorted(events)} but the documentation lists {doc_events}"
        )
    if outcome_names is not None and sorted(outcome_names) != doc_outcomes:
        found.append(
            f"outcomes: the task declares {sorted(outcome_names)} but the documentation "
            f"lists {doc_outcomes}"
        )
    return found


def bundle_archive(
    files: Mapping[str, bytes], metadata: Mapping[str, Any]
) -> tuple[bytes, PackageInfo]:
    """Package ``files`` with ``metadata`` exactly as a person's release is
    packaged, and return the ZIP bytes and what ``inspect_bundle`` read from
    them. PackageError if a package rule refuses them.

    The server calls this again at acceptance when the person changes the
    title, summary or licence."""
    with tempfile.TemporaryDirectory(prefix="alhazen-ai-") as folder:
        root = Path(folder) / "source"
        for path, data in files.items():
            target = root / safe_relative(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        output = Path(folder) / "package.zip"
        build_bundle(root, output, dict(metadata), sorted(files))
        archive = output.read_bytes()
        again = Path(folder) / "inspected.zip"
        again.write_bytes(archive)
        return archive, inspect_bundle(again)


def _files_check(files: Mapping[str, bytes]) -> list[str]:
    found: list[str] = []
    total = 0
    for path, data in files.items():
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            found.append(f"{path}: {len(data)} bytes; a generated file is at most {MAX_FILE_BYTES}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            found.append(f"{path}: not UTF-8 text")
            continue
        if "\x00" in text:
            found.append(f"{path}: contains a NUL character")
        if path.endswith((".md", ".py")) and not text.strip():
            found.append(f"{path}: empty")
    if total > MAX_TOTAL_BYTES:
        found.append(f"{total} bytes in all; a generated package is at most {MAX_TOTAL_BYTES}")
    return found


@dataclass(frozen=True)
class GeneratedBundle:
    """A validated package: its files (manifest excluded), the manifest
    inspect_bundle read, the report, and the ZIP itself."""

    files: dict[str, bytes]
    manifest: dict[str, Any]
    report: ValidationReport
    archive: bytes = field(repr=False)
    sha256: str = ""


def validate_package(
    plan: Plan,
    files: Mapping[str, bytes],
    metadata: Mapping[str, Any],
    *,
    extra: Sequence[str] = (),
) -> tuple[ValidationReport, bytes | None, PackageInfo | None]:
    """Every static check, in order, on an assembled package; the archive and
    its inspection when packaging succeeded. ``extra`` are problems found
    while assembling, reported under the documentation check."""
    names = names_of(plan)
    checks: list[Check] = [Check("files", tuple(_files_check(files)))]
    trees: dict[str, ast.Module] = {}
    syntax: list[str] = []
    for path, data in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        text = data.decode("utf-8", errors="replace")
        try:
            compile(text, path, "exec", dont_inherit=True)
            trees[path] = ast.parse(text, filename=path)
        except SyntaxError as error:
            syntax.append(f"{path}:{error.lineno}: {error.msg}")
        except ValueError as error:  # NUL bytes in the source
            syntax.append(f"{path}: {error}")
    checks.append(Check("syntax", tuple(syntax)))
    generated = {path: tree for path, tree in trees.items() if path != "run.py"}
    safety = [line for path, tree in generated.items() for line in _safety(path, tree)]
    checks.append(Check("safety", tuple(safety)))
    own = {
        path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(
            ".__init__"
        ): tree
        for path, tree in trees.items()
        if path.startswith("src/")
    }
    import_problems: list[str] = []
    import_notes: list[str] = []
    for path, tree in generated.items():
        found, notes = _imports(path, tree, names.package, own)
        import_problems += found
        import_notes += notes
    checks.append(Check("imports", tuple(import_problems), tuple(import_notes)))
    task_tree = trees.get(names.task_module)
    params_node = task_node = None
    if task_tree is None:
        structure = [f"{names.task_module}: missing or not valid Python"]
    else:
        structure, params_node, task_node = _structure(names, task_tree)
    checks.append(Check("structure", tuple(structure)))
    tree = params_tree(plan)
    if params_node is not None:
        default_problems, default_notes = _defaults(names, params_node, tree)
        checks.append(Check("defaults", tuple(default_problems), tuple(default_notes)))
    else:
        checks.append(Check("defaults", ("the params model was not found",)))
    archive: bytes | None = None
    info: PackageInfo | None = None
    try:
        archive, info = bundle_archive(files, metadata)
        checks.append(Check("package"))
    except PackageError as error:
        checks.append(Check("package", (str(error),)))
    documentation_problems = list(extra)
    resolved: dict[str, Any] | None = None
    if archive is not None and info is not None:
        with tempfile.TemporaryDirectory(prefix="alhazen-ai-doc-") as folder:
            path = Path(folder) / "package.zip"
            path.write_bytes(archive)
            try:
                resolved = read_documentation(path, info.manifest)
            except DocumentationError as error:
                documentation_problems.append(str(error))
        if resolved is not None:
            documentation_problems += _timeline_problems(resolved)
            documentation_problems += _declared_vs_documented(task_node, resolved)
    else:
        documentation_problems.append("not checked: the package could not be built")
    checks.append(Check("documentation", tuple(documentation_problems)))
    report = ValidationReport(
        tuple(checks), tuple((path, len(data)) for path, data in sorted(files.items()))
    )
    return report, archive, info


def _source_attempt(
    plan: Plan, text: str, ctx: AuthoringContext, model: str
) -> tuple[ValidationReport, dict[str, bytes], bytes | None, PackageInfo | None]:
    answer, report = _answer_report(text, SOURCE_SCHEMA, "source")
    if not report.ok:
        return report, {}, None, None
    files, assembly = assemble(plan, answer, ctx, model)
    metadata = manifest_metadata(plan, ctx.alhazen_version, answer["references"])
    validation, archive, info = validate_package(plan, files, metadata, extra=assembly)
    return (
        ValidationReport((*report.checks, *validation.checks), validation.files),
        files,
        archive,
        info,
    )


def generate_source(
    client: ProviderClient,
    plan: Plan,
    ctx: AuthoringContext,
    *,
    on_completion: OnCompletion | None = None,
) -> GeneratedBundle:
    """Ask for the source of ``plan``, assemble the package and validate it.

    One request and, if any check fails, one repair request carrying the
    report; SourceInvalid (with the report and the files) if the repaired
    answer still fails."""
    names = names_of(plan)
    messages = prompts.source_messages(
        plan.to_dict(),
        names.for_prompt(),
        params_yaml(plan, names),
        ctx.source_items,
        SOURCE_SCHEMA,
    )
    completion = _complete(client, messages, SOURCE_SCHEMA, SOURCE_MAX_TOKENS, on_completion)
    report, files, archive, info = _source_attempt(plan, completion.text, ctx, completion.model)
    if not report.ok:
        messages = prompts.repair_messages(messages, completion.text, report.problems)
        completion = _complete(client, messages, SOURCE_SCHEMA, SOURCE_MAX_TOKENS, on_completion)
        report, files, archive, info = _source_attempt(
            plan, completion.text, ctx, completion.model
        )
    if not report.ok or archive is None or info is None:
        raise SourceInvalid(
            "the generated source failed validation after one repair",
            report,
            completion.text,
            files,
        )
    return GeneratedBundle(
        files=files,
        manifest=info.manifest,
        report=report,
        archive=archive,
        sha256=info.sha256,
    )
