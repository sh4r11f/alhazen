"""Parameter-set guides: what each named configuration of an experiment actually sets.

For one experiment at one source version, describe each named parameter set (``run.py``
``PARAMETERS``) with the ACTUAL values of its YAML file, what the documentation says each
value means, whether a replication marks it ASSUMED, and how it differs from another set
of the same task. Training ladders (``run.py`` ``LADDERS``) are described as separate
guides, and rig files are listed apart.

The decision it hides is **how experiment source is read without running it**: ``run.py``
and the task modules are read with ``ast``; YAML through PyYAML's SafeLoader node graph (so
comments and line numbers stay attached to keys). Unsupported constructs (YAML merge keys,
anchors and aliases, python tags, non-static paths in run.py, non-literal defaults) are
rejected or left unresolved with a warning, never guessed at. Nothing here imports or
executes experiment code, reads git or touches the network: callers pass files in.

Defaults an experiment inherits from alhazen (``SubjectParams``, ``SchedulerConfig``) are
listed when the caller passes alhazen's own source at the experiment's pinned version:
``library_files(tree)`` from a ``SourceTree`` of the alhazen repository at that tag, or
``installed_library()`` when the installed alhazen is that version (``pin_matches``).
Without it the guide still builds and says what it could not resolve.

This is the Mana Experiment Hub's extractor (``enrich_parameters.py``) with its output
unchanged, minus its git and Mana shell, and with YAML aliases refused rather than
expanded (an alias can make a small file expand without bound).

Interface: ``build_parameter_guide``, ``extract_experiment``, ``alhazen_pin``,
``run_fields_from_alhazen_source``, ``run_fields_from_library``, ``library_files``,
``installed_library``, ``pin_matches``, ``parse_yaml``, ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

from alhazen.experiment_platform.errors import SourceError

SCHEMA_VERSION = 1
DURATION_KEYS = {"ms", "s", "frames", "us", "min"}
SECTION_RE = re.compile(r"^#\s*-{2,}\s*(.*?)\s*-*\s*$")
UNIT_SUFFIXES = (  # naming convention of alhazen params; reported with unitSource="key suffix"
    ("_dva_s", "deg/s"),
    ("_deg_s", "deg/s"),
    ("_dva", "dva"),
    ("_deg", "deg"),
    ("_pct", "%"),
    ("_hz", "Hz"),
    ("_ms", "ms"),
    ("_cd_m2", "cd/m²"),
    ("_cm", "cm"),
    ("_ua", "µA"),
    ("_px", "px"),
    ("_s", "s"),
)


# ----------------------------------------------------------------------------- small utils


def _text(blob: str | bytes) -> str:
    if isinstance(blob, bytes):
        return blob.decode("utf-8-sig")
    return blob[1:] if blob.startswith("\ufeff") else blob


def git_blob_sha(blob: str | bytes) -> str:
    raw = blob.encode("utf-8") if isinstance(blob, str) else blob
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s or "set"


def _json_safe(v: Any, where: str, warnings: list[str]) -> Any:
    """YAML values as JSON: structures stay structures; non-JSON scalars become strings, loudly."""
    if isinstance(v, dict):
        return {str(k): _json_safe(x, where, warnings) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x, where, warnings) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        warnings.append(f"{where}: non-finite number {v!r} kept as text")
        return repr(v)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    warnings.append(f"{where}: {type(v).__name__} value kept as text")
    return str(v)


def is_duration(v: Any) -> bool:
    return (
        isinstance(v, dict)
        and len(v) == 1
        and next(iter(v)) in DURATION_KEYS
        and isinstance(next(iter(v.values())), (int, float))
        and not isinstance(next(iter(v.values())), bool)
    )


def display(v: Any) -> str:
    """A compact, human-readable rendering of any value (structures as compact JSON)."""
    if is_duration(v):
        k, x = next(iter(v.items()))
        return f"{x:g} {k}" if isinstance(x, float) else f"{x} {k}"
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float, str)):
        return str(v)
    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


def canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def unit_from_key(key: str) -> str | None:
    leaf = key.rsplit(".", 1)[-1]
    for suf, unit in UNIT_SUFFIXES:
        if leaf.endswith(suf):
            return unit
    return None


# ----------------------------------------------------------------------------- YAML with comments


@dataclass
class KeyNote:
    path: tuple  # mapping keys (str) and sequence indices (int)
    value: Any
    line: int  # 1-based line of the key
    inline: str | None  # comment on the key's line
    lead: str | None  # contiguous comment block directly above the key
    section: str | None  # nearest '# --- title ---' header above (top-level keys)
    flow_parent: bool = False


@dataclass
class ParsedYaml:
    data: Any
    notes: list[KeyNote]
    header: str | None
    warnings: list[str] = field(default_factory=list)


class _AliasRefused(yaml.YAMLError):
    pass


class _NoAliasLoader(yaml.SafeLoader):
    """yaml.SafeLoader without aliases: a params file needs none, and aliases are how a
    small YAML file expands without bound. An anchor alone is harmless and allowed."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise _AliasRefused(f"YAML alias at line {event.start_mark.line + 1} is not supported")
        return super().compose_node(parent, index)


def _load_yaml(text: str) -> Any:
    """``yaml.safe_load`` without aliases."""
    loader = _NoAliasLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def parse_yaml(text: str, source: str) -> ParsedYaml:
    """Parse one YAML file the way alhazen does (``yaml.safe_load``), keeping every mapping
    key's line, inline comment, leading comment block and section header.

    Rejected (SourceError): anything SafeLoader rejects (python tags), anchors and aliases,
    merge keys (``<<``). Duplicate keys follow safe_load (last wins) and warn."""
    warnings: list[str] = []
    try:
        loader = _NoAliasLoader(text)
        try:
            root = loader.get_single_node()
        finally:
            loader.dispose()
        data = None if root is None else _NoAliasLoader("").construct_document(root)
    except _AliasRefused as exc:
        raise SourceError(f"{source}: {exc}") from None
    except yaml.YAMLError as exc:
        raise SourceError(f"{source}: invalid YAML: {exc}") from exc

    def reject_merge(node, seen=None):
        """Before anything is constructed: constructing a mapping flattens '<<' away in place."""
        seen = seen if seen is not None else set()
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if k.tag == "tag:yaml.org,2002:merge":
                    raise SourceError(
                        f"{source}: YAML merge key (<<) at line {k.start_mark.line + 1} "
                        "is not supported"
                    )
                reject_merge(v, seen)
        elif isinstance(node, yaml.SequenceNode):
            for v in node.value:
                reject_merge(v, seen)

    if root is not None:
        reject_merge(root)
    lines = text.splitlines()
    constructor = yaml.SafeLoader("")

    def build(node):
        return constructor.construct_object(node, deep=True)

    header_lines: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            header_lines.append(s.lstrip("#").strip() if not s.startswith("#!") else s)
        else:
            break
    header_end = len(header_lines)  # header = comment lines at the very top
    header = "\n".join(header_lines).strip() or None

    def comment_after(line_no: int, col: int) -> str | None:
        if line_no >= len(lines):
            return None
        rest = lines[line_no][col:]
        rest = rest.lstrip()
        if rest.startswith(":"):
            rest = rest[1:].lstrip()
        return rest[1:].strip() if rest.startswith("#") else None

    def lead_above(line_no: int) -> str | None:
        out: list[str] = []
        i = line_no - 1
        while i >= 0:
            s = lines[i].strip()
            if not s.startswith("#") or SECTION_RE.match(s):
                break
            out.insert(0, s.lstrip("#").strip())
            i -= 1
        if i < 0 and header_end and line_no - len(out) <= header_end:
            return None  # reaches the top of the file: the file header
        txt = "\n".join(out).strip()
        return txt or None

    def section_above(line_no: int) -> str | None:
        for i in range(line_no - 1, -1, -1):
            m = SECTION_RE.match(lines[i].strip())
            if m and m.group(1):
                return m.group(1)
        return None

    notes: list[KeyNote] = []

    def walk(node, path: tuple, section: str | None, flow: bool, parent_inline: str | None):
        if isinstance(node, yaml.MappingNode):
            if any(k.tag == "tag:yaml.org,2002:merge" for k, _ in node.value):
                raise SourceError(
                    f"{source}: YAML merge key under {path_str(path) or '<root>'} is not supported"
                )
            if any(not isinstance(build(k), str) for k, _ in node.value):
                return  # e.g. {4: ..., 16: ...}: one structured value, keys kept
            seen: dict[str, int] = {}
            for knode, vnode in node.value:
                key = build(knode)
                if key in seen:
                    warnings.append(
                        f"{source}: key {path_str(path + (key,))} repeated (lines {seen[key]} and "
                        f"{knode.start_mark.line + 1}); the last one is used, as safe_load does"
                    )
                seen[key] = knode.start_mark.line + 1
                kline = knode.start_mark.line
                if flow:
                    inline = parent_inline
                elif isinstance(vnode, (yaml.ScalarNode,)) or getattr(vnode, "flow_style", False):
                    inline = comment_after(vnode.end_mark.line, vnode.end_mark.column)
                else:
                    inline = comment_after(kline, knode.end_mark.column)
                sec = section_above(kline) if not path else section
                note = KeyNote(
                    path + (key,),
                    build(vnode),
                    kline + 1,
                    inline,
                    None if flow else lead_above(kline),
                    sec,
                    flow,
                )
                notes.append(note)
                child_flow = flow or bool(getattr(vnode, "flow_style", False))
                walk(vnode, path + (key,), sec, child_flow, inline if child_flow else None)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                walk(item, path + (i,), section, flow or bool(node.flow_style), parent_inline)

    if root is not None:
        walk(root, (), None, False, None)
    return ParsedYaml(data, notes, header, warnings)


def path_str(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + p)
    return out


def first_paragraph(text: str | None) -> str | None:
    if not text:
        return None
    para = text.strip().split("\n\n", 1)[0]
    return re.sub(r"\s+", " ", para).strip() or None


def classify_comment(*texts: str | None) -> dict:
    """Provenance markers the replications write in their YAML comments."""
    out: dict[str, Any] = {}
    for t in texts:
        if not t:
            continue
        if "assumed" not in out:
            if re.search(r"\bASSUMED\b", t):
                out["assumed"] = True
            elif re.search(r"(^|[\s(;])paper\s*:", t):
                out["assumed"] = False
        if re.search(r"(^|[\s(;])deviation\b", t, re.I):
            out["deviation"] = True
    return out


# ----------------------------------------------------------------------------- run.py (AST)


def _module_assignment(tree: ast.Module, name: str) -> ast.expr | None:
    """The value of a module-level ``name = ...`` / ``name: T = ...`` (last one wins)."""
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                found = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            found = node.value
    return found


def _params_path(tree: ast.Module, node: ast.expr) -> str | None:
    """Mirror of alhazen.cli.workspace._params_path: a string, or ``NAME / "a" / "b"`` where
    NAME is a module-level name bound from ``__file__``; anything else is None (not guessed)."""
    parts: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            return None
        parts.insert(0, node.right.value)
        node = node.left
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and not parts:
        text = node.value
    elif isinstance(node, ast.Name) and parts:
        bound = _module_assignment(tree, node.id)
        if bound is None or not any(
            isinstance(i, ast.Name) and i.id == "__file__" for i in ast.walk(bound)
        ):
            return None
        text = "/".join(parts)
    else:
        return None
    return PureWindowsPath(text).as_posix()


@dataclass
class Registry:
    tasks: dict[str, dict]  # name -> {class, config}
    sets: list[dict]  # {label, task, config}
    ladders: list[dict]  # {label, file}
    imports: dict[str, tuple]  # local class name -> (module, level, original name)
    warnings: list[str]


def read_registry(run_py: str) -> Registry:
    """TASKS, PARAMETERS and LADDERS from run.py, statically, with alhazen's own rules."""
    tree = ast.parse(run_py)
    warnings: list[str] = []
    imports: dict[str, tuple] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                imports[a.asname or a.name] = (node.module or "", node.level, a.name)
    tasks: dict[str, dict] = {}
    tnode = _module_assignment(tree, "TASKS")
    if isinstance(tnode, ast.Dict):
        for k, v in zip(tnode.keys, tnode.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                warnings.append("run.py TASKS has a non-literal key; entry skipped")
                continue
            cls, cfg = None, None
            if isinstance(v, (ast.Tuple, ast.List)) and v.elts:
                if isinstance(v.elts[0], ast.Name):
                    cls = v.elts[0].id
                else:
                    warnings.append(f"run.py TASKS[{k.value!r}]: task class is not a plain name")
                if len(v.elts) >= 2:
                    e = v.elts[1]
                    if isinstance(e, ast.Constant) and e.value is None:
                        cfg = None
                    else:
                        cfg = _params_path(tree, e)
                        if cfg is None:
                            warnings.append(
                                f"run.py TASKS[{k.value!r}]: "
                                "default params path is not a static path"
                            )
            else:
                warnings.append(f"run.py TASKS[{k.value!r}] is not a (class, params) tuple")
            tasks[k.value] = {"class": cls, "config": cfg}
    elif tnode is not None:
        warnings.append("run.py TASKS is not a dict literal; tasks not read")
    sets: list[dict] = []
    pnode = _module_assignment(tree, "PARAMETERS")
    if isinstance(pnode, ast.Dict):
        for k, v in zip(pnode.keys, pnode.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value.strip()):
                warnings.append("run.py PARAMETERS has a non-literal label; entry skipped")
                continue
            label = k.value.strip()
            if any(s["label"] == label for s in sets):
                warnings.append(f"run.py PARAMETERS names {label!r} twice; the first is kept")
                continue
            task = None
            if tasks:
                if not (isinstance(v, (ast.Tuple, ast.List)) and len(v.elts) == 2):
                    warnings.append(f"run.py PARAMETERS[{label!r}] is not (task, params); skipped")
                    continue
                first, v = v.elts
                if not (isinstance(first, ast.Constant) and first.value in tasks):
                    warnings.append(
                        f"run.py PARAMETERS[{label!r}] names a task that is not in TASKS; skipped"
                    )
                    continue
                task = first.value
            if isinstance(v, ast.Constant) and v.value is None:
                cfg = None
            else:
                cfg = _params_path(tree, v)
                if cfg is None:
                    warnings.append(
                        f"run.py PARAMETERS[{label!r}]: params path is not a static path; skipped"
                    )
                    continue
            sets.append({"label": label, "task": task, "config": cfg})
    elif pnode is not None:
        warnings.append("run.py PARAMETERS is not a dict literal; no sets read")
    ladders: list[dict] = []
    lnode = _module_assignment(tree, "LADDERS")
    if isinstance(lnode, ast.Dict):
        for k, v in zip(lnode.keys, lnode.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                warnings.append("run.py LADDERS has a non-literal label; skipped")
                continue
            f = _params_path(tree, v)
            if f is None:
                warnings.append(f"run.py LADDERS[{k.value!r}]: file is not a static path; skipped")
                continue
            ladders.append({"label": k.value.strip(), "file": f})
    elif lnode is not None:
        warnings.append("run.py LADDERS is not a dict literal; ladders not read")
    return Registry(tasks, sets, ladders, imports, warnings)


# ----------------------------------------------------------------------------- task modules (AST)

UNRESOLVED = object()


def _module_file(files: Mapping[str, Any], module: str) -> str | None:
    rel = module.replace(".", "/")
    for cand in (f"src/{rel}.py", f"src/{rel}/__init__.py", f"{rel}.py", f"{rel}/__init__.py"):
        if cand in files:
            return cand
    return None


def _resolve_relative(current_file: str, module: str, level: int) -> str:
    if not level:
        return module
    parts = PurePosixPath(current_file).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    pkg = list(parts[:-1])  # the package containing the file (or __init__)
    if level > 1:
        pkg = pkg[: max(0, len(pkg) - (level - 1))]
    return ".".join(pkg + ([module] if module else []))


class ModuleIndex:
    """Find classes across the project's own Python files, then the pinned library's
    (alhazen) source, by following ``from x import Y``. Nothing is imported or run."""

    def __init__(self, files: Mapping[str, Any], library: Mapping[str, Any] | None = None):
        self.files = files
        self.library = library or {}
        self._trees: dict[str, ast.Module | None] = {}

    def _blob(self, path: str) -> Any:
        if path.startswith("lib:"):
            return self.library.get(path[4:])
        return self.files.get(path)

    def tree(self, path: str) -> ast.Module | None:
        if path not in self._trees:
            blob = self._blob(path)
            try:
                self._trees[path] = ast.parse(_text(blob)) if blob is not None else None
            except (SyntaxError, UnicodeDecodeError):
                self._trees[path] = None
        return self._trees[path]

    def module_path(self, module: str) -> str | None:
        own = _module_file(self.files, module)
        if own:
            return own
        lib = _module_file(self.library, module)
        return f"lib:{lib}" if lib else None

    def find_class(self, module: str, name: str, depth: int = 0) -> tuple[str, ast.ClassDef] | None:
        path = self.module_path(module)
        if path is None or depth > 6:
            return None
        return self.find_in_file(path, name, depth)

    def find_in_file(self, path: str, name: str, depth: int = 0) -> tuple[str, ast.ClassDef] | None:
        tree = self.tree(path)
        if tree is None or depth > 6:
            return None
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return path, node
        for imported in ast.walk(tree):  # module-level and TYPE_CHECKING-guarded imports
            if isinstance(imported, ast.ImportFrom):
                for a in imported.names:
                    if (a.asname or a.name) == name:
                        mod = _resolve_relative(
                            path[4:] if path.startswith("lib:") else path,
                            imported.module or "",
                            imported.level,
                        )
                        found = self.find_class(mod, a.name, depth + 1)
                        if found:
                            return found
        return None

    # -- field defaults ---------------------------------------------------------------
    def fields_of(
        self, path: str, cls: ast.ClassDef, depth: int = 0
    ) -> tuple[dict[str, dict], list[str]]:
        """Annotated fields of a pydantic-style class and its resolvable bases (child first)."""
        fields: dict[str, dict] = {}
        external: list[str] = []
        order, stack, seen = [], [(path, cls)], set()
        while stack:
            f, c = stack.pop(0)
            if (f, c.name) in seen:
                continue
            seen.add((f, c.name))
            order.append((f, c))
            for b in _base_names(c):
                r = self.find_in_file(f, b)
                if r:
                    stack.append(r)
                elif b not in ("BaseModel", "object", "Generic"):
                    external.append(b)
        for f, c in order:
            for node in c.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    name = node.target.id
                    if (
                        name.startswith("_")
                        or "ClassVar" in ast.unparse(node.annotation)
                        or name in fields
                    ):
                        continue
                    entry = {"definedIn": f"{f}:{node.lineno}", "class": c.name}
                    if node.value is not None:
                        entry["expr"] = ast.unparse(node.value)
                        d = self.default_value(node.value, f, depth + 1)
                        if d is not UNRESOLVED:
                            entry["default"] = d
                    fields[name] = entry
        return fields, sorted(set(external))

    def default_value(self, node: ast.expr | None, path: str, depth: int = 0) -> Any:
        """A field default as data: literals; ``Duration(ms=45)`` -> {"ms": 45}; an enum
        member -> its value; ``Model(k=v)`` -> that model's resolvable defaults with k=v;
        ``Field(default=...)``. Anything else is UNRESOLVED (never evaluated)."""
        if node is None or depth > 6:
            return UNRESOLVED
        is_literal, v = _literal(node)
        if is_literal:
            return list(v) if isinstance(v, tuple) else v
        if isinstance(node, (ast.Tuple, ast.List)):
            items = [self.default_value(e, path, depth + 1) for e in node.elts]
            return UNRESOLVED if any(i is UNRESOLVED for i in items) else items
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            r = self.find_in_file(path, node.value.id)
            if r:
                for st in r[1].body:
                    if (
                        isinstance(st, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == node.attr for t in st.targets)
                        and isinstance(st.value, ast.Constant)
                    ):
                        return st.value.value
            return UNRESOLVED
        if isinstance(node, ast.Call):
            fname = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
            )
            if (
                fname == "Duration"
                and not node.args
                and len(node.keywords) == 1
                and node.keywords[0].arg in DURATION_KEYS
            ):
                v = self.default_value(node.keywords[0].value, path, depth + 1)
                return (
                    {node.keywords[0].arg: v}
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                    else UNRESOLVED
                )
            if fname == "Field":
                for kw in node.keywords:
                    if kw.arg == "default":
                        return self.default_value(kw.value, path, depth + 1)
                    if kw.arg == "default_factory" and isinstance(kw.value, ast.Name):
                        if kw.value.id in ("list", "dict", "tuple"):
                            return {} if kw.value.id == "dict" else []
                        return self.default_value(
                            ast.Call(func=kw.value, args=[], keywords=[]), path, depth + 1
                        )
                return UNRESOLVED
            if isinstance(node.func, ast.Name) and not node.args:
                r = self.find_in_file(path, node.func.id)
                if r:
                    sub, _ = self.fields_of(r[0], r[1], depth + 1)
                    out = {k: e["default"] for k, e in sub.items() if "default" in e}
                    for kw in node.keywords:
                        if kw.arg is None or kw.arg not in sub:
                            return UNRESOLVED
                        v = self.default_value(kw.value, path, depth + 1)
                        if v is UNRESOLVED:
                            out.pop(kw.arg, None)
                        else:
                            out[kw.arg] = v
                    return out
        return UNRESOLVED


def _literal(node: ast.expr) -> tuple[bool, Any]:
    """(True, value) for a Python literal; (False, None) when the node is not one (that is an
    answer, not an error: the caller then tries the other forms it knows, or gives up)."""
    try:
        return True, ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False, None


def _class_attr(cls: ast.ClassDef, attr: str) -> ast.expr | None:
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == attr for t in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attr
            and node.value is not None
        ):
            return node.value
    return None


def _base_names(cls: ast.ClassDef) -> list[str]:
    out = []
    for b in cls.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Subscript) and isinstance(b.value, ast.Name):
            out.append(b.value.id)
    return out


@dataclass
class ModelInfo:
    task_class: str
    task_file: str | None
    params_model: str | None
    params_file: str | None
    fields: dict[str, dict]  # name -> {default?, expr, definedIn}
    external_bases: list[str]
    warnings: list[str]

    def default_of(self, key: str) -> tuple[bool, Any]:
        """(resolved, value) of a dotted key's model default."""
        head, *rest = key.split(".")
        f = self.fields.get(head)
        if not f or "default" not in f:
            return False, None
        v = f["default"]
        for part in rest:
            if v is None:
                return True, None  # the parent block is null by default
            if not isinstance(v, dict) or part not in v:
                return False, None
            v = v[part]
        return True, v


def task_model(index: ModuleIndex, registry: Registry, class_name: str) -> ModelInfo:
    warnings: list[str] = []
    imp = registry.imports.get(class_name)
    found = index.find_class(imp[0], imp[2]) if imp and not imp[1] else None
    if found is None or found[0].startswith("lib:"):
        return ModelInfo(
            class_name,
            None,
            None,
            None,
            {},
            [],
            [f"task class {class_name} not found in the project's source"],
        )
    tfile, tcls = found
    pm_node, pm_file, seen = None, tfile, set()
    stack = [(tfile, tcls)]
    while stack and pm_node is None:
        f, c = stack.pop(0)
        if (f, c.name) in seen:
            continue
        seen.add((f, c.name))
        pm_node, pm_file = _class_attr(c, "params_model"), f
        if pm_node is None:
            for b in _base_names(c):
                r = index.find_in_file(f, b)
                if r:
                    stack.append(r)
    if not isinstance(pm_node, ast.Name):
        warnings.append(f"{class_name}: params_model not found as a plain class name")
        return ModelInfo(class_name, tfile, None, None, {}, [], warnings)
    pm = index.find_in_file(pm_file, pm_node.id)
    if pm is None:
        warnings.append(f"{class_name}: params model {pm_node.id} not found in the source")
        return ModelInfo(class_name, tfile, pm_node.id, None, {}, [], warnings)
    fields, external = index.fields_of(*pm)
    if external:
        warnings.append(
            f"{class_name}: params model bases not readable ({', '.join(external)}); "
            "their fields are not listed"
        )
    return ModelInfo(class_name, tfile, pm_node.id, pm[0], fields, external, warnings)


def run_fields_from_alhazen_source(subject_kind_py: str) -> set[str]:
    """The fields of alhazen's SubjectParams (who the subject is, what pays): run metadata,
    not task parameters. Read from alhazen's own source at the pinned version."""
    tree = ast.parse(subject_kind_py)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SubjectParams":
            return {
                n.target.id
                for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
    raise SourceError("alhazen source has no SubjectParams class")


# ----------------------------------------------------------------------------- docs input


def _maybe_json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError as exc:
            raise SourceError(f"docs field is not JSON: {exc}") from exc
    return v


def normalise_docs(docs: Mapping[str, Any] | None) -> tuple[list[dict], list[dict] | None]:
    """(tasks, assumed) from a hub docs record (JSON strings) or a docs/experiment.json dict.
    ``assumed`` is None when the docs have no assumed table (not a replication)."""
    if not docs:
        return [], None
    tasks = _maybe_json(docs.get("tasks"), [])
    assumed = _maybe_json(docs.get("assumed"), None) if "assumed" in docs else None
    if not isinstance(tasks, list) or (assumed is not None and not isinstance(assumed, list)):
        raise SourceError("docs tasks/assumed must be lists")
    return tasks, assumed


# ----------------------------------------------------------------------------- one experiment


def _flatten(
    data: Any, documented: set[str], warnings: list[str], where: str, prefix: tuple = ()
) -> list[tuple[tuple, Any]]:
    """Mapping leaves as (path, value). Durations, empty mappings, lists, documented paths
    and mappings whose keys contain '.' stay one structured value."""
    out: list[tuple[tuple, Any]] = []
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        p = prefix + (k,)
        atomic = (
            not isinstance(v, dict)
            or not v
            or is_duration(v)
            or path_str(p) in documented
            or any(not isinstance(ck, str) for ck in v)
        )
        if not atomic and any("." in ck for ck in v):
            warnings.append(f"{where}: {path_str(p)} has keys containing '.', kept as one value")
            atomic = True
        if atomic:
            out.append((p, v))
        else:
            out.extend(_flatten(v, documented, warnings, where, p))
    return out


def _common_title(titles: list[str]) -> str | None:
    if not titles:
        return None
    pre = titles[0]
    for t in titles[1:]:
        while not t.startswith(pre):
            pre = pre[:-1]
    cut = max(pre.rfind(c) for c in ",:(—–")
    pre = (pre[:cut] if cut > 0 else pre).strip(" ,:(—–-")
    return pre if len(pre) >= 3 else None


def _docstring_summary(index: ModuleIndex, info: ModelInfo) -> str | None:
    if not info.task_file:
        return None
    found = index.find_in_file(info.task_file, info.task_class)
    return first_paragraph(ast.get_docstring(found[1])) if found else None


def extract_experiment(
    experiment_id: str,
    files: Mapping[str, str | bytes],
    *,
    source_commit: str,
    docs: Mapping[str, Any] | None = None,
    docs_origin: str | None = None,
    run_metadata_fields: set[str] | frozenset[str] | None = None,
    library_files: Mapping[str, str | bytes] | None = None,
    source_root: str = "",
) -> dict:
    """The manifest entry for one experiment, from its files as of ``source_commit``.

    files: path relative to the experiment root -> text or bytes (only run.py, configs/,
    the task modules and pyproject are read). docs: hub docs record or experiment.json.
    run_metadata_fields: alhazen SubjectParams field names (``run_fields_from_alhazen_source``);
    None derives them from ``library_files`` when it holds SubjectParams, else every key stays
    a task parameter and a warning says so.
    library_files: the pinned alhazen's ``src/`` files ({"alhazen/...py": text}); lets model
    defaults inherited from alhazen classes (SchedulerConfig, SubjectParams) be read."""
    warnings: list[str] = []
    if "run.py" not in files:
        raise SourceError(f"{experiment_id}: no run.py at {source_commit[:7]}")
    reg = read_registry(_text(files["run.py"]))
    warnings += reg.warnings
    if not reg.sets:
        warnings.append("run.py declares no PARAMETERS sets")
    doc_tasks, assumed_table = normalise_docs(docs)
    replication = assumed_table is not None
    assumed_keys: dict[str, list[str]] = {}
    for a in assumed_table or []:
        if isinstance(a, dict) and a.get("key"):
            assumed_keys.setdefault(str(a["key"]), []).append(str(a.get("reason") or ""))
    index = ModuleIndex(files, library_files)
    if run_metadata_fields is None:
        sp = index.find_class("alhazen.task.subject_kind", "SubjectParams")
        if sp:
            run_metadata_fields = set(index.fields_of(*sp)[0])
    run_fields = set(run_metadata_fields or ())
    if run_metadata_fields is None:
        warnings.append(
            "run metadata fields (alhazen SubjectParams) unknown: "
            "subject/reward kept with task values"
        )
    models: dict[str, ModelInfo] = {}
    for tname, t in reg.tasks.items():
        if t["class"]:
            models[tname] = task_model(index, reg, t["class"])
            warnings += [f"task {tname}: {w}" for w in models[tname].warnings]
    docs_by_task: dict[str, list[dict]] = {}
    for dt in doc_tasks:
        docs_by_task.setdefault(str(dt.get("id")), []).append(dt)
    for tid in docs_by_task:
        if tid not in reg.tasks:
            warnings.append(
                f"docs describe task {tid!r}, which run.py TASKS does not declare at this commit"
            )
    for tid, entries in docs_by_task.items():
        for e in entries:
            pf = e.get("parameters_file")
            if pf and pf not in files:
                warnings.append(
                    f"docs entry {tid!r} names {pf}, which is not in the source at this commit"
                )

    def make_item(
        key: str, v: Any, doc_params: dict, note: KeyNote | None, cfg: str | None
    ) -> dict:
        """One value: its docs (label, unit, group, meaning), its comments, its provenance."""
        dp, dfile = doc_params.get(key, (None, None))
        unit = (dp or {}).get("unit") or unit_from_key(key)
        path = key.split(".")
        group = (
            (dp or {}).get("group")
            or (note.section if note else None)
            or (path[0] if len(path) > 1 else None)
        )
        item: dict[str, Any] = {
            "key": key,
            "label": (dp or {}).get("label"),
            "value": v,
            "unit": unit,
            "group": group,
            "meaning": (dp or {}).get("meaning"),
            "assumed": None,
            "documented": dp is not None,
        }
        if not isinstance(v, (str, int, float)) or isinstance(v, bool):
            item["display"] = display(v)  # scalars display as themselves
        if unit and not (dp or {}).get("unit"):
            item["unitSource"] = "key suffix"
        if dp is not None and dfile and dfile != cfg:
            item["meaningFrom"] = dfile
        if note:
            item["line"] = note.line
            if note.inline:
                item["comment"] = note.inline
            if note.lead:
                item["leadComment"] = note.lead
        if replication:
            c = classify_comment(note.inline if note else None, note.lead if note else None)
            if "assumed" in c:
                item["assumed"] = c["assumed"]  # the set's own config comment says so
            elif key in assumed_keys:
                item["assumed"], item["assumedSource"] = (
                    True,
                    "docs assumed table (not set-specific)",
                )
            if c.get("deviation"):
                item["deviation"] = True
        return item

    # ---- sets
    ids: set[str] = set()
    sets: list[dict] = []
    parsed_cache: dict[str, ParsedYaml] = {}
    flat_by_set: dict[str, dict[str, dict]] = {}
    for s in reg.sets:
        sid = slugify(s["label"])
        n = 2
        while sid in ids:
            sid, n = f"{slugify(s['label'])}-{n}", n + 1
        ids.add(sid)
        task = s["task"] or (next(iter(reg.tasks)) if len(reg.tasks) == 1 else None)
        if task is None:
            warnings.append(f"set {sid}: task cannot be determined from run.py")
        cfg = s["config"]
        model = models.get(task) if task else None
        entries = docs_by_task.get(task, []) if task else []
        exact = next((e for e in entries if cfg and e.get("parameters_file") == cfg), None)
        doc_params: dict[str, tuple[dict, str | None]] = {}
        for e in ([exact] if exact else []) + [e for e in entries if e is not exact]:
            for p in e.get("parameters") or []:
                if isinstance(p, dict) and p.get("name") and p["name"] not in doc_params:
                    doc_params[p["name"]] = (p, e.get("parameters_file"))
        values: list[dict] = []
        runv: list[dict] = []
        header = None
        flat: dict[str, dict] = {}
        refs: list[dict] = []
        if cfg is None:
            source_kind = "task model defaults (run.py gives no params file)"
            if model and model.fields:
                resolved = {k: f["default"] for k, f in model.fields.items() if "default" in f}
                for name, f in model.fields.items():
                    if "default" not in f:
                        warnings.append(
                            f"set {sid}: default of {name} is {f.get('expr', 'required')!r}, "
                            "not resolvable statically"
                        )
                for path, raw in _flatten(
                    resolved, set(doc_params), warnings, f"{model.params_model} defaults"
                ):
                    key = path_str(path)
                    item = make_item(key, _json_safe(raw, key, warnings), doc_params, None, cfg)
                    item["source"] = "model default"
                    item["definedIn"] = model.fields[path[0]]["definedIn"]
                    (runv if path[0] in run_fields else values).append(item)
                    flat[key] = item
                for name, f in model.fields.items():
                    if "default" not in f:
                        item = make_item(name, None, doc_params, None, cfg)
                        item.update(
                            source="model default",
                            resolved=False,
                            expr=f.get("expr"),
                            display=f.get("expr") or "required",
                            definedIn=f["definedIn"],
                        )
                        (runv if name in run_fields else values).append(item)
                        flat[name] = item
            else:
                warnings.append(f"set {sid}: no params file and no readable model defaults")
        elif cfg not in files:
            source_kind = "missing"
            warnings.append(f"set {sid}: params file {cfg} is not in the source at this commit")
        else:
            source_kind = "params file"
            try:
                parsed = parsed_cache.get(cfg) or parse_yaml(_text(files[cfg]), cfg)
            except SourceError as exc:
                warnings.append(f"set {sid}: {exc}")
                parsed = None
            if parsed is not None:
                parsed_cache[cfg] = parsed
                warnings += [w for w in parsed.warnings if w not in warnings]
                if parsed.data is not None and not isinstance(parsed.data, dict):
                    warnings.append(f"set {sid}: {cfg} is not a mapping at the top level")
                header = parsed.header
                notes = {n.path: n for n in parsed.notes}
                documented = set(doc_params)
                for path, raw in _flatten(parsed.data or {}, documented, warnings, cfg):
                    key = path_str(path)
                    v = _json_safe(raw, f"{cfg}:{key}", warnings)
                    note = notes.get(path)
                    item = make_item(key, v, doc_params, note, cfg)
                    if isinstance(raw, dict) and any(not isinstance(k, str) for k in raw):
                        item["keysStringified"] = True
                    dp, dfile = doc_params.get(key, (None, None))
                    if isinstance(v, str) and v in files and v != cfg:
                        item["file"] = True
                        refs.append({"key": key, "path": v})
                    if (
                        exact
                        and dp is not None
                        and dfile == cfg
                        and "default" in dp
                        and canon(dp["default"]) != canon(v)
                    ):
                        warnings.append(
                            f"set {sid}: docs give {key} = {display(dp['default'])} for {cfg}, "
                            f"the source has {display(v)}"
                        )
                    (runv if path[0] in run_fields else values).append(item)
                    flat[key] = item
                unset = [
                    n
                    for n in doc_params
                    if n not in flat
                    and not any(k.startswith(n + ".") for k in flat)
                    and not any(n.startswith(k + ".") for k in flat)
                ]
                unresolved: list[str] = []
                for name in unset:
                    ok, dv = model.default_of(name) if model else (False, None)
                    if not ok or model is None:
                        unresolved.append(name)
                        continue
                    item = make_item(name, _json_safe(dv, name, warnings), doc_params, None, cfg)
                    item["source"] = "model default (documented, not set in this file)"
                    item["definedIn"] = model.fields[name.split(".")[0]]["definedIn"]
                    (runv if name.split(".")[0] in run_fields else values).append(item)
                    flat[name] = item
                if unresolved:
                    warnings.append(
                        f"set {sid}: docs describe {', '.join(unresolved)}, "
                        f"which {cfg} does not set "
                        f"and whose task default is not readable statically"
                    )
        flat_by_set[sid] = flat
        subject, subject_src = None, None
        if "subject_kind" in flat and flat["subject_kind"].get("value") is not None:
            subject, subject_src = (
                flat["subject_kind"]["value"],
                "params file" if cfg else "model default",
            )
        else:
            m = re.search(r"\((?:[^()]*,\s*)?(human|monkey)\)\s*$", s["label"])
            if m:
                subject, subject_src = m.group(1), "set label"
        if subject is None:
            warnings.append(
                f"set {sid}: subject not declared (no subject_kind, no (human)/(monkey) label)"
            )
        response = None
        if "response_mode" in flat:
            response = {
                "mode": flat["response_mode"]["value"],
                "source": "params file response_mode",
            }
        elif model and "response_mode" in model.fields:
            f = model.fields["response_mode"]
            response = {
                "mode": f.get("default", f.get("expr")),
                "source": f"model default ({f['definedIn']})",
            }
        purpose, purpose_src = None, None
        if exact and exact.get("summary"):
            purpose, purpose_src = exact["summary"], "docs task entry for this file"
        elif first_paragraph(header):
            purpose, purpose_src = first_paragraph(header), "params file header comment"
        impl = None
        if model:
            impl = {
                "class": model.task_class,
                "file": model.task_file,
                "paramsModel": model.params_model,
                "paramsModelFile": model.params_file,
            }
        entry = {
            "id": sid,
            "name": s["label"],
            "configPath": cfg,
            "taskIds": [task] if task else [],
            "taskImplementation": impl,
            "subject": subject,
            "purpose": purpose,
            "response": response,
            "values": values,
            "runValues": runv,
            "differences": [],
            "provenance": {
                "sourceCommit": source_commit,
                "registeredIn": "run.py PARAMETERS",
                "label": s["label"],
                "configPath": cfg,
                "configBlob": git_blob_sha(files[cfg]) if cfg and cfg in files else None,
                "valuesFrom": source_kind,
                "subjectSource": subject_src,
                "purposeSource": purpose_src,
                "taskDefault": bool(task and reg.tasks.get(task, {}).get("config") == cfg),
                "docsEntry": (exact or {}).get("parameters_file"),
                "docsOrigin": docs_origin,
            },
        }
        froms = {v.get("meaningFrom") for v in values + runv if v.get("documented")}
        if len(froms) == 1 and None not in froms:  # every meaning from one other docs entry
            entry["provenance"]["meaningsFrom"] = froms.pop()
            for v in values + runv:
                v.pop("meaningFrom", None)
        if exact and exact.get("title"):
            entry["docsTitle"] = exact["title"]
        if header:
            entry["headerComment"] = header
        if refs:
            entry["referencedFiles"] = refs
        sets.append(entry)

    # ---- comparisons (same task only)
    def diff(a: dict[str, dict], b: dict[str, dict], model: ModelInfo | None) -> list[dict]:
        out = []
        for key in list(dict.fromkeys([*a, *b])):
            ia, ib = a.get(key), b.get(key)
            va, vb = (ia or {}).get("value"), (ib or {}).get("value")
            src_a = src_b = None
            ok, dv = model.default_of(key) if model else (False, None)
            if ia is None and ok:
                va, src_a = dv, "model default"
            if ib is None and ok:
                vb, src_b = dv, "model default"
            if (ia is not None or src_a) and (ib is not None or src_b) and canon(va) == canon(vb):
                continue
            ref = ia or ib or {}
            d = {
                "key": key,
                "label": ref.get("label"),
                "value": va,
                "comparedTo": None,
                "baseValue": vb,
                "meaning": ref.get("meaning"),
                "display": display(va) if (ia or src_a) else "not set",
                "baseDisplay": display(vb) if (ib or src_b) else "not set",
            }
            if ref.get("unit"):
                d["unit"] = ref["unit"]
            if ia is None:
                d["valueSource"] = (
                    src_a
                    or "not set in this file (task default applies; not resolvable statically)"
                )
            if ib is None:
                d["baseValueSource"] = (
                    src_b
                    or "not set in that file (task default applies; not resolvable statically)"
                )
            if key.split(".")[0] in run_fields:
                d["scope"] = "run"
            out.append(d)
        return out

    by_task: dict[str, list[dict]] = {}
    for e in sets:
        for t in e["taskIds"]:
            by_task.setdefault(t, []).append(e)
    for task_id, group in by_task.items():
        model = models.get(task_id)
        default_cfg = reg.tasks.get(task_id, {}).get("config")
        for e in group:
            others = [o for o in group if o is not e]
            if not others:
                e["comparison"] = {
                    "comparedTo": None,
                    "reason": "the only parameter set of this task",
                }
                continue
            base = next((o for o in others if default_cfg and o["configPath"] == default_cfg), None)
            reason = "the task's default parameter set (run.py TASKS)"
            if base is None:
                base = min(
                    others,
                    key=lambda o: len(diff(flat_by_set[e["id"]], flat_by_set[o["id"]], model)),
                )
                reason = "the closest other parameter set of the same task"
            ds = diff(flat_by_set[e["id"]], flat_by_set[base["id"]], model)
            for d in ds:
                d["comparedTo"] = base["id"]
            e["differences"] = ds
            e["comparison"] = {
                "comparedTo": base["id"],
                "comparedToName": base["name"],
                "reason": reason,
                "nDifferences": len(ds),
            }

    # ---- task definitions
    tdefs: list[dict] = []
    for tname, t in reg.tasks.items():
        info = models.get(tname)
        entries = docs_by_task.get(tname, [])
        title, tsrc, summary, ssrc = None, None, None, None
        if len(entries) == 1:
            title, tsrc = entries[0].get("title"), "docs task entry"
            summary, ssrc = entries[0].get("summary"), "docs task entry"
        elif entries:
            title = _common_title([str(e.get("title") or "") for e in entries])
            tsrc = "common start of the docs entries' titles" if title else None
            pick = next((e for e in entries if e.get("parameters_file") == t["config"]), entries[0])
            summary, ssrc = (
                pick.get("summary"),
                f"docs task entry for {pick.get('parameters_file')}",
            )
        if not title:
            title, tsrc = tname, "task id (no docs title)"
        if not summary and info:
            summary = _docstring_summary(index, info)
            ssrc = "task class docstring" if summary else None
        if not entries:
            warnings.append(f"task {tname}: no docs entry describes it")
        tdefs.append(
            {
                "id": tname,
                "title": title,
                "titleSource": tsrc,
                "summary": summary,
                "summarySource": ssrc,
                "implementations": [
                    {
                        "class": info.task_class,
                        "file": info.task_file,
                        "paramsModel": info.params_model,
                        "paramsModelFile": info.params_file,
                        "externalBases": info.external_bases,
                    }
                ]
                if info
                else [],
                "defaultConfig": t["config"],
                "setIds": [e["id"] for e in sets if tname in e["taskIds"]],
                "docsEntries": [
                    {"title": e.get("title"), "parametersFile": e.get("parameters_file")}
                    for e in entries
                ],
            }
        )
        if not tdefs[-1]["setIds"]:
            warnings.append(f"task {tname}: declared in TASKS but no PARAMETERS set runs it")

    # ---- training ladders (guides; their stage configs are overrides, not sets)
    ladders: list[dict] = []
    for lad in reg.ladders:
        f = lad["file"]
        if f not in files:
            warnings.append(f"ladder {lad['label']!r}: {f} not in the source at this commit")
            continue
        try:
            py = parse_yaml(_text(files[f]), f)
        except SourceError as exc:
            warnings.append(f"ladder {lad['label']!r}: {exc}")
            continue
        data = py.data if isinstance(py.data, dict) else {}
        notes = {n.path: n for n in py.notes}
        stages_raw = data.get("stages")
        if not isinstance(stages_raw, list) or not data.get("name") or not data.get("title"):
            warnings.append(f"ladder {lad['label']!r}: {f} lacks name, title or a stages list")
            continue
        folder = PurePosixPath(f).parent

        def rel(p: Any, folder: PurePosixPath = folder) -> str | None:
            if not isinstance(p, str):
                return None
            parts: list[str] = []
            for seg in (folder / p).parts:
                if seg == "..":
                    if parts:
                        parts.pop()
                elif seg != ".":
                    parts.append(seg)
            return "/".join(parts)

        lparams = rel(data.get("params"))
        stages = []
        for i, st in enumerate(stages_raw):
            if not isinstance(st, dict) or "id" not in st:
                warnings.append(
                    f"ladder {lad['label']!r}: stage {i + 1} is not a mapping with an id"
                )
                continue
            task = st.get("task") or data.get("task")
            if task and ":" not in str(task) and task not in reg.tasks:
                warnings.append(
                    f"ladder {lad['label']!r} stage {st['id']!r} runs {task!r}, not one of TASKS"
                )
            params = rel(st.get("params")) or lparams
            ovs = []
            for k, v in (st.get("overrides") or {}).items():
                note = notes.get(("stages", i, "overrides", k))
                o = {
                    "key": str(k),
                    "value": _json_safe(v, f"{f}:{k}", warnings),
                    "display": display(v),
                }
                if note and note.inline:
                    o["comment"] = note.inline
                if replication:
                    c = classify_comment(note.inline if note else None)
                    if "assumed" in c:
                        o["assumed"] = c["assumed"]
                ovs.append(o)
            stage = {
                "id": st["id"],
                "number": i + 1,
                "title": st.get("title"),
                "description": st.get("description"),
                "task": task,
                "taskKind": "training class (module:Class)"
                if task and ":" in str(task)
                else "TASKS entry",
                "params": params,
                "overrides": ovs,
                "success": st.get("success"),
            }
            for k in ("reward", "criterion"):
                if st.get(k) is not None:
                    stage[k] = _json_safe(st[k], f"{f}:{k}", warnings)
            stages.append(stage)
        set_of = {e["configPath"]: e["id"] for e in sets}
        ladders.append(
            {
                "id": slugify(lad["label"]),
                "name": lad["label"],
                "configPath": f,
                "ladderName": data.get("name"),
                "title": data.get("title"),
                "description": data.get("description"),
                "task": data.get("task"),
                "params": lparams,
                "paramsSetId": set_of.get(lparams),
                "headerComment": py.header,
                "stages": stages,
                "configBlob": git_blob_sha(files[f]),
            }
        )
        if lparams and lparams not in files:
            warnings.append(f"ladder {lad['label']!r}: params {lparams} not in the source")

    # ---- other configs: rigs (hardware), data files named by values, unregistered files
    used = (
        {e["configPath"] for e in sets}
        | {t["config"] for t in reg.tasks.values()}
        | {lad["file"] for lad in reg.ladders}
    )
    used |= {st.get("params") for lad in ladders for st in lad["stages"]}
    hw = []
    for p in sorted(files):
        if p.startswith("configs/") and p.endswith((".yaml", ".yml")) and p not in used:
            loaded: Any
            try:
                loaded = _load_yaml(_text(files[p]))
            except _AliasRefused as exc:
                loaded = None
                warnings.append(f"{p}: {exc}")
            except yaml.YAMLError:
                loaded = None
                warnings.append(f"{p}: invalid YAML")
            rb = sorted(
                {r["key"] for e in sets for r in e.get("referencedFiles", []) if r["path"] == p}
            )
            if PurePosixPath(p).name.startswith("rig-"):
                kind = "rig (hardware; alhazen rig file)"
            elif rb:
                kind = "data file named by a parameter value"
            else:
                kind = "not registered in run.py"
            item = {"configPath": p, "kind": kind}
            if isinstance(loaded, dict) and isinstance(loaded.get("extends"), str):
                item["extends"] = loaded["extends"]
            if rb:
                item["referencedBy"] = rb
            hw.append(item)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "experimentId": experiment_id,
        "sourceCommit": source_commit,
        "sourceRoot": source_root,
        "assumedApplies": replication,
        "docsOrigin": docs_origin,
        "sets": sets,
        "taskDefinitions": tdefs,
        "ladders": ladders,
        "otherConfigs": hw,
        "warnings": list(dict.fromkeys(warnings)),
    }


# ----------------------------------------------------------------------------- inputs (no I/O)

_PIN = re.compile(r'alhazen-vision\s*=\s*\{[^}]*tag\s*=\s*"([^"]+)"')
SUBJECT_KIND_FILE = "alhazen/task/subject_kind.py"


def alhazen_pin(files: Mapping[str, Any]) -> str | None:
    """The alhazen tag an experiment pins in its pyproject.toml (``[tool.uv.sources]``
    ``alhazen-vision = { git = ..., tag = "vX.Y.Z" }``), or None."""
    match = _PIN.search(_text(files.get("pyproject.toml", b"")))
    return match.group(1) if match else None


def run_fields_from_library(library: Mapping[str, str | bytes] | None) -> set[str] | None:
    """The SubjectParams field names of an alhazen source (``library_files``), or None
    when it is absent or has no SubjectParams."""
    if not library or SUBJECT_KIND_FILE not in library:
        return None
    try:
        return run_fields_from_alhazen_source(_text(library[SUBJECT_KIND_FILE]))
    except (SourceError, SyntaxError):
        return None


def library_files(tree: Any) -> dict[str, bytes]:
    """alhazen's own Python source from a ``SourceTree`` of the alhazen repository (read
    at the pinned tag, the whole repository or its ``src`` folder), keyed
    ``"alhazen/...py"`` as ``extract_experiment`` expects."""
    prefix = "" if tree.subdir.strip("/") == "src" else "src/"
    out: dict[str, bytes] = {}
    for path, raw in tree.files.items():
        if not path.startswith(prefix) or not path.endswith(".py"):
            continue
        rel = path[len(prefix) :]
        if rel.startswith("alhazen/"):
            out[rel] = raw
    if SUBJECT_KIND_FILE not in out:
        raise SourceError("the tree is not alhazen's source (no alhazen/task/subject_kind.py)")
    return out


def installed_library() -> tuple[dict[str, bytes], str]:
    """The installed alhazen's own Python source (keyed ``"alhazen/...py"``) and its
    version. Use it for an experiment only when ``pin_matches(version, tag)``: another
    version's defaults are not that experiment's."""
    import alhazen.version as version_module

    root = Path(version_module.__file__).resolve().parent
    files = {
        "alhazen/" + path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return files, version_module.__version__


def pin_matches(version: str, tag: str | None) -> bool:
    """Whether an installed alhazen ``version`` (``2.16.0``) is the pinned ``tag``
    (``v2.16.0``)."""
    return bool(tag) and tag.removeprefix("v") == version.removeprefix("v")  # type: ignore[union-attr]


def build_parameter_guide(
    experiment_id: str,
    files: Mapping[str, str | bytes],
    *,
    source_commit: str,
    docs: Mapping[str, Any] | None = None,
    docs_origin: str | None = None,
    library: Mapping[str, str | bytes] | None = None,
    source_root: str = "",
    pinned: str | None = None,
    listed_sets: list[str] | None = None,
    listed_ladders: list[str] | None = None,
) -> dict:
    """The parameter guide a catalogue version stores: ``extract_experiment`` plus what
    binds it to the alhazen it was read against. ``library`` is alhazen's source at the
    experiment's pinned tag (``library_files`` / ``installed_library``), or None when it is
    not available, which the guide then says. ``pinned`` is the tag recorded when
    pyproject.toml names none. ``listed_sets`` / ``listed_ladders`` are a listing's set
    and ladder names; a disagreement with run.py at this commit is reported, not fixed.

    This is the Mana hub's ``build_entry`` without its git reads: the caller reads the
    files (``sources.read_tree``) and alhazen's source."""
    tag = alhazen_pin(files) or pinned
    run_fields = run_fields_from_library(library)
    entry = extract_experiment(
        experiment_id,
        files,
        source_commit=source_commit,
        docs=docs,
        docs_origin=docs_origin,
        run_metadata_fields=run_fields,
        library_files=library,
        source_root=source_root,
    )
    if library is None:
        entry["warnings"].append(
            f"alhazen {tag} source not readable: defaults inherited from alhazen are not listed"
        )
    entry["alhazen"] = tag
    if run_fields is not None:
        entry["runMetadataFields"] = sorted(run_fields)
    if listed_sets is not None:
        got = [s["name"] for s in entry["sets"]]
        if list(listed_sets) != got:
            entry["warnings"].append(
                f"listing parameterSets {list(listed_sets)} differ from run.py at "
                f"{source_commit[:7]}: {got}"
            )
    if listed_ladders is not None:
        names = [lad["name"] for lad in entry["ladders"]]
        if list(listed_ladders) != names:
            entry["warnings"].append(
                f"listing ladders {list(listed_ladders)} differ from run.py: {names}"
            )
    return entry
