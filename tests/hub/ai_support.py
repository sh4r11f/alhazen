"""A provider stand-in for AI authoring tests: no network, recorded answers.

``FakeProvider`` answers the plan and source requests with the recorded
example in ``fixtures/ai`` (a real gpt-4.1-mini plan and a source answer
corrected by hand; see that folder's README), and can be scripted to answer
badly or fail the way a provider does. It records every request, so a test
can check exactly what would have been disclosed.

    provider = FakeProvider()                                # always valid
    provider = FakeProvider(["invalid_json", "valid"])       # one bad answer, then good
    provider = FakeProvider(["quota"])                       # the provider is out of credit
    provider = FakeProvider(source_queue=[bad, good])        # these source answers in order

Behaviours, one consumed per request (the last repeats):
``valid``, ``invalid_json``, ``schema_invalid``, ``rules_invalid`` (valid JSON
that breaks the plan rules, or a source or run-repair answer whose task module
does not compile), and the provider failures ``quota``, ``auth``, ``timeout``,
``invalid``, ``other`` (raised as ``ProviderError(kind=...)``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
PROVIDER_FAILURES = ("quota", "auth", "timeout", "invalid", "other")

try:  # the server's provider errors, once providers.py is integrated
    from alhazen.hub.ai.providers import ProviderError
except ImportError:  # pragma: no cover - only before integration

    class ProviderError(Exception):  # type: ignore[no-redef]
        """Stand-in with the contract's shape: ``kind`` is one of
        quota, auth, timeout, invalid, other."""

        def __init__(self, message: str = "", *, kind: str = "other") -> None:
            super().__init__(message)
            self.kind = kind


def _provider_error(kind: str) -> Exception:
    message = f"fake provider: {kind}"
    try:
        return ProviderError(message, kind=kind)
    except TypeError:
        return ProviderError(kind=kind, message=message)


def live_exchanges() -> list[dict[str, Any]]:
    """The four recorded gpt-4.1-mini exchanges (two plan, two source)."""
    return json.loads((FIXTURES / "live" / "exchanges.json").read_text("utf-8"))


def plan_answer() -> str:
    """A valid plan answer: the live run's repaired plan, as answered."""
    return next(e["text"] for e in live_exchanges() if e["step"] == "plan" and e["attempt"] == 2)


def source_answer() -> dict[str, Any]:
    """A valid source answer: the live run's second answer, corrected by hand."""
    return json.loads((FIXTURES / "valid" / "source-answer.json").read_text("utf-8"))


def _rules_invalid_plan() -> str:
    plan = json.loads(plan_answer())
    plan["timeline"][1]["parameter"] = "no_such_parameter"
    return json.dumps(plan)


def _rules_invalid_source() -> str:
    answer = source_answer()
    answer["task_module"] = answer["task_module"] + "\ndef broken(:\n"
    return json.dumps(answer)


def repair_answer() -> dict[str, Any]:
    """A valid run-repair answer for the package built from the valid
    fixture (a comment added to build_trial; nothing else changes)."""
    return json.loads((FIXTURES / "repair" / "default-answer.json").read_text(encoding="utf-8"))


def _rules_invalid_repair() -> str:
    answer = repair_answer()
    answer["task_module"] = answer["task_module"] + "\ndef broken(:\n"
    return json.dumps(answer)


@dataclass(frozen=True)
class FakeCompletion:
    text: str
    usage: dict[str, Any]
    model: str


@dataclass
class FakeProvider:
    """Scripted provider; ``requests`` holds every request in order."""

    behaviours: list[str] = field(default_factory=lambda: ["valid"])
    plan_text: str | None = None
    source_text: str | None = None
    # Valid-behaviour source answers to give in order before source_text (a
    # live failure, then its repair).
    source_queue: list[str] = field(default_factory=list)
    # Run-repair answers (schema alhazen_repair), in order, before repair_text.
    repair_queue: list[str] = field(default_factory=list)
    repair_text: str | None = None
    model: str = "fake-model-1"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> FakeCompletion:
        self.requests.append(
            {
                "messages": [dict(message) for message in messages],
                "json_schema": json_schema,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        index = len(self.requests) - 1
        behaviour = self.behaviours[min(index, len(self.behaviours) - 1)]
        if behaviour in PROVIDER_FAILURES:
            raise _provider_error(behaviour)
        step = (json_schema or {}).get("title")
        if behaviour == "invalid_json":
            text = '{"title": "unfinished'
        elif behaviour == "schema_invalid":
            text = json.dumps({"title": "only a title"})
        elif behaviour == "rules_invalid":
            if step == "alhazen_plan":
                text = _rules_invalid_plan()
            elif step == "alhazen_repair":
                text = _rules_invalid_repair()
            else:
                text = _rules_invalid_source()
        elif behaviour == "valid" and step == "alhazen_repair":
            if self.repair_queue:
                text = self.repair_queue.pop(0)
            elif self.repair_text is not None:
                text = self.repair_text
            else:
                text = json.dumps(repair_answer())
        elif behaviour == "valid":
            if step == "alhazen_plan":
                text = self.plan_text if self.plan_text is not None else plan_answer()
            elif self.source_queue:
                text = self.source_queue.pop(0)
            else:
                text = (
                    self.source_text
                    if self.source_text is not None
                    else json.dumps(source_answer())
                )
        else:
            raise ValueError(f"unknown behaviour {behaviour!r}")
        chars = sum(len(message["content"]) for message in messages)
        return FakeCompletion(
            text=text,
            usage={"prompt_tokens": chars // 4, "completion_tokens": len(text) // 4},
            model=self.model,
        )

    def sent_text(self) -> str:
        """Everything sent, every message of every request, as one text."""
        return "\n".join(
            message["content"] for request in self.requests for message in request["messages"]
        )
