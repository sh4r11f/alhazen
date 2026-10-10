"""alhazen.hub.ai.author: plans and sources from a provider, checked statically.

Held here, with no network (FakeProvider replays the recorded example in
fixtures/ai):

1. The schemas are what a strict structured-output mode accepts, and the
   local checker enforces them, lengths included.
2. The context is alhazen's own and public; start-from files are sent only
   within budget, and the disclosure record matches what was sent.
3. A plan is accepted only if it passes its schema and rules, after at most
   one repair; durations are parameters or events, never invented numbers.
4. A source answer becomes a package that inspect_bundle and the
   documentation loader accept; any failed check gets one repair round; each
   forbidden pattern is reported, never executed.
5. The recorded live run reproduces offline: its plan, its failure mode, and
   the hand-corrected example that validates.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.hub.ai_support import (
    FIXTURES,
    FakeProvider,
    ProviderError,
    live_exchanges,
    plan_answer,
    source_answer,
)

from alhazen.hub.ai import author, prompts
from alhazen.hub.ai.schemas import (
    LICENSES,
    PLAN_SCHEMA,
    SOURCE_SCHEMA,
    for_provider,
    problems,
)
from alhazen.hub.documentation import read_documentation
from alhazen.hub.packages import inspect_bundle
from alhazen.version import __version__ as ALHAZEN_VERSION

PROMPT = (FIXTURES / "prompt.txt").read_text("utf-8").strip()
DOC_FIXTURE = Path(__file__).parent / "fixtures" / "documentation" / "scaffold" / "docs"


@pytest.fixture(scope="module")
def ctx() -> author.AuthoringContext:
    return author.build_context(ALHAZEN_VERSION)


@pytest.fixture(scope="module")
def the_plan() -> author.Plan:
    return author.Plan.from_dict(json.loads(plan_answer()))


@pytest.fixture(scope="module")
def bundle(the_plan, ctx) -> author.GeneratedBundle:
    return author.generate_source(FakeProvider(), the_plan, ctx)


def source_text(**changes: Any) -> str:
    answer = source_answer()
    answer.update(changes)
    return json.dumps(answer)


def task_module() -> str:
    return source_answer()["task_module"]


def failing(the_plan, ctx, text: str) -> author.ValidationReport:
    """The report on ``text``, answered twice (so the repair fails too)."""
    provider = FakeProvider(source_text=text)
    with pytest.raises(author.SourceInvalid) as raised:
        author.generate_source(provider, the_plan, ctx)
    assert len(provider.requests) == 2
    return raised.value.report


# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------


def _objects(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


@pytest.mark.parametrize("schema", [PLAN_SCHEMA, SOURCE_SCHEMA], ids=["plan", "source"])
def test_schemas_are_strict_mode_shaped(schema):
    for node in _objects(schema):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    stripped = json.dumps(for_provider(schema))
    assert "maxLength" not in stripped and "minLength" not in stripped
    assert "maxLength" in json.dumps(schema)  # the local schema keeps them
    assert for_provider(schema)["title"] == schema["title"]


def test_checker_enforces_type_enum_pattern_length_and_closure():
    plan = json.loads(plan_answer())
    assert problems(plan, PLAN_SCHEMA) == []
    broken = json.loads(plan_answer())
    broken["subject_kind"] = "robot"
    broken["slug"] = "Not A Slug"
    broken["title"] = "x" * 161
    broken["hardware"]["reward"] = "no"
    broken["extra"] = 1
    del broken["notes"]
    broken["parameters"][0]["default"] = {"seconds": 1}
    found = "\n".join(problems(broken, PLAN_SCHEMA, root="plan"))
    for expected in (
        "plan.subject_kind: must be one of",
        "plan.slug:",
        "plan.title: longer than 160",
        "plan.hardware.reward: must be boolean",
        "unexpected field 'extra'",
        "plan.notes: missing",
        "plan.parameters[0].default: does not match any allowed form",
    ):
        assert expected in found


def test_checker_refuses_booleans_as_numbers_and_unknown_keywords():
    assert problems(True, {"type": "integer"}) == ["$: must be integer"]
    assert problems(float("nan"), {"type": "number"}) == ["$: must be finite"]
    with pytest.raises(ValueError, match="unsupported keywords"):
        problems(1, {"type": "number", "multipleOf": 2})


def test_licences_all_have_a_licence_file():
    for licence in LICENSES:
        assert "the authors of Demo" in author.license_text(licence, "Demo")


# ---------------------------------------------------------------------------
# 2. Context and disclosure
# ---------------------------------------------------------------------------


def test_context_is_alhazens_own(ctx):
    names = dict(ctx.source_items)
    release = author.release_of(ALHAZEN_VERSION)
    guide = json.loads(names[f"alhazen {release}: modes and protections (generated guide)"])
    assert guide["alhazen_version"] == ALHAZEN_VERSION
    api = names[f"alhazen {release}: public API used by tasks"]
    for name in ("HoldFixation(", "AcquireFixation(", "make_fixation(", "SubjectParams("):
        assert name in api
    assert "class FixationDemoTask(Task)" in names["scaffold: src/fixation_demo/task.py"]
    assert not any(name.startswith("start/") for name, _ in ctx.source_items)
    assert ctx.disclosure("source")["start_from"] is None


def test_example_documentation_is_the_tested_scaffold_example():
    example = author.EXAMPLE_ROOT
    for path in DOC_FIXTURE.rglob("*"):
        if path.is_file():
            copy = example / path.relative_to(DOC_FIXTURE)
            assert copy.read_bytes() == path.read_bytes(), copy
    assert sorted(p.name for p in example.rglob("*") if p.is_file()) == sorted(
        p.name for p in DOC_FIXTURE.rglob("*") if p.is_file()
    )


def test_release_of():
    assert author.release_of("2.13.0") == "2.13.0"
    assert author.release_of("2.14.0.dev3") == "2.14.0"
    with pytest.raises(ValueError):
        author.release_of("next")


def test_start_from_is_sent_within_budget_and_recorded_exactly():
    big = "x" * (author.MAX_START_FILE_BYTES + 1)
    files = {
        "run.py": "print('start')\n",
        "configs/task.yaml": "a: 1\n",
        "data/sub-01/trials.csv": "secret,participant,rows\n",
        "assets/picture.png": "binary-ish",
        "../escape.py": "x = 1\n",
        "src/pkg/huge.py": big,
    }
    start = author.StartFrom("Source experiment", "1.2.0", files)
    ctx = author.build_context(ALHAZEN_VERSION, start)
    record = ctx.disclosure("plan")["start_from"]
    sent = {item["path"] for item in record["files"]}
    assert sent == {"run.py", "configs/task.yaml"}
    assert record["bytes"] == len("print('start')\n") + len("a: 1\n")
    omitted = {item["path"]: item["reason"] for item in record["omitted"]}
    assert omitted["data/sub-01/trials.csv"] == "not source text"
    assert omitted["assets/picture.png"] == "not source text"
    assert omitted["src/pkg/huge.py"] == "larger than 64 KiB"
    assert omitted["../escape.py"] == "not a package path"
    provider = FakeProvider()
    author.plan(provider, PROMPT, ctx)
    text = provider.sent_text()
    assert "print('start')" in text and "a: 1" in text
    assert "participant,rows" not in text and big not in text
    plan_bytes = sum(item["bytes"] for item in ctx.disclosure("plan")["context"])
    assert ctx.disclosure("plan")["context_bytes"] == plan_bytes


def test_start_from_budget_stops_at_256_kib():
    files = {f"src/pkg/m{i:02d}.py": "y" * 60_000 for i in range(6)}
    ctx = author.build_context(ALHAZEN_VERSION, author.StartFrom("S", "1.0.0", files))
    record = ctx.disclosure("source")["start_from"]
    assert record["bytes"] <= author.MAX_START_BYTES
    assert len(record["files"]) == 4
    assert {item["reason"] for item in record["omitted"]} == {"over the 256 KiB budget"}


def test_requests_carry_only_prompt_context_and_schema(ctx):
    provider = FakeProvider()
    author.plan(provider, PROMPT, ctx)
    request = provider.requests[0]
    assert request["json_schema"] == for_provider(PLAN_SCHEMA)
    assert request["messages"][0] == {"role": "system", "content": prompts.PLAN_SYSTEM}
    user = request["messages"][1]["content"]
    assert PROMPT in user
    for name, text in ctx.plan_items:
        assert text in user, name


# ---------------------------------------------------------------------------
# 3. Plans
# ---------------------------------------------------------------------------


def test_plan_from_a_valid_answer(ctx):
    calls: list[Any] = []
    provider = FakeProvider()
    result = author.plan(provider, PROMPT, ctx, on_completion=calls.append)
    assert isinstance(result, author.Plan)
    assert len(provider.requests) == 1 and len(calls) == 1
    assert author.Plan.from_dict(result.to_dict()) == result
    assert result.to_dict() == json.loads((FIXTURES / "plan.json").read_text("utf-8"))


def test_plan_repairs_once_with_the_problems(ctx):
    provider = FakeProvider(["rules_invalid", "valid"])
    author.plan(provider, PROMPT, ctx)
    assert len(provider.requests) == 2
    repair = provider.requests[1]["messages"]
    assert repair[-2]["role"] == "assistant"
    assert "no_such_parameter" in repair[-1]["content"]
    assert "is not in parameters" in repair[-1]["content"]


@pytest.mark.parametrize("behaviour", ["invalid_json", "schema_invalid", "rules_invalid"])
def test_plan_invalid_after_one_repair(ctx, behaviour):
    provider = FakeProvider([behaviour])
    with pytest.raises(author.PlanInvalid) as raised:
        author.plan(provider, PROMPT, ctx)
    assert len(provider.requests) == 2
    assert not raised.value.report.ok
    assert raised.value.output


@pytest.mark.parametrize("kind", ["quota", "auth", "timeout", "invalid", "other"])
def test_provider_errors_propagate_unchanged(ctx, the_plan, kind):
    with pytest.raises(ProviderError) as raised:
        author.plan(FakeProvider([kind]), PROMPT, ctx)
    assert raised.value.kind == kind
    with pytest.raises(ProviderError):
        author.generate_source(FakeProvider([kind]), the_plan, ctx)


def test_a_failure_after_the_first_answer_stops_before_the_repair(ctx):
    class Cancelled(Exception):
        pass

    def cancel(_completion):
        raise Cancelled

    provider = FakeProvider(["invalid_json", "valid"])
    with pytest.raises(Cancelled):
        author.plan(provider, PROMPT, ctx, on_completion=cancel)
    assert len(provider.requests) == 1


def test_empty_prompt_is_refused_before_any_request(ctx):
    provider = FakeProvider()
    with pytest.raises(ValueError):
        author.plan(provider, "   ", ctx)
    assert provider.requests == []


def edit_plan(change) -> list[str]:
    plan = json.loads(plan_answer())
    change(plan)
    with pytest.raises(author.PlanInvalid) as raised:
        author.Plan.from_dict(plan)
    return raised.value.report.problems


def _param(plan: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in plan["parameters"] if p["name"] == name)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda p: p["timeline"][1].update(parameter="nope"), "is not in parameters"),
        (lambda p: p["timeline"][1].update(parameter=None), "names its duration parameter"),
        (lambda p: p["timeline"][0].update(until=" "), "says what it waits until"),
        (
            lambda p: p["timeline"][1].update(parameter="fix_size_dva"),
            "is not a duration",
        ),
        (lambda p: p["timeline"][-1].update(parameter="hold_duration"), "instant phase"),
        (lambda p: p["hardware"].update(reward=True), "never pays reward"),
        (
            lambda p: p.update(subject_kind="monkey", hardware={**p["hardware"], "reward": True}),
            "reward.by_outcome",
        ),
        (lambda p: p["parameters"].append(dict(p["parameters"][0])), "named twice"),
        (
            lambda p: p["parameters"].append({**p["parameters"][0], "name": "paradigm"}),
            "also a value",
        ),
        (lambda p: _param(p, "paradigm.kind").update(default="latin"), "not one of the choices"),
        (lambda p: _param(p, "hold_duration").update(default=1000), "duration default"),
        (lambda p: _param(p, "fix_size_dva").update(default={"ms": 3}), "needs type duration"),
        (
            lambda p: _param(p, "hold_duration")["constraints"].update(max=10),
            "outside min..max",
        ),
        (
            lambda p: p["parameters"].remove(_param(p, "paradigm.n_per_condition")),
            "paradigm.n_per_condition is required",
        ),
        (
            lambda p: p["parameters"].append({**p["parameters"][0], "name": "subject_kind"}),
            "written from subject_kind",
        ),
        (lambda p: p.update(slug="json"), "would shadow"),
        (lambda p: p["tasks"].append(dict(p["tasks"][0])), "more than 1 items"),
    ],
)
def test_plan_rules(change, expected):
    assert any(expected in line for line in edit_plan(change)), edit_plan(change)


def test_a_timeline_never_holds_a_number():
    phases = PLAN_SCHEMA["properties"]["timeline"]["items"]["properties"]
    assert phases["duration"]["enum"] == ["parameterized", "event-driven", "instant"]
    problems_found = edit_plan(lambda p: p["timeline"][1].update(duration={"ms": 300}))
    assert any("timeline[1].duration" in line for line in problems_found)


def test_plan_edits_are_validated_again(the_plan):
    edited = the_plan.edited(title="Flash and fixation", notes="Reviewed.")
    assert edited.title == "Flash and fixation" and edited.notes == "Reviewed."
    assert edited.parameters == the_plan.parameters
    with pytest.raises(author.PlanInvalid):
        the_plan.edited(title="x" * 500)


# ---------------------------------------------------------------------------
# 4. Sources
# ---------------------------------------------------------------------------


def test_generated_bundle_is_a_valid_package(bundle, the_plan):
    assert bundle.report.ok, bundle.report.problems
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "p.zip"
        path.write_bytes(bundle.archive)
        info = inspect_bundle(path)
        documentation = read_documentation(path, info.manifest)
    assert info.sha256 == bundle.sha256
    manifest = info.manifest
    assert manifest["name"] == the_plan.slug == "fixation-flash-hold"
    assert manifest["alhazen_min"] == author.release_of(ALHAZEN_VERSION)
    assert manifest["license"] == "MIT"
    assert manifest["documentation"] == "docs/experiment.json"
    assert manifest["hardware"] == the_plan.hardware
    assert {f["path"] for f in manifest["files"]} == set(bundle.files)
    assert documentation is not None
    task = documentation["tasks"][0]
    statuses = {p["name"]: p["source"]["status"] for p in task["parameters"]}
    assert set(statuses.values()) == {"matched"}
    assert set(statuses) == {"subject_kind", *(p["name"] for p in the_plan.parameters)}
    assert task["parameters_undocumented"] == []


def test_generated_files(bundle, the_plan):
    files = {path: data.decode("utf-8") for path, data in bundle.files.items()}
    assert set(files) == {
        "run.py",
        "pyproject.toml",
        ".gitignore",
        "src/fixation_flash_hold/__init__.py",
        "src/fixation_flash_hold/task.py",
        "tests/test_task.py",
        "configs/task.yaml",
        "docs/experiment.json",
        "docs/methods.md",
        "docs/tasks/fixation-flash-hold.md",
        "docs/ai-provenance.json",
        "README.md",
        "LICENSE",
    }
    params = yaml.safe_load(files["configs/task.yaml"])
    assert params["subject_kind"] == "human"
    assert params["paradigm"] == {"kind": "sequence", "n_per_condition": 20}
    assert params["hold_duration"] == {"ms": 1000}
    run = ast.parse(files["run.py"])
    assigned = {
        target.id
        for node in run.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "PARAMETERS" in assigned and "LADDERS" not in assigned
    parameters = next(
        node.value
        for node in run.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PARAMETERS"
    )
    assert isinstance(parameters, ast.Dict)
    assert [key.value for key in parameters.keys] == [the_plan.title]  # type: ignore[union-attr]
    assert files["LICENSE"].startswith("MIT License")
    pyproject = files["pyproject.toml"]
    assert 'fixation-flash-hold = "fixation_flash_hold.task:FixationFlashHoldTask"' in pyproject
    provenance = json.loads(files["docs/ai-provenance.json"])
    assert provenance["ai_assisted"] is True and provenance["model"] == "fake-model-1"
    assert provenance["started_from"] is None


def test_run_py_calls_run_experiment_as_the_scaffold_does(bundle):
    def keywords(text: str) -> set[str]:
        call = next(
            node
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_experiment"
        )
        return {kw.arg for kw in call.keywords if kw.arg}

    scaffold = author.scaffold_files()["run.py"]
    assert keywords(bundle.files["run.py"].decode()) == keywords(scaffold)


def test_generation_is_deterministic(the_plan, ctx, bundle):
    again = author.generate_source(FakeProvider(), the_plan, ctx)
    assert again.files == bundle.files and again.sha256 == bundle.sha256


def test_source_request_carries_names_params_file_and_context(the_plan, ctx):
    provider = FakeProvider()
    author.generate_source(provider, the_plan, ctx)
    request = provider.requests[0]
    assert request["json_schema"] == for_provider(SOURCE_SCHEMA)
    user = request["messages"][1]["content"]
    names = author.names_of(the_plan)
    assert author.params_yaml(the_plan, names) in user
    for value in names.for_prompt().values():
        assert value in user
    for name, text in ctx.source_items:
        assert text in user, name


def test_source_repairs_once_with_the_report(the_plan, ctx):
    provider = FakeProvider(["rules_invalid", "valid"])
    result = author.generate_source(provider, the_plan, ctx)
    assert result.report.ok
    repair = provider.requests[1]["messages"][-1]["content"]
    assert "syntax: src/fixation_flash_hold/task.py:" in repair


@pytest.mark.parametrize("behaviour", ["invalid_json", "schema_invalid", "rules_invalid"])
def test_source_invalid_after_one_repair(the_plan, ctx, behaviour):
    provider = FakeProvider([behaviour])
    with pytest.raises(author.SourceInvalid) as raised:
        author.generate_source(provider, the_plan, ctx)
    assert len(provider.requests) == 2
    assert not raised.value.report.ok


FORBIDDEN = [
    ("import subprocess\n", "starting processes"),
    ("import socket\n", "network access"),
    ("import requests\n", "network access"),
    ("import os\n", "operating-system and file access"),
    ("import importlib\n", "dynamic code"),
    ("import scipy\n", "does not depend on"),
    ("import sqlite3\n", "does not need"),
    ("X = eval('1')\n", "eval"),
    ("exec('x = 1')\n", "exec"),
    ("M = __import__('json')\n", "__import__"),
    ("def f(obj, name):\n    return getattr(obj, name)\n", "getattr with a computed name"),
    ("def f(p):\n    open(p, 'w').write('x')\n", "opens a file for writing"),
    ("def f(p):\n    p.write_text('x')\n", "write_text() writes to disk"),
    ("def f(p):\n    p.unlink()\n", "unlink() writes to disk"),
    ("import pathlib\nK = pathlib.Path.home().environ\n", ".environ"),
    ("from alhazen.task.phases import FlashPhase\n", "alhazen.task.phases has no FlashPhase"),
    ("from alhazen.nothing import X\n", "alhazen has no module alhazen.nothing"),
    ("from alhazen.cli.modes import run_experiment\n", "task code does not import alhazen.cli"),
    ("from alhazen.testing import FakeClock\n", "imports alhazen.testing"),
    ("from alhazen.stimuli.base import NullStimulus\n", "stand-in that draws nothing"),
    ("from . import task\n", "relative import"),
]


@pytest.mark.parametrize(("line", "expected"), FORBIDDEN)
def test_forbidden_patterns_are_reported_not_run(the_plan, ctx, line, expected):
    module = task_module().replace("\nimport numpy as np\n", f"\nimport numpy as np\n{line}", 1)
    report = failing(the_plan, ctx, source_text(task_module=module))
    assert any(expected in problem for problem in report.problems), report.problems


def test_attribute_of_an_alhazen_module_must_exist(the_plan, ctx):
    module = task_module().replace("phases.AcquireFixation(", "phases.AcquireFixationNow(", 1)
    report = failing(the_plan, ctx, source_text(task_module=module))
    assert any("alhazen.task.phases has no AcquireFixationNow" in p for p in report.problems)


def test_tests_may_write_files_and_import_pytest(the_plan, ctx):
    tests = source_answer()["test_module"] + (
        "\n\ndef test_writes(tmp_path):\n    (tmp_path / 'x').write_text('ok')\n"
    )
    result = author.generate_source(
        FakeProvider(source_text=source_text(test_module=tests)), the_plan, ctx
    )
    assert result.report.ok, result.report.problems


def test_a_test_importing_a_missing_name_fails(the_plan, ctx):
    tests = source_answer()["test_module"].replace(
        "FixationFlashHoldTaskParams\n", "FixationFlashHoldTaskParams, Missing\n", 1
    )
    report = failing(the_plan, ctx, source_text(test_module=tests))
    assert any("defines no Missing" in p for p in report.problems), report.problems


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("Duration(ms=1000)", "Duration(ms=900)", 'hold_duration defaults to {"ms": 900}'),
        (
            "fix_window_dva: float = 2.0",
            "fix_window_dva: float = 2.5",
            "fix_window_dva defaults to 2.5",
        ),
        (
            'SchedulerConfig(kind="sequence", n_per_condition=20)',
            'SchedulerConfig(kind="sequence", n_per_condition=10)',
            "paradigm.n_per_condition defaults to 10",
        ),
        ("    flash_size_dva: float = 1.0\n", "", "has no field flash_size_dva"),
        (
            "    fix_size_dva: float = 2.0\n",
            "    fix_size_dva: float = 2.0\n    gap_dva: float\n",
            "gap_dva has no default",
        ),
        ("(SubjectParams):", "(Model):", "must subclass SubjectParams"),
        (
            "    paradigm: SchedulerConfig",
            '    subject_kind: str = "human"\n    paradigm: SchedulerConfig',
            "redeclares subject_kind",
        ),
        ('name = "fixation-flash-hold"', 'name = "other"', 'name must be "fixation-flash-hold"'),
        ('"FLASH_ON"))', '"FLASH_ON", "EXTRA"))', "the task declares"),
        ("    def conditions(", "    def conditions_unused(", "defines no conditions()"),
    ],
)
def test_structure_and_defaults_against_the_params_file(the_plan, ctx, old, new, expected):
    module = task_module()
    assert old in module
    report = failing(the_plan, ctx, source_text(task_module=module.replace(old, new, 1)))
    assert any(expected in problem for problem in report.problems), report.problems


def test_unknown_keys_of_alhazen_models_are_found(ctx):
    plan = json.loads(plan_answer())
    plan["parameters"].append(
        {
            "name": "paradigm.repeats",
            "label": "Repeats",
            "group": "Design",
            "type": "integer",
            "unit": None,
            "default": 2,
            "constraints": {"min": None, "max": None, "choices": None, "note": None},
            "meaning": "Not a scheduler field.",
        }
    )
    report = failing(author.Plan.from_dict(plan), ctx, json.dumps(source_answer()))
    assert any("paradigm.repeats is not a field of SchedulerConfig" in p for p in report.problems)


def _doc(change) -> str:
    doc = json.loads(source_answer()["task_documentation_json"])
    change(doc)
    return json.dumps(doc)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda d: d["timeline"]["phases"][1].update(timing={"kind": "fixed", "ms": 300}),
            "has a fixed duration",
        ),
        (lambda d: d["events"].pop(), "the documentation lists"),
        (lambda d: d.update(parameters=[]), "unexpected key 'parameters'"),
        (lambda d: d.pop("diagram"), "missing key 'diagram'"),
        (
            lambda d: d["timeline"].update(
                between_trials={"label": "ITI", "timing": {"kind": "parameter", "param": "iti"}}
            ),
            "names 'iti', which is not a documented parameter",
        ),
    ],
)
def test_documentation_checks(the_plan, ctx, change, expected):
    report = failing(the_plan, ctx, source_text(task_documentation_json=_doc(change)))
    assert any(expected in problem for problem in report.problems), report.problems


def test_unparseable_documentation_json_is_reported(the_plan, ctx):
    report = failing(the_plan, ctx, source_text(task_documentation_json="{not json"))
    assert any("task_documentation_json: not valid JSON" in p for p in report.problems)


def test_size_limits(the_plan, ctx, bundle):
    report = failing(the_plan, ctx, source_text(readme_markdown="x" * 100_001))
    assert any("source.readme_markdown: longer than" in p for p in report.problems)
    files = dict(bundle.files)
    files["README.md"] = b"x" * (author.MAX_FILE_BYTES + 1)
    metadata = author.manifest_metadata(the_plan, author.release_of(ALHAZEN_VERSION), [])
    report, _, _ = author.validate_package(the_plan, files, metadata)
    assert any(p.startswith("files: README.md:") and "at most" in p for p in report.problems)
    files["docs/methods.md"] = b"y" * author.MAX_FILE_BYTES
    files["docs/extra.md"] = b"z" * author.MAX_FILE_BYTES * 8
    report, _, _ = author.validate_package(the_plan, files, metadata)
    assert any("in all; a generated package is at most" in p for p in report.problems)


def test_report_shape(bundle):
    report = bundle.report.to_dict()
    assert report["ok"] is True
    assert [check["name"] for check in report["checks"]] == [
        "answer",
        "schema",
        "files",
        "syntax",
        "safety",
        "imports",
        "structure",
        "defaults",
        "package",
        "documentation",
    ]
    assert {entry["path"] for entry in report["files"]} == set(bundle.files)


def test_bundle_archive_rebuilds_with_new_metadata(bundle, the_plan):
    metadata = author.manifest_metadata(the_plan, author.release_of(ALHAZEN_VERSION), [])
    metadata.update(title="Accepted title", license="BSD-3-Clause")
    archive, info = author.bundle_archive(bundle.files, metadata)
    assert info.manifest["title"] == "Accepted title"
    assert info.sha256 != bundle.sha256


# ---------------------------------------------------------------------------
# 5. The recorded live run
# ---------------------------------------------------------------------------


def test_live_run_record():
    exchanges = live_exchanges()
    assert [(e["step"], e["attempt"]) for e in exchanges] == [
        ("plan", 1),
        ("plan", 2),
        ("source", 1),
        ("source", 2),
    ]
    assert {e["model"] for e in exchanges} == {"gpt-4.1-mini-2025-04-14"}
    assert all(e["finish_reason"] == "stop" for e in exchanges)


def test_live_plan_failure_then_repair_reproduce(ctx):
    exchanges = live_exchanges()
    first, report = author._plan_attempt(exchanges[0]["text"])
    assert first is None
    assert all("outside min..max" in p for p in report.problems)
    second, _ = author._plan_attempt(exchanges[1]["text"])
    assert second is not None


def test_live_source_failure_mode_reproduces(the_plan, ctx):
    """Both live source answers fail; the second exactly as recorded (its
    timeline names an `iti` the plan never had), plus the checks added after
    the run (a NullStimulus flash, a redeclared subject_kind)."""
    exchanges = live_exchanges()
    recorded = json.loads((FIXTURES / "live" / "source-report.json").read_text("utf-8"))
    recorded_problems = [
        f"{check['name']}: {line}" for check in recorded["checks"] for line in check["problems"]
    ]
    provider = FakeProvider(["valid"], source_text=exchanges[2]["text"])
    with pytest.raises(author.SourceInvalid):
        author.generate_source(provider, the_plan, ctx)
    second = FakeProvider(["valid"], source_text=exchanges[3]["text"])
    with pytest.raises(author.SourceInvalid) as raised:
        author.generate_source(second, the_plan, ctx)
    now = raised.value.report.problems
    assert set(recorded_problems) <= set(now)
    assert any("NullStimulus" in p for p in now)
    assert any("redeclares subject_kind" in p for p in now)


def test_valid_fixture_is_what_the_kit_builds(the_plan, ctx):
    live_model = live_exchanges()[3]["model"]
    bundle = author.generate_source(FakeProvider(model=live_model), the_plan, ctx)
    recorded = FIXTURES / "valid" / "package.zip"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "p.zip"
        path.write_bytes(recorded.read_bytes())
        info = inspect_bundle(path)
    # Compare declared files by hash rather than ZIP bytes: deflate output
    # may differ between zlib builds.
    by_path = {f["path"]: f["sha256"] for f in info.manifest["files"]}
    rebuilt = {f["path"]: f["sha256"] for f in bundle.manifest["files"]}
    assert by_path.keys() == rebuilt.keys()
    assert by_path == rebuilt
    report = json.loads((FIXTURES / "valid" / "report.json").read_text("utf-8"))
    assert report == bundle.report.to_dict()


def test_valid_fixture_corrections_are_the_documented_ones():
    """The hand corrections to the live answer touch only what README.md lists."""
    live = json.loads(live_exchanges()[3]["text"])
    corrected = source_answer()
    changed = sorted(key for key in corrected if corrected[key] != live[key])
    assert changed == ["task_documentation_json", "task_markdown", "task_module"]
    assert re.search(r"NullStimulus", live["task_module"])
    assert "NullStimulus" not in corrected["task_module"]
