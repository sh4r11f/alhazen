"""Server-side test doubles for AI authoring: a scripted provider and a
minimal authoring kit with the contract's signatures (build_context, plan,
generate_source, StartFrom, Plan.from_dict, PlanInvalid, SourceInvalid).

The real kit (alhazen.hub.ai.author) and its FakeProvider
(tests/hub/ai_support.py) are developed in parallel; these stand in for them
so the server's job, disclosure and acceptance paths are tested on their
own. No network: every answer is canned.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

from alhazen.hub.ai.jobs import AuthorKit
from alhazen.hub.ai.providers import Completion, ProviderError

PLAN = {
    "title": "Saccade latency to a gap",
    "summary": "Synthetic gap-saccade task for tests.",
    "paradigm": "gap saccade",
    "subject_kind": "human",
    "stimuli": ["fixation dot", "peripheral target"],
    "measures": ["saccade latency"],
    "parameters": [
        {"name": "gap_ms", "meaning": "gap", "unit": "ms", "default": 200, "constraints": ">=0"}
    ],
    "timeline": [{"phase": "fixation", "duration": "parameterized", "note": ""}],
    "tasks": [{"name": "gap", "description": "one task"}],
    "tests": [{"name": "simulate", "how": "--mode simulate"}],
    "hardware": {"display": True, "eye_tracker": True, "reward": False},
    "notes": "",
}


@dataclass
class StartFrom:
    experiment_title: str
    version: str
    files: dict[str, str]


@dataclass
class Context:
    alhazen_version: str
    start: StartFrom | None


@dataclass
class Plan:
    fields: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(dict(data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.fields)


@dataclass
class Report:
    ok: bool
    problems: list[str] = field(default_factory=list)


class PlanInvalid(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__("plan invalid")
        self.report = report


class SourceInvalid(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__("source invalid")
        self.report = report


@dataclass
class Bundle:
    files: dict[str, bytes]
    manifest: dict[str, Any]
    report: Report


def build_context(alhazen_version: str, start: StartFrom | None) -> Context:
    return Context(alhazen_version, start)


def _messages(prompt: str, ctx: Context) -> list[dict[str, Any]]:
    parts = [f"PROMPT: {prompt}"]
    if ctx.start is not None:
        for path, text in sorted(ctx.start.files.items()):
            parts.append(f"FILE {path}:\n{text}")
    return [
        {"role": "system", "content": "You write alhazen experiments."},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def plan(client: Any, prompt: str, ctx: Context) -> Plan:
    for _attempt in range(2):  # one repair round
        answer = client.complete(
            _messages(prompt, ctx), json_schema={"type": "object"}, max_tokens=4000
        )
        try:
            data = json.loads(answer.text)
        except ValueError:
            data = None
        if isinstance(data, dict) and "title" in data:
            return Plan(data)
    raise PlanInvalid(Report(False, ["answer is not a plan"]))


def generate_source(client: Any, plan_obj: Plan, ctx: Context) -> Bundle:
    answer = client.complete(
        [{"role": "user", "content": "PLAN " + json.dumps(plan_obj.fields)}],
        json_schema={"type": "object"},
        max_tokens=16000,
    )
    data = json.loads(answer.text)
    if not data.get("files"):
        raise SourceInvalid(Report(False, ["no files"]))
    files = {path: text.encode() for path, text in data["files"].items()}
    manifest = {
        "name": data.get("name", "gap-saccade"),
        "version": "0.1.0",
        "title": plan_obj.fields.get("title", "Untitled"),
        "description": plan_obj.fields.get("summary", ""),
        "hardware": {"display": True, "eye_tracker": True, "reward": False},
        "license": "MIT",
        "citations": [],
    }
    return Bundle(files, manifest, Report(True))


def repair_from_run(client: Any, bundle: Any, log: str, ctx: Context) -> Bundle:
    """Send the log and the base package's file list; the answer replaces files."""
    answer = client.complete(
        [
            {
                "role": "user",
                "content": "RUN LOG\n" + log + "\nBASE FILES " + ",".join(sorted(bundle.files)),
            }
        ],
        json_schema={"type": "object"},
        max_tokens=16000,
    )
    data = json.loads(answer.text)
    if not data.get("files"):
        raise SourceInvalid(Report(False, ["no files"]))
    files = dict(bundle.files)
    files.update({path: text.encode() for path, text in data["files"].items()})
    manifest = {k: v for k, v in bundle.manifest.items() if k not in ("files", "schema_version")}
    if "version" in data:
        manifest["version"] = data["version"]
    return Bundle(files, manifest, Report(True))


def kit() -> AuthorKit:
    module = SimpleNamespace(
        StartFrom=StartFrom,
        Plan=Plan,
        PlanInvalid=PlanInvalid,
        SourceInvalid=SourceInvalid,
        build_context=build_context,
        plan=plan,
        generate_source=generate_source,
        repair_from_run=repair_from_run,
    )
    return AuthorKit(module)


SOURCE = {
    "name": "gap-saccade",
    "files": {
        "run.py": "print('synthetic gap saccade')\n",
        "configs/task.yaml": "trials: 10\n",
        "README.md": "# Gap saccade\n",
    },
}


class ScriptedProvider:
    """Answers each call from ``script`` (text, a ProviderError, or a callable
    run before answering). Records every request it receives."""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.keys: list[str] = []
        self.verify_error: ProviderError | None = None

    def factory(self) -> Callable[..., Any]:
        provider = self

        def make(info: Any, key: str, model: str) -> Any:
            provider.keys.append(key)
            return _Client(provider, model)

        return make

    def sent_text(self) -> str:
        return "\n".join(m["content"] for r in self.requests for m in r["messages"])


class _Client:
    def __init__(self, provider: ScriptedProvider, model: str) -> None:
        self.provider = provider
        self.model = model

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Completion:
        self.provider.requests.append(
            {"messages": [dict(m) for m in messages], "max_tokens": max_tokens}
        )
        step = self.provider.script.pop(0) if self.provider.script else json.dumps(PLAN)
        while callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        return Completion(
            text=step, usage={"input_tokens": 10, "output_tokens": 5}, model=self.model
        )

    def verify(self) -> None:
        if self.provider.verify_error is not None:
            raise self.provider.verify_error


def plan_text() -> str:
    return json.dumps(PLAN)


def source_text(**changes: Any) -> str:
    return json.dumps({**SOURCE, **changes})


def plan_dict() -> dict[str, Any]:
    return json.loads(json.dumps(PLAN))


def as_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)


def repair_text(**changes: Any) -> str:
    return json.dumps({"files": {"run.py": "print('repaired: draw a Circle')\n"}, **changes})
