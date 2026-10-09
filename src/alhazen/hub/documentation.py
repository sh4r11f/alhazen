"""Scientific documentation of an experiment package, and alhazen's own guide.

An experiment package (``alhazen.hub.packages``) may name one documentation
descriptor in its manifest (``"documentation": "docs/experiment.json"``). The
descriptor is author-written JSON: the experiment's methods (a Markdown file
in the package), and per task a parameter reference, a trial timeline and a
stimulus schematic. This module is the only place that knows what that JSON
means. The decision it hides is **how untrusted documentation becomes safe,
bounded, resolved data**:

- only files the manifest declares are read, by exact path, and each one's
  size and SHA-256 are checked against the manifest again;
- nothing is imported, executed or evaluated: a diagram is a small grammar of
  shapes whose numbers are literals or ``{"param": name, "factor": k}``
  (a parameter's documented default times a literal), never an expression;
- every count, string and number is bounded, and non-finite numbers, duplicate
  JSON keys, unknown fields and YAML aliases are refused;
- a parameter whose default can be checked against the package's own params
  file is checked, and a mismatch refuses the documentation: a methods page
  that disagrees with the code it describes is worse than none;
- durations that depend on the subject (wait for fixation, up to a timeout)
  or on a condition stay marked as such, so no renderer can draw them as if
  they were fixed.

``global_guide()`` is the other half: what alhazen's modes and protections
actually are, read from alhazen's own source (the ``Mode`` enum, its flag and
real-data rules, the rehearsal and training roots, the subject kinds, the
frame-QA policies, the calibration choices), plus short authored notes that
each name the module they describe. It needs no network and no hub extras,
so a rig shows it offline.

Interface: ``DocumentationError``, ``read_documentation``, ``global_guide``,
``SCHEMA_VERSION``, ``DESCRIPTOR_SCHEMA``. The descriptor and the resolved
shape are documented in docs/hub/documentation.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DESCRIPTOR_SCHEMA",
    "SCHEMA_VERSION",
    "DocumentationError",
    "global_guide",
    "read_documentation",
]

SCHEMA_VERSION = 1
DESCRIPTOR_SCHEMA = "alhazen-documentation"


class DocumentationError(ValueError):
    """The documentation cannot be used as written. The message names the
    descriptor field or package file at fault (a path inside the package),
    never a path on the machine that read it, so it is safe to show."""


# ---------------------------------------------------------------------------
# Bounds. Generous for a methods section, small enough that a hostile package
# cannot make a reader allocate or draw without limit.
# ---------------------------------------------------------------------------
MAX_DESCRIPTOR_BYTES = 256 * 1024
MAX_MARKDOWN_BYTES = 128 * 1024
MAX_TOTAL_MARKDOWN_BYTES = 512 * 1024
MAX_PARAMS_FILE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 12
MAX_TASKS = 32
MAX_PARAMETERS = 200
MAX_OUTCOMES = 64
MAX_EVENTS = 64
MAX_PHASES = 24
MAX_TRACKS = 16
MAX_BRANCHES = 32
MAX_ELEMENTS = 200
MAX_REFERENCES = 100
MAX_CHOICES = 64
MAX_DOT_COUNT = 400
MAX_GRATING_CYCLES = 40
MAX_ABS_NUMBER = 1e6
MAX_DIAGRAM_EXTENT = 200.0

TEXT_LIMITS = {
    "title": 160,
    "label": 120,
    "short": 400,
    "summary": 1000,
    "meaning": 2000,
    "caption": 1200,
    "reference": 600,
}

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
PARAM_NAME_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,5}$"
)
EVENT_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
UNIT_PATTERN = re.compile(r"^[^\x00-\x1f<>]{1,24}$")
MODEL_PATTERN = re.compile(r"^[A-Za-z_][\w.]{0,199}(:[A-Za-z_]\w{0,99})?$")
# A package path as packages.safe_relative leaves it: relative POSIX, no
# empty, "." or ".." segment, no backslash or drive. Membership in the
# manifest's file list is the real guard; this only keeps the message honest.
PACKAGE_PATH_PATTERN = re.compile(r"^(?!/)(?!.*\\)(?!.*:)[^\x00-\x1f]{1,400}$")
MARKDOWN_REFERENCE = re.compile(r"\[\[([a-z]+):([^\]\s]{1,80})\]\]")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

PARAMETER_TYPES = ("duration", "number", "integer", "boolean", "string", "choice", "object")
TIMING_KINDS = ("fixed", "parameter", "event", "conditional")
BRANCH_KINDS = ("failure", "abort")
TRACK_ROLES = ("stimulus", "requirement", "response", "reward", "annotation")
ELEMENT_ROLES = ("stimulus", "region", "apparatus", "annotation", "target", "distractor", "cue")
TIME_UNITS_MS = {"ms": 1.0, "s": 1000.0}


# ---------------------------------------------------------------------------
# Reading the package
# ---------------------------------------------------------------------------
class _Bundle:
    """The declared files of one package ZIP, read by exact path, each
    re-checked against the manifest's size and SHA-256."""

    def __init__(self, archive: zipfile.ZipFile, declared: dict[str, tuple[int, str]]) -> None:
        self._archive = archive
        self._declared = declared
        self.digests: dict[str, str] = {}

    def has(self, path: str) -> bool:
        return path in self._declared

    def read(self, path: str, limit: int, what: str) -> bytes:
        if path not in self._declared:
            raise DocumentationError(f"{what} {path!r} is not a file this package declares")
        size, digest = self._declared[path]
        if size > limit:
            raise DocumentationError(f"{what} {path!r} is {size} bytes; the limit is {limit}")
        try:
            info = self._archive.getinfo(path)
        except KeyError:
            raise DocumentationError(
                f"{what} {path!r} is declared but missing from the archive"
            ) from None
        if info.is_dir() or info.file_size != size:
            raise DocumentationError(f"{what} {path!r} does not match its declared size")
        try:
            with self._archive.open(info) as handle:
                data = handle.read(limit + 1)
        except (
            zipfile.BadZipFile,
            zlib.error,
            OSError,
            EOFError,
            NotImplementedError,
            RuntimeError,
        ):
            # RuntimeError: an encrypted member; NotImplementedError: an
            # unsupported compression method. Either way, unreadable.
            raise DocumentationError(
                f"{what} {path!r} could not be read from the archive"
            ) from None
        if len(data) != size:
            raise DocumentationError(f"{what} {path!r} does not match its declared size")
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise DocumentationError(f"{what} {path!r} does not match its declared SHA-256")
        self.digests[path] = actual
        return data


def _declared_files(manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise DocumentationError("the manifest has no file list")
    declared: dict[str, tuple[int, str]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise DocumentationError("the manifest's file list has an entry that is not an object")
        path, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
        if (
            not isinstance(path, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise DocumentationError("the manifest's file list has a malformed entry")
        declared[path] = (size, digest)
    return declared


def _package_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not PACKAGE_PATH_PATTERN.match(value):
        raise DocumentationError(f"{where} must be a relative path inside the package")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise DocumentationError(f"{where} must be a normalized relative path inside the package")
    return value


def _decode(data: bytes, path: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DocumentationError(f"{path!r} is not UTF-8 text") from None
    if CONTROL_CHARACTERS.search(text):
        raise DocumentationError(f"{path!r} contains control characters")
    return text.removeprefix("\ufeff")


def _reject_constant(name: str) -> Any:
    raise DocumentationError(f"the descriptor contains {name}, which is not a finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DocumentationError(f"the descriptor repeats the key {key!r}")
        result[key] = value
    return result


def _depth(value: Any, limit: int, level: int = 0) -> None:
    if level > limit:
        raise DocumentationError(f"the descriptor is nested more than {limit} levels deep")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, limit, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, limit, level + 1)


def _parse_json(text: str, path: str) -> Any:
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise DocumentationError(
            f"{path!r} is not valid JSON (line {error.lineno}, column {error.colno})"
        ) from None
    _depth(value, MAX_JSON_DEPTH)
    return value


class _NoAliasLoader(yaml.SafeLoader):
    """yaml.SafeLoader without anchors and aliases: a params file needs
    neither, and aliases are how a small YAML file expands without bound."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise DocumentationError(
                "the params file uses a YAML alias, which is not supported here"
            )
        return super().compose_node(parent, index)


def _parse_params_file(text: str, path: str) -> Any:
    if path.endswith(".json"):
        return _parse_json(text, path)
    if not path.endswith((".yaml", ".yml")):
        raise DocumentationError(f"parameters file {path!r} must be .yaml, .yml or .json")
    try:
        value = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 - safe loader subclass
    except yaml.YAMLError as error:
        raise DocumentationError(
            f"parameters file {path!r} is not valid YAML: {_yaml_where(error)}"
        ) from None
    _depth(value, MAX_JSON_DEPTH)
    _finite_tree(value, f"parameters file {path!r}")
    return value


def _yaml_where(error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        return "unreadable"
    return f"line {mark.line + 1}, column {mark.column + 1}"


def _finite_tree(value: Any, where: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DocumentationError(f"{where} holds a number that is not finite")
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item, where)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item, where)


# ---------------------------------------------------------------------------
# Field checks. One object's fields are taken one by one and any field left
# over is an error, so a typo ("defualt") fails naming itself instead of
# silently documenting nothing.
# ---------------------------------------------------------------------------
class _Fields:
    def __init__(self, value: Any, where: str) -> None:
        if not isinstance(value, dict):
            raise DocumentationError(f"{where} must be an object")
        self._value = value
        self._where = where
        self._taken: set[str] = set()

    def where(self, name: str) -> str:
        return f"{self._where}.{name}"

    def raw(self, name: str) -> Any:
        self._taken.add(name)
        return self._value.get(name)

    def present(self, name: str) -> bool:
        return name in self._value

    def text(self, name: str, limit: str, *, required: bool = False) -> str | None:
        value = self.raw(name)
        if value is None:
            if required:
                raise DocumentationError(f"{self.where(name)} is required")
            return None
        return _text(value, self.where(name), TEXT_LIMITS[limit])

    def ident(self, name: str, pattern: re.Pattern[str], what: str) -> str:
        value = self.raw(name)
        if not isinstance(value, str) or not pattern.match(value):
            raise DocumentationError(f"{self.where(name)} must be {what}")
        return value

    def number(self, name: str, *, required: bool = True, positive: bool = False) -> float | None:
        value = self.raw(name)
        if value is None and not required:
            return None
        return _number(value, self.where(name), positive=positive)

    def boolean(self, name: str, default: bool | None = None) -> bool | None:
        value = self.raw(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise DocumentationError(f"{self.where(name)} must be true or false")
        return value

    def items(self, name: str, limit: int, *, required: bool = False) -> list[Any]:
        value = self.raw(name)
        if value is None:
            if required:
                raise DocumentationError(f"{self.where(name)} is required")
            return []
        if not isinstance(value, list):
            raise DocumentationError(f"{self.where(name)} must be a list")
        if len(value) > limit:
            raise DocumentationError(
                f"{self.where(name)} has {len(value)} entries; the limit is {limit}"
            )
        return value

    def done(self) -> None:
        extra = sorted(set(self._value) - self._taken)
        if extra:
            raise DocumentationError(
                f"{self._where} has unknown field(s): {', '.join(map(repr, extra))}"
            )


def _text(value: Any, where: str, limit: int) -> str:
    if not isinstance(value, str):
        raise DocumentationError(f"{where} must be text")
    if CONTROL_CHARACTERS.search(value):
        raise DocumentationError(f"{where} contains control characters")
    stripped = value.strip()
    if not stripped:
        raise DocumentationError(f"{where} must not be empty")
    if len(stripped) > limit:
        raise DocumentationError(f"{where} is {len(stripped)} characters; the limit is {limit}")
    return stripped


def _number(value: Any, where: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DocumentationError(f"{where} must be a number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_ABS_NUMBER:
        raise DocumentationError(f"{where} must be a finite number within ±{MAX_ABS_NUMBER:g}")
    if positive and number <= 0:
        raise DocumentationError(f"{where} must be greater than zero")
    return number


def _unique(names: Iterable[str], where: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise DocumentationError(f"{where} names {name!r} twice")
        seen.add(name)


# ---------------------------------------------------------------------------
# Values: documented defaults, their text, and the check against the
# package's own params file.
# ---------------------------------------------------------------------------
def _format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


def _format_value(value: Any, unit: str | None) -> str:
    """How a value reads in a table: ``500 ms``, ``0.3 dva``, ``true``.
    hub_docs.js formatValue implements the same rule for the browser."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value) + (f" {unit}" if unit else "")
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"ms"} and _is_number(value["ms"]):
        return f"{_format_number(value['ms'])} ms"
    if isinstance(value, dict) and set(value) == {"frames"} and _is_number(value["frames"]):
        return f"{_format_number(value['frames'])} frames"
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_format_value(item, None)}" for key, item in value.items())
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(item, None) for item in value) + "]"
    if value is None:
        return "none"
    return str(value)


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _same_value(left: Any, right: Any) -> bool:
    """Equal as a params file means it: 2 and 2.0 are the same number, but
    true is not 1 and "2" is not 2."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_value(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_same_value, left, right))
    return type(left) is type(right) and left == right


def _lookup(tree: Any, dotted: str) -> tuple[bool, Any]:
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _leaf_keys(tree: Any, prefix: str = "") -> list[str]:
    if not isinstance(tree, dict) or not tree:
        return [prefix] if prefix else []
    keys: list[str] = []
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        # A Duration ({ms: 500}) is one value, not two keys.
        if isinstance(value, dict) and set(value) in ({"ms"}, {"frames"}):
            keys.append(path)
        else:
            keys.extend(_leaf_keys(value, path))
    return keys


def _duration_ms(value: Any) -> float | None:
    """Milliseconds of a Duration value; None for frames (refresh-dependent)."""
    if isinstance(value, dict) and set(value) == {"ms"}:
        return float(value["ms"])
    return None


def _check_default(value: Any, kind: str, constraints: dict[str, Any], where: str) -> None:
    if kind == "duration":
        ok = (
            isinstance(value, dict)
            and len(value) == 1
            and (
                ("ms" in value and _is_number(value["ms"]) and value["ms"] >= 0)
                or (
                    "frames" in value
                    and isinstance(value["frames"], int)
                    and not isinstance(value["frames"], bool)
                    and value["frames"] >= 0
                )
            )
        )
        if not ok:
            raise DocumentationError(f"{where} must be {{'ms': n}} or {{'frames': n}} (n >= 0)")
        magnitude: float | None = _duration_ms(value)
    elif kind == "number":
        magnitude = _number(value, where)
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise DocumentationError(f"{where} must be a whole number")
        magnitude = _number(value, where)
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise DocumentationError(f"{where} must be true or false")
        magnitude = None
    elif kind == "string":
        _text(value, where, TEXT_LIMITS["short"])
        magnitude = None
    elif kind == "choice":
        magnitude = None
    else:  # object
        if len(json.dumps(value)) > 4000:
            raise DocumentationError(f"{where} is too large to document as one value")
        _finite_tree(value, where)
        magnitude = None
    choices = constraints.get("choices")
    if choices is not None and not any(_same_value(value, choice) for choice in choices):
        raise DocumentationError(f"{where} is not one of the documented choices")
    if magnitude is not None:
        low, high = constraints.get("min"), constraints.get("max")
        if low is not None and magnitude < low:
            raise DocumentationError(f"{where} is below the documented minimum")
        if high is not None and magnitude > high:
            raise DocumentationError(f"{where} is above the documented maximum")


def _constraints(raw: Any, where: str, kind: str) -> dict[str, Any]:
    if raw is None:
        if kind == "choice":
            raise DocumentationError(f"{where} is required for a choice parameter (choices)")
        return {}
    fields = _Fields(raw, where)
    result: dict[str, Any] = {}
    low = fields.number("min", required=False)
    high = fields.number("max", required=False)
    if low is not None:
        result["min"] = low
    if high is not None:
        result["max"] = high
    if low is not None and high is not None and low > high:
        raise DocumentationError(f"{where}: min is greater than max")
    choices = fields.items("choices", MAX_CHOICES)
    for index, choice in enumerate(choices):
        if isinstance(choice, str):
            _text(choice, f"{where}.choices[{index}]", TEXT_LIMITS["label"])
        elif not _is_number(choice) and not isinstance(choice, bool):
            raise DocumentationError(
                f"{where}.choices[{index}] must be text, a number or true/false"
            )
        else:
            _finite_tree(choice, f"{where}.choices[{index}]")
    if choices:
        result["choices"] = list(choices)
    elif kind == "choice":
        raise DocumentationError(f"{where}.choices is required for a choice parameter")
    note = fields.text("note", "short")
    if note:
        result["note"] = note
    fields.done()
    return result


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------
class _Resolver:
    def __init__(self, bundle: _Bundle, manifest: dict[str, Any], pointer: str) -> None:
        self._bundle = bundle
        self._manifest = manifest
        self._pointer = pointer
        self._markdown_bytes = 0
        self._params_files: dict[str, Any] = {}

    # --- package files -----------------------------------------------------
    def _params_file(self, path: str) -> Any:
        if path not in self._params_files:
            data = self._bundle.read(path, MAX_PARAMS_FILE_BYTES, "parameters file")
            self._params_files[path] = _parse_params_file(_decode(data, path), path)
        return self._params_files[path]

    def _markdown(self, value: Any, where: str) -> dict[str, Any] | None:
        if value is None:
            return None
        path = _package_path(value, where)
        if not path.endswith((".md", ".markdown")):
            raise DocumentationError(f"{where} must name a Markdown (.md) file")
        data = self._bundle.read(path, MAX_MARKDOWN_BYTES, "Markdown file")
        self._markdown_bytes += len(data)
        if self._markdown_bytes > MAX_TOTAL_MARKDOWN_BYTES:
            raise DocumentationError(
                f"the documentation's Markdown totals more than {MAX_TOTAL_MARKDOWN_BYTES} bytes"
            )
        return {"path": path, "sha256": self._bundle.digests[path], "markdown": _decode(data, path)}

    # --- top level ---------------------------------------------------------
    def descriptor(self, raw: Any) -> dict[str, Any]:
        fields = _Fields(raw, "documentation")
        schema = fields.raw("schema")
        version = fields.raw("schema_version")
        if schema != DESCRIPTOR_SCHEMA:
            raise DocumentationError(f"documentation.schema must be {DESCRIPTOR_SCHEMA!r}")
        if version != SCHEMA_VERSION or isinstance(version, bool):
            raise DocumentationError(
                f"documentation.schema_version {version!r} is not supported (this reader knows "
                f"{SCHEMA_VERSION})"
            )
        title = fields.text("title", "title") or _text(
            self._manifest.get("title") or self._manifest.get("name") or "Untitled experiment",
            "manifest title",
            TEXT_LIMITS["title"],
        )
        summary = fields.text("summary", "summary")
        methods = self._markdown(fields.raw("methods"), "documentation.methods")
        references = [
            _text(item, f"documentation.references[{index}]", TEXT_LIMITS["reference"])
            for index, item in enumerate(fields.items("references", MAX_REFERENCES))
        ]
        tasks = [
            self._task(item, f"documentation.tasks[{index}]")
            for index, item in enumerate(fields.items("tasks", MAX_TASKS))
        ]
        fields.done()
        _unique((task["id"] for task in tasks), "documentation.tasks")
        if methods is None and not tasks:
            raise DocumentationError("documentation must have methods or at least one task")
        self._check_markdown_references(methods, None, tasks, "documentation.methods")
        for task in tasks:
            self._check_markdown_references(
                task["description"], task, tasks, f"task {task['id']!r}"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "source": {
                "package": self._manifest.get("name"),
                "version": self._manifest.get("version"),
                "descriptor": self._pointer,
                "descriptor_sha256": self._bundle.digests[self._pointer],
            },
            "title": title,
            "summary": summary,
            "references": references,
            "methods": methods,
            "tasks": tasks,
        }

    # --- one task ----------------------------------------------------------
    def _task(self, raw: Any, where: str) -> dict[str, Any]:
        fields = _Fields(raw, where)
        task_id = fields.ident("id", ID_PATTERN, "a task name (letters, digits, '-' or '_')")
        where = f"task {task_id!r}"
        title = fields.text("title", "title") or task_id
        summary = fields.text("summary", "summary")
        description = self._markdown(fields.raw("description"), f"{where}.description")
        params_file = fields.raw("parameters_file")
        if params_file is not None:
            params_file = _package_path(params_file, f"{where}.parameters_file")
            self._params_file(params_file)
        parameters = [
            self._parameter(item, f"{where}.parameters[{index}]", params_file)
            for index, item in enumerate(fields.items("parameters", MAX_PARAMETERS))
        ]
        _unique((p["name"] for p in parameters), f"{where}.parameters")
        by_name = {p["name"]: p for p in parameters}
        outcomes = [
            self._outcome(item, f"{where}.outcomes[{index}]")
            for index, item in enumerate(fields.items("outcomes", MAX_OUTCOMES))
        ]
        _unique((o["name"] for o in outcomes), f"{where}.outcomes")
        events = [
            self._event(item, f"{where}.events[{index}]")
            for index, item in enumerate(fields.items("events", MAX_EVENTS))
        ]
        _unique((e["name"] for e in events), f"{where}.events")
        context = _TaskContext(
            where,
            by_name,
            {o["name"] for o in outcomes} | _reserved_outcomes(),
            {e["name"] for e in events},
        )
        timeline = fields.raw("timeline")
        diagram = fields.raw("diagram")
        fields.done()
        undocumented: list[str] = []
        if params_file is not None:
            documented = {
                p["source"]["key"] for p in parameters if p["source"]["file"] == params_file
            }
            undocumented = [
                key
                for key in _leaf_keys(self._params_file(params_file))
                if key not in documented
                and not any(key.startswith(name + ".") for name in documented)
            ]
        return {
            "id": task_id,
            "title": title,
            "summary": summary,
            "description": description,
            "parameters_file": params_file,
            "parameters": parameters,
            "parameters_undocumented": undocumented,
            "outcomes": outcomes,
            "events": events,
            "timeline": None
            if timeline is None
            else _timeline(timeline, f"{where}.timeline", context),
            "diagram": None if diagram is None else _diagram(diagram, f"{where}.diagram", context),
        }

    def _parameter(self, raw: Any, where: str, params_file: str | None) -> dict[str, Any]:
        fields = _Fields(raw, where)
        name = fields.ident("name", PARAM_NAME_PATTERN, "a parameter name (a dotted key path)")
        where = f"{where} ({name})"
        kind = fields.raw("type")
        if kind not in PARAMETER_TYPES:
            raise DocumentationError(f"{where}.type must be one of {', '.join(PARAMETER_TYPES)}")
        unit = fields.raw("unit")
        if unit is not None and (not isinstance(unit, str) or not UNIT_PATTERN.match(unit)):
            raise DocumentationError(f"{where}.unit must be short text (at most 24 characters)")
        constraints = _constraints(fields.raw("constraints"), f"{where}.constraints", kind)
        has_default = fields.present("default")
        default = fields.raw("default")
        source_raw = fields.raw("source")
        source = self._source(source_raw, f"{where}.source", name, params_file)
        if has_default:
            _check_default(default, kind, constraints, f"{where}.default")
        status = "declared"
        if source["file"] is not None:
            found, value = _lookup(self._params_file(source["file"]), source["key"])
            if not found:
                raise DocumentationError(
                    f"{where}: {source['file']!r} has no key {source['key']!r}"
                )
            if has_default:
                if not _same_value(default, value):
                    raise DocumentationError(
                        f"{where}: the documented default {_format_value(default, unit)} differs "
                        f"from "
                        f"{source['file']!r} ({_format_value(value, unit)}); update the "
                        f"documentation "
                        f"with the code"
                    )
                status = "matched"
            else:
                _check_default(value, kind, constraints, f"{where} (value in {source['file']!r})")
                default, has_default, status = value, True, "read"
        source["status"] = status
        record = {
            "name": name,
            "label": fields.text("label", "label") or name,
            "group": fields.text("group", "label"),
            "type": kind,
            "unit": unit,
            "default": default if has_default else None,
            "has_default": has_default,
            "default_text": _format_value(default, unit) if has_default else None,
            "constraints": constraints,
            "meaning": fields.text("meaning", "meaning", required=True),
            "interactions": fields.text("interactions", "meaning"),
            "source": source,
        }
        fields.done()
        return record

    def _source(self, raw: Any, where: str, name: str, params_file: str | None) -> dict[str, Any]:
        source: dict[str, Any] = {"file": params_file, "key": name, "model": None}
        if raw is None:
            return source
        fields = _Fields(raw, where)
        if fields.present("file"):
            file = fields.raw("file")
            source["file"] = None if file is None else _package_path(file, f"{where}.file")
        if fields.present("key"):
            source["key"] = fields.ident("key", PARAM_NAME_PATTERN, "a dotted key path")
        if fields.present("model"):
            source["model"] = fields.ident(
                "model", MODEL_PATTERN, "'module:Class' or 'module.Class'"
            )
        fields.done()
        return source

    def _outcome(self, raw: Any, where: str) -> dict[str, Any]:
        fields = _Fields(raw, where)
        record = {
            "name": fields.ident("name", EVENT_PATTERN, "an outcome name (UPPER_CASE)"),
            "completed": fields.boolean("completed"),
            "success": fields.boolean("success"),
            "meaning": fields.text("meaning", "meaning", required=True),
        }
        if record["completed"] is None:
            raise DocumentationError(f"{where}.completed is required (does the trial count?)")
        fields.done()
        return record

    def _event(self, raw: Any, where: str) -> dict[str, Any]:
        fields = _Fields(raw, where)
        record = {
            "name": fields.ident("name", EVENT_PATTERN, "an event name (UPPER_CASE)"),
            "meaning": fields.text("meaning", "meaning", required=True),
        }
        fields.done()
        return record

    # --- Markdown references ------------------------------------------------
    @staticmethod
    def _check_markdown_references(
        markdown: dict[str, Any] | None,
        task: dict[str, Any] | None,
        tasks: list[dict[str, Any]],
        where: str,
    ) -> None:
        if markdown is None:
            return
        for kind, target in MARKDOWN_REFERENCE.findall(markdown["markdown"]):
            if kind == "task":
                if not any(t["id"] == target for t in tasks):
                    raise DocumentationError(f"{where} refers to unknown task [[task:{target}]]")
            elif kind == "param":
                if _resolve_parameter_reference(target, task, tasks) is None:
                    raise DocumentationError(
                        f"{where} refers to [[param:{target}]], which names no single documented "
                        f"parameter (write [[param:task-id/name]] to be explicit)"
                    )
            else:
                raise DocumentationError(
                    f"{where} uses [[{kind}:...]]; only param and task are supported"
                )


def _resolve_parameter_reference(
    target: str, task: dict[str, Any] | None, tasks: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """``task-id/name`` names one parameter of one task; a bare ``name`` is the
    current task's, or — in the methods — the one task that has it. The same
    rule is hub_docs.js resolveParameterReference."""
    if "/" in target:
        task_id, _, name = target.partition("/")
        for candidate in tasks:
            if candidate["id"] == task_id and any(
                p["name"] == name for p in candidate["parameters"]
            ):
                return task_id, name
        return None
    if task is not None:
        return (
            (task["id"], target) if any(p["name"] == target for p in task["parameters"]) else None
        )
    owners = [t["id"] for t in tasks if any(p["name"] == target for p in t["parameters"])]
    return (owners[0], target) if len(owners) == 1 else None


def _reserved_outcomes() -> set[str]:
    """Outcomes the engine itself can end a trial with, whatever the task
    declares (alhazen.core.trial)."""
    from alhazen.core.trial import ABORTED, DROPPED_FRAMES, PAUSED

    return {ABORTED.name, DROPPED_FRAMES.name, PAUSED.name}


class _TaskContext:
    def __init__(
        self,
        where: str,
        parameters: dict[str, dict[str, Any]],
        outcomes: set[str],
        events: set[str],
    ) -> None:
        self.where = where
        self.parameters = parameters
        self.outcomes = outcomes
        self.events = events

    def parameter(self, name: Any, where: str) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.parameters:
            raise DocumentationError(f"{where} names {name!r}, which is not a documented parameter")
        return self.parameters[name]

    def outcome(self, name: Any, where: str) -> str:
        if not isinstance(name, str) or name not in self.outcomes:
            raise DocumentationError(
                f"{where} names outcome {name!r}, which this task does not document "
                f"(and the engine does not reserve)"
            )
        return name

    def event(self, name: Any, where: str) -> str:
        if not isinstance(name, str) or name not in self.events:
            raise DocumentationError(
                f"{where} names event {name!r}, which this task does not document"
            )
        return name


# ---------------------------------------------------------------------------
# Timelines. A phase's time is one of four kinds, and only a time that is
# actually known (a literal, or a parameter in ms) is marked ``scaled``: a
# phase that waits on the subject, or happens only sometimes, is never given
# a length it does not have.
# ---------------------------------------------------------------------------
def _parameter_ms(parameter: dict[str, Any], where: str) -> tuple[float | None, str]:
    if not parameter["has_default"]:
        raise DocumentationError(f"{where}: parameter {parameter['name']!r} has no default to time")
    value, unit = parameter["default"], parameter["unit"]
    if parameter["type"] == "duration":
        ms = _duration_ms(value)
        if ms is None:
            return None, f"{_format_value(value, unit)} (depends on the refresh rate)"
        return ms, _format_value(value, unit)
    if parameter["type"] in ("number", "integer") and unit in TIME_UNITS_MS:
        return float(value) * TIME_UNITS_MS[unit], _format_value(value, unit)
    raise DocumentationError(
        f"{where}: parameter {parameter['name']!r} is not a duration (type duration, or a number "
        f"in ms or s)"
    )


def _timing(raw: Any, where: str, context: _TaskContext, *, nested: bool = False) -> dict[str, Any]:
    fields = _Fields(raw, where)
    kind = fields.raw("kind")
    if kind not in TIMING_KINDS or (nested and kind == "conditional"):
        allowed = TIMING_KINDS[:3] if nested else TIMING_KINDS
        raise DocumentationError(f"{where}.kind must be one of {', '.join(allowed)}")
    result: dict[str, Any]
    if kind == "fixed":
        ms = fields.number("ms")
        assert ms is not None
        if ms < 0:
            raise DocumentationError(f"{where}.ms must be >= 0")
        result = {"kind": kind, "ms": ms, "text": _format_value(ms, "ms"), "scaled": True}
    elif kind == "parameter":
        parameter = context.parameter(fields.raw("param"), f"{where}.param")
        ms, text = _parameter_ms(parameter, where)
        result = {
            "kind": kind,
            "param": parameter["name"],
            "param_label": parameter["label"],
            "ms": ms,
            "text": text,
            "scaled": ms is not None,
        }
    elif kind == "event":
        result = {
            "kind": kind,
            "until": fields.text("until", "short", required=True),
            "ms": None,
            "scaled": False,
            "max_ms": None,
            "max_text": None,
            "max_param": None,
        }
        maximum = fields.raw("max")
        if maximum is not None:
            limit = _Fields(maximum, f"{where}.max")
            if limit.present("param"):
                parameter = context.parameter(limit.raw("param"), f"{where}.max.param")
                result["max_ms"], result["max_text"] = _parameter_ms(parameter, f"{where}.max")
                result["max_param"] = parameter["name"]
            else:
                ms = limit.number("ms")
                assert ms is not None
                if ms < 0:
                    raise DocumentationError(f"{where}.max.ms must be >= 0")
                result["max_ms"], result["max_text"] = ms, _format_value(ms, "ms")
            limit.done()
        result["text"] = (
            "until "
            + result["until"]
            + (f", at most {result['max_text']}" if result["max_text"] else "")
        )
    else:
        when = fields.text("when", "short", required=True)
        inner = _timing(fields.raw("timing"), f"{where}.timing", context, nested=True)
        result = {
            "kind": kind,
            "when": when,
            "inner": inner,
            "ms": inner["ms"],
            "scaled": inner["scaled"],
            "text": f"only if {when}: {inner['text']}",
        }
    fields.done()
    return result


def _phase_ids(phases: list[dict[str, Any]]) -> list[str]:
    return [phase["id"] for phase in phases]


def _timeline(raw: Any, where: str, context: _TaskContext) -> dict[str, Any]:
    fields = _Fields(raw, where)
    phases: list[dict[str, Any]] = []
    for index, item in enumerate(fields.items("phases", MAX_PHASES, required=True)):
        at = f"{where}.phases[{index}]"
        phase = _Fields(item, at)
        phase_record = {
            "id": phase.ident("id", ID_PATTERN, "a phase id"),
            "label": phase.text("label", "label", required=True),
            "timing": _timing(phase.raw("timing"), f"{at}.timing", context),
            "note": phase.text("note", "short"),
            "start_events": [
                context.event(e, f"{at}.start_events") for e in phase.items("start_events", 8)
            ],
            "end_events": [
                context.event(e, f"{at}.end_events") for e in phase.items("end_events", 8)
            ],
        }
        phase.done()
        phases.append(phase_record)
    if not phases:
        raise DocumentationError(f"{where}.phases must name at least one phase")
    ids = _phase_ids(phases)
    _unique(ids, f"{where}.phases")

    tracks = []
    for index, item in enumerate(fields.items("tracks", MAX_TRACKS)):
        at = f"{where}.tracks[{index}]"
        track = _Fields(item, at)
        start, end = track.raw("from"), track.raw("to")
        if start not in ids or end not in ids or ids.index(start) > ids.index(end):
            raise DocumentationError(f"{at}: from/to must name phases, in order")
        role = track.raw("role") or "stimulus"
        if role not in TRACK_ROLES:
            raise DocumentationError(f"{at}.role must be one of {', '.join(TRACK_ROLES)}")
        tracks.append(
            {
                "label": track.text("label", "label", required=True),
                "role": role,
                "from": start,
                "to": end,
            }
        )
        track.done()

    branches = []
    for index, item in enumerate(fields.items("branches", MAX_BRANCHES)):
        at = f"{where}.branches[{index}]"
        branch = _Fields(item, at)
        origin = branch.raw("from")
        if origin != "*" and origin not in ids:
            raise DocumentationError(f"{at}.from must name a phase, or '*' for any phase")
        kind = branch.raw("kind")
        if kind not in BRANCH_KINDS:
            raise DocumentationError(f"{at}.kind must be one of {', '.join(BRANCH_KINDS)}")
        branches.append(
            {
                "from": origin,
                "kind": kind,
                "when": branch.text("when", "short", required=True),
                "outcome": context.outcome(branch.raw("outcome"), f"{at}.outcome"),
                "effect": branch.text("effect", "short"),
            }
        )
        branch.done()

    end = None
    if fields.present("end"):
        end_fields = _Fields(fields.raw("end"), f"{where}.end")
        end = {
            "outcome": context.outcome(end_fields.raw("outcome"), f"{where}.end.outcome"),
            "label": end_fields.text("label", "label"),
        }
        end_fields.done()

    between = None
    if fields.present("between_trials"):
        between_fields = _Fields(fields.raw("between_trials"), f"{where}.between_trials")
        between = {
            "label": between_fields.text("label", "label", required=True),
            "timing": _timing(
                between_fields.raw("timing"), f"{where}.between_trials.timing", context
            ),
        }
        between_fields.done()

    record: dict[str, Any] = {
        "caption": fields.text("caption", "caption"),
        "phases": phases,
        "tracks": tracks,
        "branches": branches,
        "end": end,
        "between_trials": between,
    }
    fields.done()
    return record


# ---------------------------------------------------------------------------
# Stimulus schematics: a closed set of shapes in degrees of visual angle,
# origin at the display centre, y up. Each number is a literal or one
# documented parameter's default times a literal factor.
# ---------------------------------------------------------------------------
# type -> (numeric fields, of which positive, default role, accepts luminance)
ELEMENT_TYPES: dict[str, tuple[tuple[str, ...], tuple[str, ...], str, bool]] = {
    "circle": (("cx", "cy", "r"), ("r",), "stimulus", True),
    "ellipse": (("cx", "cy", "rx", "ry"), ("rx", "ry"), "stimulus", True),
    "rect": (("cx", "cy", "width", "height"), ("width", "height"), "stimulus", True),
    "line": (("x1", "y1", "x2", "y2"), (), "annotation", False),
    "arrow": (("x1", "y1", "x2", "y2"), (), "annotation", False),
    "dot_field": (("cx", "cy", "radius", "dot_radius"), ("radius", "dot_radius"), "stimulus", True),
    "grating": (("cx", "cy", "radius", "cycles"), ("radius", "cycles"), "stimulus", False),
    "text": (("x", "y"), (), "annotation", False),
    "dimension": (("x1", "y1", "x2", "y2"), (), "annotation", False),
    "screen": (("cx", "cy", "width", "height"), ("width", "height"), "apparatus", False),
}
COORDINATE_FIELDS = {"cx", "cy", "x", "y", "x1", "y1", "x2", "y2"}


def _quantity(
    raw: Any, where: str, context: _TaskContext, *, positive: bool = False
) -> tuple[float, dict[str, Any] | None]:
    if isinstance(raw, dict):
        fields = _Fields(raw, where)
        parameter = context.parameter(fields.raw("param"), f"{where}.param")
        factor = fields.number("factor", required=False)
        fields.done()
        if parameter["type"] not in ("number", "integer") or not parameter["has_default"]:
            raise DocumentationError(
                f"{where}: parameter {parameter['name']!r} must be a number with a default to be "
                f"drawn"
            )
        value = float(parameter["default"]) * (1.0 if factor is None else factor)
        reference: dict[str, Any] | None = {
            "param": parameter["name"],
            "factor": 1.0 if factor is None else factor,
        }
    else:
        value, reference = _number(raw, where), None
    if not math.isfinite(value) or abs(value) > MAX_DIAGRAM_EXTENT * 10:
        raise DocumentationError(f"{where} resolves outside the drawable range")
    if positive and value <= 0:
        raise DocumentationError(f"{where} must be greater than zero")
    return value, reference


def _diagram(raw: Any, where: str, context: _TaskContext) -> dict[str, Any]:
    fields = _Fields(raw, where)
    unit = fields.raw("unit") or "dva"
    if not isinstance(unit, str) or not UNIT_PATTERN.match(unit):
        raise DocumentationError(f"{where}.unit must be short text")
    width = fields.number("width", positive=True)
    height = fields.number("height", positive=True)
    assert width is not None and height is not None
    if width > MAX_DIAGRAM_EXTENT or height > MAX_DIAGRAM_EXTENT:
        raise DocumentationError(f"{where}: width and height are at most {MAX_DIAGRAM_EXTENT:g}")
    background = fields.number("background", required=False)
    if background is not None and not 0 <= background <= 1:
        raise DocumentationError(f"{where}.background is a luminance between 0 and 1")
    elements = [
        _element(item, f"{where}.elements[{index}]", context, unit)
        for index, item in enumerate(fields.items("elements", MAX_ELEMENTS, required=True))
    ]
    if not elements:
        raise DocumentationError(f"{where}.elements must hold at least one shape")
    record = {
        "caption": fields.text("caption", "caption", required=True),
        "unit": unit,
        "width": width,
        "height": height,
        "background": background,
        "elements": elements,
    }
    fields.done()
    return record


def _element(raw: Any, where: str, context: _TaskContext, unit: str) -> dict[str, Any]:
    fields = _Fields(raw, where)
    kind = fields.raw("type")
    if kind not in ELEMENT_TYPES:
        raise DocumentationError(f"{where}.type must be one of {', '.join(ELEMENT_TYPES)}")
    numeric, positive, default_role, luminous = ELEMENT_TYPES[kind]
    record: dict[str, Any] = {"type": kind}
    references: dict[str, Any] = {}
    for name in numeric:
        value, reference = _quantity(
            fields.raw(name), f"{where}.{name}", context, positive=name in positive
        )
        if name in COORDINATE_FIELDS and abs(value) > MAX_DIAGRAM_EXTENT:
            raise DocumentationError(f"{where}.{name} lies outside the drawable range")
        record[name] = value
        if reference is not None:
            references[name] = reference
    if kind in ("ellipse", "rect", "grating"):
        rotation = fields.number("rotation" if kind != "grating" else "orientation", required=False)
        record["rotation" if kind != "grating" else "orientation"] = rotation or 0.0
    if kind == "dot_field":
        count, seed = fields.raw("count"), fields.raw("seed")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_DOT_COUNT:
            raise DocumentationError(
                f"{where}.count must be a whole number from 1 to {MAX_DOT_COUNT}"
            )
        if seed is None:
            seed = 1
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            raise DocumentationError(f"{where}.seed must be a whole number from 0 to 2^31 - 1")
        record["count"], record["seed"] = count, seed
    if kind == "grating":
        if record["cycles"] > MAX_GRATING_CYCLES:
            raise DocumentationError(f"{where}.cycles is at most {MAX_GRATING_CYCLES}")
        contrast = fields.number("contrast", required=False)
        if contrast is not None and not 0 <= contrast <= 1:
            raise DocumentationError(f"{where}.contrast is between 0 and 1")
        record["contrast"] = 1.0 if contrast is None else contrast
    if kind == "text":
        record["text"] = fields.text("text", "label", required=True)
    if kind == "dimension":
        record["text"] = fields.text("text", "label")
        record["value"] = None
        record["value_text"] = None
        if fields.present("value"):
            value, reference = _quantity(fields.raw("value"), f"{where}.value", context)
            record["value"] = value
            record["value_text"] = _format_value(value, unit)
            if reference is not None:
                references["value"] = reference
    role = fields.raw("role") or default_role
    if role not in ELEMENT_ROLES:
        raise DocumentationError(f"{where}.role must be one of {', '.join(ELEMENT_ROLES)}")
    record["role"] = role
    record["label"] = fields.text("label", "label")
    luminance = fields.number("luminance", required=False)
    if luminance is not None and (not luminous or not 0 <= luminance <= 1):
        raise DocumentationError(f"{where}.luminance is a 0-1 fill for filled stimulus shapes only")
    record["luminance"] = luminance
    record["dashed"] = bool(fields.boolean("dashed", False))
    record["refs"] = references
    fields.done()
    return record


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def read_documentation(bundle_path: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The package's documentation, validated and resolved — or None when its
    manifest declares none (a legacy package; say so, do not invent any).

    ``manifest`` is the package's ``alhazen-package.json`` as already parsed;
    only the files it declares are read, by exact path, and each is checked
    against its declared size and SHA-256. Raises DocumentationError for
    anything that cannot be used as written.
    """
    if not isinstance(manifest, dict):
        raise DocumentationError("the manifest must be an object")
    pointer = manifest.get("documentation")
    if pointer is None:
        return None
    pointer = _package_path(pointer, "the manifest's documentation field")
    if not pointer.endswith(".json"):
        raise DocumentationError("the manifest's documentation field must name a .json descriptor")
    declared = _declared_files(manifest)
    try:
        archive = zipfile.ZipFile(bundle_path)
    except (zipfile.BadZipFile, OSError):
        raise DocumentationError("the package archive could not be opened as a ZIP") from None
    with archive:
        bundle = _Bundle(archive, declared)
        descriptor = _parse_json(
            _decode(
                bundle.read(pointer, MAX_DESCRIPTOR_BYTES, "documentation descriptor"), pointer
            ),
            pointer,
        )
        return _Resolver(bundle, manifest, pointer).descriptor(descriptor)


# ---------------------------------------------------------------------------
# The global guide
# ---------------------------------------------------------------------------
GUIDE_SCHEMA_VERSION = 1

# What each mode leaves behind, for the three that write no session folder.
# Taken from each mode's own module docstring (alhazen.modes.measure, .demo,
# .movie); the four that run trials have their destination derived below.
_NON_SESSION_OUTPUT = {
    "measure": "a measurement report, printed and written next to the rig file",
    "demo": "nothing: no trials and no data",
    "movie": "movie files and a contact sheet in the folder given with --out",
}


def _mode_records() -> list[dict[str, Any]]:
    from types import SimpleNamespace

    from alhazen.modes import MODE_SUMMARIES, Mode, flag_refusal, real_data_refusal
    from alhazen.modes.rehearsal import REHEARSAL_SUFFIX
    from alhazen.training.ladder import TRAINING_SUFFIX

    development_rig: Any = SimpleNamespace(real_data=False)
    records = []
    for mode in Mode:
        if mode.writes_real_data:
            data = "the rig's data root"
        elif mode is Mode.TRAINING:
            data = (
                f"<data root>{TRAINING_SUFFIX}/<ladder>/<stage>/, never the experiment's own root"
            )
        elif mode.runs_trials:
            data = f"<data root>{REHEARSAL_SUFFIX}, a sibling the analysis never reads as subjects"
        else:
            data = _NON_SESSION_OUTPUT[mode.value]
        records.append(
            {
                "id": mode.value,
                "summary": MODE_SUMMARIES[mode],
                "runs_trials": mode.runs_trials,
                "drives_subject": mode.drives_subject,
                "writes_real_data": mode.writes_real_data,
                "refuses_development_rig": real_data_refusal(mode, development_rig) is not None,
                "accepts": {
                    "headless": flag_refusal(mode, headless=True) is None,
                    "mouse": flag_refusal(mode, mouse=True) is None,
                    "calibration_target": flag_refusal(mode, calibration=True) is None,
                },
                "data": data,
                "source": "alhazen.modes:Mode",
            }
        )
    return records


def _literal_choices(model: Any, field: str) -> list[str]:
    from typing import get_args

    return [str(choice) for choice in get_args(model.model_fields[field].annotation)]


def _guide_sections() -> list[dict[str, Any]]:
    from alhazen.config.models import (
        CALIBRATION_APPEARANCES,
        CALIBRATION_MOTIONS,
        FrameQAConfig,
    )
    from alhazen.core.trial import ABORTED, DROPPED_FRAMES
    from alhazen.paradigms.config import SchedulerConfig
    from alhazen.task.subject_kind import SubjectKind

    def item(
        item_id: str, title: str, text: str, sources: list[str], values: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "id": item_id,
            "title": title,
            "text": text,
            "sources": sources,
            "values": values or [],
        }

    return [
        {
            "id": "timing",
            "title": "Timing",
            "items": [
                item(
                    "frames",
                    "Phases last whole frames",
                    "A phase of d seconds is on screen for d to the nearest display frame. The "
                    "frame "
                    "its time runs out on is drawn by the next phase, so nothing stays up a frame "
                    "late.",
                    ["alhazen.core.engine:TrialEngine"],
                ),
                item(
                    "flip-stamps",
                    "Events carry the flip's time",
                    "A visual event is stamped with the time of the screen flip that showed it, "
                    "not "
                    "the moment the code asked for it; device messages and sync pulses follow "
                    "that flip.",
                    ["alhazen.core.engine:TrialEngine"],
                ),
                item(
                    "durations",
                    "Durations in milliseconds or frames",
                    "A Duration is given in ms or in display frames and is resolved once, against "
                    "the "
                    "measured refresh rate, when the session is built.",
                    ["alhazen.config.models:Duration"],
                ),
                item(
                    "refresh",
                    "The refresh rate is measured and checked",
                    "Frame arithmetic uses the measured refresh rate after checking it agrees "
                    "with the "
                    "rig file's rate within its tolerance; a disagreement stops the session "
                    "instead of "
                    "silently changing every frame-based duration.",
                    ["alhazen.config.models:resolve_refresh"],
                ),
                item(
                    "frame-qa",
                    "Dropped frames are counted",
                    "Every frame interval is logged. The rig's frame-QA policy decides what a "
                    "trial with "
                    f"dropped frames does; under recycle_trial it ends as {DROPPED_FRAMES.name} "
                    f"and its "
                    "condition is served again.",
                    ["alhazen.config.models:FrameQAConfig"],
                    _literal_choices(FrameQAConfig, "policy"),
                ),
            ],
        },
        {
            "id": "hardware",
            "title": "Rigs and hardware",
            "items": [
                item(
                    "real-data",
                    "Development rigs never collect real data",
                    "Run and training modes refuse a rig whose file says real_data: false before "
                    "anything is opened or written. Every other mode may rehearse on it.",
                    ["alhazen.modes:real_data_refusal"],
                ),
                item(
                    "flags",
                    "Flags belong to one mode",
                    "--headless is honoured only by simulate, --mouse only by test; any other mode "
                    "refuses the flag by name rather than ignoring it.",
                    ["alhazen.modes:flag_refusal"],
                ),
                item(
                    "device-faults",
                    "A failed device aborts the trial",
                    f"If a device stops while the measurement is being made, the trial ends as "
                    f"{ABORTED.name} with the fault on its row and its condition is served again; "
                    "during the closing phase the fault is only flagged.",
                    ["alhazen.core.engine:TrialEngine"],
                ),
                item(
                    "measure",
                    "Measure the rig itself",
                    "Measure mode checks the display, the response keys and the eye tracker "
                    "through "
                    "the same code a session uses, and reports what it measured.",
                    ["alhazen.modes.measure"],
                ),
            ],
        },
        {
            "id": "reward",
            "title": "Subjects and reward",
            "items": [
                item(
                    "subject-kind",
                    "The params file declares the subject",
                    "A human session opens no reward line, whatever the rig has. A monkey session "
                    "requires a reward block and pays only the outcomes it names. A file that "
                    "declares "
                    "neither keeps the behaviour of versions before 2.12.",
                    ["alhazen.task.subject_kind:SubjectParams"],
                    [kind.value for kind in SubjectKind],
                ),
                item(
                    "training",
                    "Training pays one stage's success",
                    "Training mode runs the stage the operator chooses, pays only that stage's "
                    "success "
                    "(and the device-fault reward), refuses a human subject, and never advances a "
                    "subject by itself: criteria are recommendations.",
                    ["alhazen.training.ladder:resolve_stage"],
                ),
            ],
        },
        {
            "id": "calibration",
            "title": "Eye-tracker calibration",
            "items": [
                item(
                    "startup",
                    "Calibrate before trial 1",
                    "With a real tracker the session asks for a calibration before the first "
                    "trial. "
                    "Calibrating is the default; reusing the previous calibration is a separate "
                    "key, "
                    "offered only when this rig's own record says it fits. Stand-ins (mouse, "
                    "simulation) are never asked.",
                    ["alhazen.session.startup_calibration:StartupCalibration"],
                ),
                item(
                    "targets",
                    "Calibration targets",
                    "Run, test and training can choose what the tracker draws: the standard "
                    "target, "
                    "chosen pictures or random pictures, still or pulsating.",
                    ["alhazen.config.models:CalibrationTargetConfig", "alhazen.modes:flag_refusal"],
                    [*CALIBRATION_APPEARANCES, *CALIBRATION_MOTIONS],
                ),
            ],
        },
        {
            "id": "designs",
            "title": "Trial schedules",
            "items": [
                item(
                    "schedulers",
                    "How conditions are served",
                    "A task's paradigm block chooses the scheduler. A trial that did not complete "
                    "(a fixation break, an abort) is served again rather than counted.",
                    ["alhazen.paradigms.config:SchedulerConfig"],
                    _literal_choices(SchedulerConfig, "kind"),
                ),
                item(
                    "estimate",
                    "Session length before launch",
                    "The workspace estimates a session's length from the effective parameters, "
                    "mode "
                    "and rig, and says what it cannot know (waits on the subject, adaptive "
                    "designs).",
                    ["alhazen.modes.estimate:estimate_trials"],
                ),
            ],
        },
        {
            "id": "documentation",
            "title": "Experiment documentation",
            "items": [
                item(
                    "methods",
                    "Methods travel with the code",
                    "An experiment's methods, parameter reference, timelines and stimulus "
                    "schematics "
                    "are written by its authors and versioned with its package. Defaults are "
                    "checked "
                    "against the package's params file when it is uploaded.",
                    ["alhazen.hub.documentation:read_documentation"],
                ),
                item(
                    "figures",
                    "Figures explain; they do not measure",
                    "Timelines draw only known durations to scale; phases that wait on the "
                    "subject or "
                    "happen only sometimes are marked as such. Schematics show the design in "
                    "degrees "
                    "of visual angle, not a measurement of your rig.",
                    ["alhazen.hub.documentation:read_documentation"],
                ),
                item(
                    "missing",
                    "Missing documentation is said plainly",
                    "A package without documentation is shown as undocumented. Read its source and "
                    "parameter files before running it.",
                    ["alhazen.hub.documentation:read_documentation"],
                ),
            ],
        },
    ]


def global_guide() -> dict[str, Any]:
    """What alhazen's modes and protections are, from alhazen's own source.

    Deterministic and offline: the same alhazen version gives the same guide.
    """
    from alhazen.version import __version__

    modes = _mode_records()
    return {
        "schema_version": GUIDE_SCHEMA_VERSION,
        "title": "Alhazen guide",
        "alhazen_version": __version__,
        "intro": (
            f"Every experiment starts in one of {len(modes)} modes. Simulate, test and run share "
            f"one "
            "code path and differ only in trial counts, who supplies the gaze and keys, and where "
            "the data goes."
        ),
        "modes": modes,
        "sections": _guide_sections(),
    }
