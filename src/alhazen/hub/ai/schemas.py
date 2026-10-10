"""The JSON shapes a provider must answer in, and the checker for them.

Two documents cross the provider boundary: the *plan* (what the user reviews
before any code exists) and the *source* answer (the files the model writes;
everything else in a package is assembled by :mod:`alhazen.hub.ai.author`).
Both schemas are kept here so the server, the browser and the tests read one
definition.

The schemas are written in the subset of JSON Schema that strict
structured-output modes accept: every object closes with
``additionalProperties: false`` and lists every property as required, and an
optional value is a ``null`` alternative rather than a missing key.
:func:`for_provider` removes the length keywords such modes refuse; the full
schema, lengths included, is what :func:`problems` checks locally, because a
provider's own enforcement is never relied on.

The plan, as the browser receives it (``Plan.to_dict()``)::

    {title, slug, summary, paradigm, subject_kind: "human" | "monkey", license,
     hardware: {display, eye_tracker, reward},
     design: [{label, value}],            # conditions, trial counts, session length
     stimuli: [{name, size, position, notes}],
     measures: [{name, unit, definition}],
     parameters: [{name, label, group, type, unit | null, default,
                   constraints: {min, max, choices, note} (each may be null),
                   meaning}],
     timeline: [{phase, duration: "parameterized" | "event-driven" | "instant",
                 parameter | null, until | null, note}],
     tasks: [{name, description}],        # exactly one in this phase
     tests: [{name, how}],
     notes}

``timeline[].duration`` never holds a number: a timed phase names the
parameter that sets it, a phase that waits on the subject says what it waits
``until``, so no plan asserts a length the experiment does not have.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from typing import Any

from alhazen.hub.documentation import PARAMETER_TYPES

SUBJECT_KINDS = ("human", "monkey")
TIMELINE_DURATIONS = ("parameterized", "event-driven", "instant")
# A licence the assembler can write a LICENSE file for. "Proprietary" keeps
# all rights; the others are their SPDX identifiers.
LICENSES = ("MIT", "BSD-3-Clause", "Apache-2.0", "CC-BY-4.0", "Proprietary")

SLUG_PATTERN = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
PARAMETER_NAME_PATTERN = r"^[a-z_][a-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,5}$"
MAX_TASKS = 1


def _text(
    max_length: int,
    *,
    nullable: bool = False,
    pattern: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"] if nullable else "string"}
    schema["maxLength"] = max_length
    if pattern is not None:
        schema["pattern"] = pattern
    if description is not None:
        schema["description"] = description
    return schema


def _object(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
        **extra,
    }


def _rows(item: dict[str, Any], max_items: int, min_items: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": item, "maxItems": max_items}
    if min_items:
        schema["minItems"] = min_items
    return schema


_SCALAR = [
    {"type": "number"},
    {"type": "boolean"},
    {"type": "string", "maxLength": 400},
]
DEFAULT_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        *_SCALAR,
        _object({"ms": {"type": "number", "minimum": 0}}),
        _object({"frames": {"type": "integer", "minimum": 0}}),
        {"type": "array", "items": {"anyOf": _SCALAR}, "maxItems": 64},
    ]
}

PLAN_SCHEMA: dict[str, Any] = _object(
    {
        "title": _text(160),
        "slug": _text(64, pattern=SLUG_PATTERN),
        "summary": _text(1000),
        "paradigm": _text(
            4000,
            description="Prose: what one trial asks of the subject, how conditions differ "
            "and how they are served.",
        ),
        "subject_kind": {"type": "string", "enum": list(SUBJECT_KINDS)},
        "license": {"type": "string", "enum": list(LICENSES)},
        "hardware": _object(
            {
                "display": {"type": "boolean"},
                "eye_tracker": {"type": "boolean"},
                "reward": {"type": "boolean"},
            }
        ),
        "design": _rows(
            _object(
                {
                    "label": _text(80, description="Conditions, Trials, Session, Subject..."),
                    "value": _text(400),
                }
            ),
            12,
        ),
        "stimuli": _rows(
            _object(
                {
                    "name": _text(120),
                    "size": _text(200, description="A value with its unit, e.g. '1.0 dva'."),
                    "position": _text(200, description="Where, with units, e.g. '8 dva right'."),
                    "notes": _text(400),
                }
            ),
            40,
        ),
        "measures": _rows(
            _object({"name": _text(120), "unit": _text(24), "definition": _text(600)}), 40
        ),
        "parameters": _rows(
            _object(
                {
                    "name": _text(200, pattern=PARAMETER_NAME_PATTERN),
                    "label": _text(120),
                    "group": _text(60),
                    "type": {"type": "string", "enum": list(PARAMETER_TYPES)},
                    "unit": _text(24, nullable=True),
                    "default": DEFAULT_VALUE_SCHEMA,
                    "constraints": _object(
                        {
                            "min": {"type": ["number", "null"]},
                            "max": {"type": ["number", "null"]},
                            "choices": {
                                "type": ["array", "null"],
                                "items": {"anyOf": _SCALAR},
                                "maxItems": 64,
                            },
                            "note": _text(400, nullable=True),
                        }
                    ),
                    "meaning": _text(2000),
                }
            ),
            120,
            min_items=1,
        ),
        "timeline": _rows(
            _object(
                {
                    "phase": _text(120, description="A plain label, e.g. 'Hold fixation'."),
                    "duration": {"type": "string", "enum": list(TIMELINE_DURATIONS)},
                    "parameter": _text(200, nullable=True),
                    "until": _text(
                        300,
                        nullable=True,
                        description="For an event-driven phase, what ends it, in words.",
                    ),
                    "note": _text(400),
                }
            ),
            24,
            min_items=1,
        ),
        "tasks": _rows(
            _object({"name": _text(64, pattern=SLUG_PATTERN), "description": _text(2000)}),
            MAX_TASKS,
            min_items=1,
        ),
        "tests": _rows(_object({"name": _text(160), "how": _text(600)}), 30, min_items=1),
        "notes": _text(
            4000, description="Every value you chose that the description did not give."
        ),
    },
    title="alhazen_plan",
)

# What the model writes in the source step. Python and Markdown are text
# fields; the task's documentation is a JSON *text* holding the part of the
# descriptor only the model can write (outcomes, events, timeline, diagram),
# because that grammar is owned by alhazen.hub.documentation and is checked by
# its loader, not restated here.
SOURCE_SCHEMA: dict[str, Any] = _object(
    {
        "task_module": _text(200_000),
        "test_module": _text(200_000),
        "task_documentation_json": _text(100_000),
        "references": _rows(_text(600), 100),
        "methods_markdown": _text(100_000),
        "task_markdown": _text(100_000),
        "readme_markdown": _text(100_000),
    },
    title="alhazen_source",
)

_PROVIDER_REFUSED_KEYWORDS = frozenset({"maxLength", "minLength"})
_KNOWN_KEYWORDS = frozenset(
    {
        "type", "enum", "properties", "required", "additionalProperties", "items", "anyOf",
        "maxLength", "minLength", "pattern", "minimum", "maximum", "maxItems", "minItems", "title",
        "description",
    }
)
_MAX_PROBLEMS = 50


def for_provider(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` without the keywords strict structured-output modes refuse.

    The lengths still bind: :func:`problems` checks the full schema."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: strip(value)
                for key, value in node.items()
                if key not in _PROVIDER_REFUSED_KEYWORDS
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(copy.deepcopy(dict(schema)))


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    raise ValueError(f"schema uses an unsupported type {kind!r}")


def _check(value: Any, schema: Mapping[str, Any], where: str) -> Iterator[str]:
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        raise ValueError(f"schema uses unsupported keywords {sorted(unknown)}")
    if "anyOf" in schema:
        if not any(not list(_check(value, option, where)) for option in schema["anyOf"]):
            yield f"{where}: does not match any allowed form"
        return
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_type_ok(value, kind) for kind in kinds):
            yield f"{where}: must be {' or '.join(kinds)}"
            return
    if value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        yield f"{where}: must be one of {', '.join(map(str, schema['enum']))}"
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            yield f"{where}: longer than {schema['maxLength']} characters"
        if "minLength" in schema and len(value) < schema["minLength"]:
            yield f"{where}: shorter than {schema['minLength']} characters"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            yield f"{where}: {value[:60]!r} does not match {schema['pattern']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value or value in (float("inf"), float("-inf")):
            yield f"{where}: must be finite"
        if "minimum" in schema and value < schema["minimum"]:
            yield f"{where}: below {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            yield f"{where}: above {schema['maximum']}"
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            yield f"{where}: more than {schema['maxItems']} items"
        if "minItems" in schema and len(value) < schema["minItems"]:
            yield f"{where}: needs at least {schema['minItems']} item(s)"
        if "items" in schema:
            for index, item in enumerate(value):
                yield from _check(item, schema["items"], f"{where}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                yield f"{where}.{key}: missing"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    yield f"{where}: unexpected field {str(key)[:60]!r}"
        for key, sub in properties.items():
            if key in value:
                yield from _check(value[key], sub, f"{where}.{key}")


def problems(value: Any, schema: Mapping[str, Any], *, root: str = "$") -> list[str]:
    """Every way ``value`` departs from ``schema`` (at most 50), as
    ``path: message`` lines; empty when it conforms.

    Supports the keywords the schemas in this module use (type, enum,
    properties, required, additionalProperties, items, anyOf, lengths,
    pattern, bounds); any other keyword in a schema is a bug and raises."""
    found: list[str] = []
    for line in _check(value, schema, root):
        found.append(line)
        if len(found) >= _MAX_PROBLEMS:
            found.append("(further problems not listed)")
            break
    return found
