"""The reader document of one experiment version: its tasks resolved, plus compact guides.

``build_guide`` turns an experiment's documentation at one source version into the
resolved document the platform's reader draws (the "HubDocs" schema): the descriptor's
tasks resolved through ``documentation`` (parameter defaults checked against the params
files of that version), optional compact stimulus diagrams and trial timelines from a
**guide overlay**, and the methods with their parameter references made unambiguous.

The decisions it hides:

- **Version binding.** An overlay is authored for one bundle (one exact set of files). It
  is applied only when its ``bundleId`` equals the listing's bundle id; an overlay for
  another version is ignored and the document records a warning. It is never relabelled
  for a version it was not checked against (``overlay_applies``).
- **Legacy descriptor shapes** that the version-1 resolver cannot express are adapted
  without losing information: a parameter ``source`` given as a citation string becomes
  ``source.citation``; a ``null`` default is kept with ``nullable: true``; a choice with no
  enumerated choices is validated as a JSON value and keeps its declared type with a note;
  an inline task description becomes a generated Markdown file; tasks that share an id
  (one per parameters file) get distinct ids and references follow them.
- **Bounded drawing extensions** on diagram elements: ``color`` (a six-digit hex colour),
  ``font_size`` and ``stroke_width`` (numbers or parameter quantities, positive, at most
  200 diagram units), ``font_family`` and ``font_weight`` (closed lists). They are validated
  here and added to the resolver's validated geometry; there are no new shapes.
- **Bare references** in the methods (``[[param:name]]``) that several tasks own are
  rewritten to qualified references: to the first owner when every owner has the same
  value, otherwise to every owner, so the methods never silently pick one. Each rewrite is
  listed in ``referenceResolutions``.

The output schema is the one the platform's reader and the Mana reader consume:
``{schema_version: 1, bundleId, title, summary, source, methods, tasks, references,
warnings, referenceResolutions}``. Nothing here executes experiment code.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from alhazen.experiment_platform.documentation import (
    MAX_DESCRIPTOR_BYTES,
    TEXT_LIMITS,
    DocumentationError,
    check_references,
    parse_json,
    quantity,
    resolve_parameter_reference,
    resolve_task,
    same_value,
)

__all__ = ["DESCRIPTOR_PATH", "build_guide", "overlay_applies", "parse_overlay", "task_key"]

DESCRIPTOR_PATH = "docs/experiment.json"
GUIDE_NOTE = (
    "Methods and parameter values checked against this source version. Compact diagram "
    "annotations are a reader aid; the experiment code is unchanged."
)
# The fields of a descriptor task the version-1 resolver knows. Replication records
# (assumptions, deviations) stay in their own documentation fields and are not passed off
# as reader fields.
TASK_FIELDS = {
    "id",
    "title",
    "summary",
    "description",
    "parameters_file",
    "parameters",
    "outcomes",
    "events",
    "timeline",
    "diagram",
}
OVERLAY_TASK_FIELDS = {"taskKey", "timeline", "diagram", "description", "sourceFiles", "notes"}
MAX_OVERLAY_TASKS = 64
MAX_OVERLAY_NOTES = 32
EXTENSION_FIELDS = ("color", "font_size", "font_weight", "font_family", "stroke_width")
FONT_WEIGHTS = ("normal", "bold", 100, 200, 300, 400, 500, 600, 700, 800, 900)
FONT_FAMILIES = (
    "Arial",
    "Helvetica",
    "DejaVu Sans",
    "DejaVu Sans Mono",
    "Courier New",
    "sans-serif",
    "serif",
)
MAX_EXTENSION_VALUE = 200.0
UNDOCUMENTED_MEANING = "Meaning not documented in this version."
UNENUMERATED_CHOICE_NOTE = "Allowed choices are not enumerated in this documentation."
PARAM_REFERENCE = re.compile(r"\[\[param:([^]\s]+)\]\]")
COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


def task_key(task: Mapping[str, Any]) -> str:
    """A descriptor task's identity across versions of its descriptor: its id, plus
    ``@<parameters file>`` when it names one (two tasks may share an id, one per file)."""
    file = task.get("parameters_file")
    return str(task["id"]) + ("@" + str(file) if file else "")


def parse_overlay(overlay: Any) -> dict[str, Any] | None:
    """A guide overlay as ``{bundleId, tasks}``. Accepts that shape or the stored one,
    ``{bundleId, json: "<JSON text of {tasks: [...]}>"}`` (parsed with the documentation
    rules: bounded, no duplicate keys, no non-finite numbers). Each task record is checked
    for its fields; its drawings are checked when the guide is built."""
    if overlay is None:
        return None
    if not isinstance(overlay, Mapping):
        raise DocumentationError("the guide overlay must be an object")
    record = dict(overlay)
    if "json" in record:
        text = record.pop("json")
        if not isinstance(text, str):
            raise DocumentationError("the guide overlay's json field must be JSON text")
        parsed = parse_json(text, "guide overlay", MAX_DESCRIPTOR_BYTES)
        if not isinstance(parsed, dict):
            raise DocumentationError("the guide overlay must be an object")
        record.update(parsed)
    tasks = record.get("tasks") or []
    if not isinstance(tasks, list) or len(tasks) > MAX_OVERLAY_TASKS:
        raise DocumentationError(
            f"the guide overlay's tasks must be a list of at most {MAX_OVERLAY_TASKS}"
        )
    for index, task in enumerate(tasks):
        _check_overlay_task(task, f"guide overlay tasks[{index}]")
    record["tasks"] = tasks
    return record


def _check_overlay_task(task: Any, where: str) -> None:
    if not isinstance(task, dict):
        raise DocumentationError(f"{where} must be an object")
    extra = sorted(set(task) - OVERLAY_TASK_FIELDS)
    if extra:
        raise DocumentationError(f"{where} has unknown field(s): {', '.join(map(repr, extra))}")
    if not isinstance(task.get("taskKey"), str) or not task["taskKey"]:
        raise DocumentationError(f"{where}.taskKey is required")
    for field in ("notes", "sourceFiles"):
        values = task.get(field)
        if values is None:
            continue
        if not isinstance(values, list) or len(values) > MAX_OVERLAY_NOTES:
            raise DocumentationError(
                f"{where}.{field} must be a list of at most {MAX_OVERLAY_NOTES} texts"
            )
        for item in values:
            if not isinstance(item, str) or len(item) > TEXT_LIMITS["meaning"]:
                raise DocumentationError(
                    f"{where}.{field} entries must be text of at most "
                    f"{TEXT_LIMITS['meaning']} characters"
                )
    description = task.get("description")
    if description is not None and not isinstance(description, str):
        raise DocumentationError(f"{where}.description must be Markdown text")


def overlay_applies(overlay: Mapping[str, Any] | None, bundle_id: str) -> bool:
    """Whether a guide overlay may be used for the version whose bundle is ``bundle_id``:
    only when it was authored for exactly that bundle."""
    return bool(overlay) and isinstance(bundle_id, str) and overlay.get("bundleId") == bundle_id  # type: ignore[union-attr]


def _json_list(value: Any, what: str) -> list[Any]:
    """A docs field that is a list, or the JSON text of one (documentation records store
    lists as JSON strings)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = parse_json(value, what, MAX_DESCRIPTOR_BYTES * 4)
    if not isinstance(value, list):
        raise DocumentationError(f"{what} must be a list")
    return value


def _descriptor_tasks(
    descriptor: Mapping[str, Any] | None,
    docs: Mapping[str, Any],
    files: Mapping[str, bytes | str],
) -> list[Any]:
    if descriptor is None and DESCRIPTOR_PATH in files:
        descriptor = parse_json(files[DESCRIPTOR_PATH], DESCRIPTOR_PATH, MAX_DESCRIPTOR_BYTES)
    if descriptor is not None and not isinstance(descriptor, Mapping):
        raise DocumentationError("the documentation descriptor must be an object")
    tasks = (descriptor or {}).get("tasks") or _json_list(docs.get("tasks"), "docs tasks")
    if not isinstance(tasks, list):
        raise DocumentationError("documentation.tasks must be a list")
    return copy.deepcopy(tasks)


def _short_hash(text: str, length: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def _adapt_parameters(raw: dict[str, Any]) -> dict[str, Any]:
    """Adapt legacy parameter shapes in place for the version-1 resolver; returns what
    must be restored on the resolved records."""
    original_types: dict[str, Any] = {}
    citations: dict[str, str] = {}
    nullable: set[str] = set()
    unenumerated: set[str] = set()
    for p in raw.get("parameters") or []:
        if not isinstance(p, dict):
            continue  # the resolver refuses it, naming the entry
        if "meaning" not in p or not p["meaning"]:
            p["meaning"] = UNDOCUMENTED_MEANING
        name = str(p.get("name"))  # a malformed name is refused by the resolver
        original_types[name] = p.get("type")
        if isinstance(p.get("source"), str):
            citations[name] = p.pop("source")
        if p.get("type") == "choice" and not (p.get("constraints") or {}).get("choices"):
            # The v1 descriptor cannot express an unenclosed choice. Validate its value as
            # JSON without inventing a closed set; the declared type is restored below.
            p["type"] = "object"
            unenumerated.add(name)
        if "default" in p and p["default"] is None:
            p["type"] = "object"
            nullable.add(name)
    return {
        "types": original_types,
        "citations": citations,
        "nullable": nullable,
        "unenumerated": unenumerated,
    }


def _restore_parameters(resolved: dict[str, Any], adapted: dict[str, Any]) -> None:
    for p in resolved["parameters"]:
        name = p["name"]
        p["type"] = adapted["types"][name]
        if name in adapted["citations"]:
            p["source"]["citation"] = adapted["citations"][name]
        if name in adapted["nullable"]:
            p["nullable"] = True
        if name in adapted["unenumerated"]:
            note = p["constraints"].get("note") or ""
            p["constraints"]["note"] = (note + " " + UNENUMERATED_CHOICE_NOTE).strip()


def _apply_extensions(
    resolved: dict[str, Any], extensions: list[dict[str, Any]], where: str
) -> None:
    """Validate the bounded drawing extensions and add them to the resolved elements."""
    parameters = {p["name"]: p for p in resolved["parameters"]}
    for index, (element, extra) in enumerate(
        zip(resolved["diagram"]["elements"], extensions, strict=True)
    ):
        at = f"{where}.diagram.elements[{index}]"
        color = extra.get("color")
        if color is not None:
            if not isinstance(color, str) or not COLOUR.fullmatch(color):
                raise DocumentationError(f"{at}.color must be a six-digit hex colour (#rrggbb)")
            element["color"] = color
        for field in ("font_size", "stroke_width"):
            if field not in extra:
                continue
            if field == "font_size" and element["type"] != "text":
                raise DocumentationError(f"{at}.font_size only applies to text")
            value, reference = quantity(extra[field], f"{at}.{field}", parameters, positive=True)
            if value > MAX_EXTENSION_VALUE:
                raise DocumentationError(f"{at}.{field} is outside the drawable range")
            element[field] = value
            if reference:
                element["refs"][field] = reference
        if "font_weight" in extra:
            weight = extra["font_weight"]
            if isinstance(weight, bool) or weight not in FONT_WEIGHTS:
                raise DocumentationError(f"{at}.font_weight must be normal, bold or 100..900")
            element["font_weight"] = weight
        if "font_family" in extra:
            if extra["font_family"] not in FONT_FAMILIES:
                raise DocumentationError(
                    f"{at}.font_family must be one of {', '.join(FONT_FAMILIES)}"
                )
            element["font_family"] = extra["font_family"]


def build_guide(
    listing: Mapping[str, Any],
    docs: Mapping[str, Any],
    files: Mapping[str, bytes | str],
    *,
    descriptor: Mapping[str, Any] | None = None,
    overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The resolved reader document for ``listing`` at its bundle.

    ``docs`` is the version's documentation record (``methods``, ``readme``,
    ``references``, ``tasks``; lists may be JSON text). ``files`` are the experiment's
    files at that version. ``descriptor`` is the parsed documentation descriptor; when
    omitted it is read from ``docs/experiment.json`` in ``files``, and when that is absent
    the record's ``tasks`` are used. ``overlay`` is a guide overlay (see ``parse_overlay``)
    and is applied only if it is bound to ``listing.bundle.id``.

    Raises DocumentationError when the documentation cannot be used as written."""
    bundle = listing["bundle"]["id"]
    warnings: list[str] = []
    parsed_overlay = parse_overlay(overlay)
    if parsed_overlay is not None and not overlay_applies(parsed_overlay, bundle):
        bound = parsed_overlay.get("bundleId")
        warnings.append(
            f"guide overlay for {bound!r} ignored: it was not authored for this version "
            f"({bundle!r}); compact drawings are not shown"
            if bound
            else "guide overlay ignored: it names no bundleId, so it is bound to no version"
        )
        parsed_overlay = None
    files = dict(files)
    raw_tasks = _descriptor_tasks(descriptor, docs, files)
    additions = {r["taskKey"]: r for r in (parsed_overlay or {}).get("tasks", [])}
    used: set[str] = set()
    package = {"name": listing.get("id"), "version": listing.get("version")}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise DocumentationError("every documentation task must be an object with an id")
        original_id = raw["id"]
        key = task_key(raw)
        addition = additions.get(key)
        if addition:
            used.add(key)
            for field in ("diagram", "timeline"):
                if addition.get(field) is not None:
                    raw[field] = copy.deepcopy(addition[field])
            if addition.get("description") and not raw.get("description"):
                path = f"docs/mana-guides/{original_id}-{_short_hash(key, 8)}.md"
                files[path] = addition["description"]
                raw["description"] = path
        if raw["id"] in seen:
            raw["id"] = original_id + "-" + _short_hash(key, 7)
        seen.add(raw["id"])
        raw = {k: v for k, v in raw.items() if k in TASK_FIELDS}
        adapted = _adapt_parameters(raw)
        description = raw.get("description")
        if isinstance(description, str) and description not in files:
            # Legacy descriptors carry inline prose instead of a file path.
            if description.endswith((".md", ".markdown")) and "\n" not in description:
                raise DocumentationError(
                    f"task {original_id!r}: description file {description!r} is not in this "
                    f"source version"
                )
            path = f"docs/mana-guides/inline-{_short_hash(key, 12)}.md"
            files[path] = description
            raw["description"] = path
        extensions: list[dict[str, Any]] = []
        if isinstance(raw.get("diagram"), dict):
            for element in raw["diagram"].get("elements") or []:
                if isinstance(element, dict):
                    extensions.append({k: element.pop(k) for k in EXTENSION_FIELDS if k in element})
        resolved = resolve_task(raw, files, package, where=f"task {original_id!r}")
        _restore_parameters(resolved, adapted)
        if extensions and resolved["diagram"] is not None:
            _apply_extensions(resolved, extensions, f"task {original_id!r}")
        resolved["taskKey"] = key
        resolved["sourceTaskId"] = original_id
        if addition:
            resolved["guideNotes"] = addition.get("notes", [])
            resolved["guideSources"] = addition.get("sourceFiles", [])
        if resolved["description"] and raw["id"] != original_id:
            resolved["description"]["markdown"] = resolved["description"]["markdown"].replace(
                f"[[param:{original_id}/", f"[[param:{raw['id']}/"
            )
        output.append(resolved)
    for key in sorted(set(additions) - used):
        warnings.append(f"guide overlay task {key!r} matches no documented task; not shown")
    source = listing.get("source") or {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "bundleId": bundle,
        "title": listing["title"],
        "summary": listing.get("methodSummary") or listing.get("summary", ""),
        "source": {
            "package": listing.get("id"),
            "version": listing.get("version"),
            "descriptor": DESCRIPTOR_PATH,
            "commit": source.get("repoCommit") or source.get("commit"),
            "note": GUIDE_NOTE,
        },
        "methods": {
            "path": "docs/methods.md",
            "markdown": docs.get("methods") or docs.get("readme") or "",
        },
        "tasks": output,
        "references": _json_list(docs.get("references"), "docs references"),
        "warnings": warnings,
    }
    resolutions: list[dict[str, Any]] = []
    result["methods"]["markdown"] = PARAM_REFERENCE.sub(
        lambda match: _qualify(match, output, resolutions), result["methods"]["markdown"]
    )
    result["referenceResolutions"] = resolutions
    # Broken cross-references still refuse the document before anyone reads it.
    check_references(result["methods"], None, output, "methods")
    for task in output:
        check_references(task["description"], task, output, task["id"])
    return result


def _qualify(
    match: re.Match[str], tasks: list[dict[str, Any]], resolutions: list[dict[str, Any]]
) -> str:
    """One ``[[param:...]]`` of the methods, made unambiguous where several tasks own it."""
    target = match.group(1)
    name = target
    original_task = None
    if "/" in target:
        original_task, name = target.split("/", 1)
        if resolve_parameter_reference(target, None, tasks) is not None:
            return match.group(0)
    owners = [
        (task, p)
        for task in tasks
        for p in task["parameters"]
        if p["name"] == name and (original_task is None or task["sourceTaskId"] == original_task)
    ]
    if not owners or (len(owners) == 1 and original_task is None):
        return match.group(0)
    first = owners[0][1]
    equal = all(
        same_value(p["default"], first["default"])
        and p["unit"] == first["unit"]
        and p["has_default"] == first["has_default"]
        for _, p in owners
    )
    resolutions.append(
        {"parameter": name, "tasks": [task["id"] for task, _ in owners], "sameValue": equal}
    )
    if equal:
        return f"[[param:{owners[0][0]['id']}/{name}]]"
    return "; ".join(f"{task['title']}: [[param:{task['id']}/{name}]]" for task, _ in owners)
