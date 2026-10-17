"""Catalogue records from an experiment's source: the listing and its documentation.

``build_version`` turns one ``SourceTree`` (an experiment at one commit) into the three
things a catalogue version is: the bundle, the listing and the documentation record. It
is the portable form of the Mana platform's catalogue builder, without any storage: the
caller decides where the records go (a FileStore here, Firebase in the Mana adapter).

Where each field comes from:

- the experiment's own files: ``pyproject.toml`` (version, the pinned alhazen tag),
  ``run.py`` (parameter sets and training ladders), ``docs/experiment.json`` and
  ``docs/methods.md`` (title, summary, tasks, references, assumptions, deviations),
  ``docs/listing.json`` (status, category, subjects, hardware, original citation,
  licence, picture) and ``README.md``;
- the caller's ``overrides`` for what a repository does not say about itself (an emblem,
  a category, whether it is featured);
- documentation from another ref when the caller names one (``docs_tree``), recorded as
  such in ``docsource`` so a proposal is never presented as the reviewed main branch.

The parameter guide is extracted from the same files (AST + safe YAML) and bound to the
source commit; the reader document is resolved through the bounded documentation
validator and bound to the bundle id. Neither is ever carried over from another version.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from alhazen.experiment_platform.bundles import Bundle, build_bundle, split_blobs
from alhazen.experiment_platform.errors import PlatformError
from alhazen.experiment_platform.safety import check_id
from alhazen.experiment_platform.sources import SourceTree

PLATFORMS = ["linux", "darwin", "win32"]
HW_KEYS = ("display", "keyboard", "audio", "eye_tracker", "reward", "microstimulation", "recording")
# Replication tables shown structurally on the experiment page, so the methods text
# leaves them out rather than repeating them.
STRIP_SECTIONS = ("Deviations from the original", "Assumed, not in the paper")
README_LIMIT = 20000
# Listing fields an operator may set for a repository that does not describe itself.
OVERRIDABLE = (
    "title",
    "summary",
    "art",
    "featured",
    "category",
    "tags",
    "subjects",
    "hardware",
    "license",
    "status",
    "statusReason",
    "collection",
)


@dataclass
class BuiltVersion:
    """One catalogue version, ready to store."""

    exp: str
    bundle: Bundle
    listing: dict[str, Any]
    docs: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _json(text: str, where: str, warnings: list[str]) -> Any:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        warnings.append(f"{where} is not valid JSON and was ignored")
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{where} is not a JSON object and was ignored")
        return {}
    return value


def first_paragraph(markdown: str) -> str:
    for block in markdown.split("\n\n"):
        b = block.strip()
        if b and not b.startswith(("#", "```", "|", "!")):
            return re.sub(r"\s+", " ", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", b))[:600]
    return ""


def strip_sections(markdown: str, titles: tuple[str, ...] = STRIP_SECTIONS) -> str:
    """Remove ``## <title>`` sections (the page shows them as tables)."""
    out, skip = [], False
    for line in markdown.splitlines(keepends=True):
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            skip = m.group(1) in titles
        if not skip:
            out.append(line)
    return "".join(out)


def short_citation(citation: Any) -> str:
    """'Treisman, A. M., & Gelade, G. (1980). ...' -> 'Treisman & Gelade'."""
    authors = str(citation or "").split(" (")[0]
    names = re.findall(r"(?:^|,\s|&\s)([A-Z][A-Za-z'\-]+),\s[A-Z]\.", authors)
    if not names:
        return authors.split(",")[0].strip()
    if len(names) == 1:
        return names[0]
    return f"{names[0]} & {names[1]}" if len(names) == 2 else f"{names[0]} et al."


def license_text(lic: Any) -> str:
    """``{'code': 'Apache-2.0', 'docs': 'CC-BY-4.0', 'assets': 'CC-BY-4.0'}`` -> one line."""
    if not lic or not isinstance(lic, dict):
        return "Not stated"

    def pretty(s: Any) -> str:
        return str(s).replace("CC-BY-4.0", "CC BY 4.0")

    parts = [f"{pretty(lic['code'])} (code)"] if lic.get("code") else []
    if lic.get("docs") and lic.get("docs") == lic.get("assets"):
        parts.append(f"{pretty(lic['docs'])} (methods, generated assets)")
    else:
        parts += [
            f"{pretty(lic[k])} ({n})"
            for k, n in (("docs", "methods"), ("assets", "generated assets"))
            if lic.get(k)
        ]
    return "; ".join(parts)


def registry_keys(run_py: str, name: str) -> list[str]:
    """The keys of a top-level ``NAME = {...}`` dict literal in run.py, in order (the
    registered parameter sets or ladders), read as text, never executed."""
    m = re.search(r"^" + name + r"(?:\s*:[^=]+)?\s*=\s*\{(.*?)^\}", run_py, re.S | re.M)
    return re.findall(r'^\s{4}"([^"]+)"\s*:', m.group(1), re.M) if m else []


def project_version(pyproject: str) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    return m.group(1) if m else ""


def alhazen_tag(pyproject: str) -> str:
    m = re.search(r'alhazen-vision\s*=\s*\{[^}]*tag\s*=\s*"([^"]+)"', pyproject)
    return m.group(1) if m else ""


def normalised_tasks(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The task records the page lists (the documented fields only)."""
    tasks = []
    for t in doc.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        record = {
            k: t.get(k)
            for k in ("id", "title", "summary", "parameters_file", "outcomes", "events", "timeline")
            if t.get(k) is not None
        }
        record["parameters"] = [
            {
                k: p.get(k)
                for k in ("name", "label", "group", "type", "unit", "default", "meaning")
                if p.get(k) is not None
            }
            for p in t.get("parameters") or []
            if isinstance(p, dict)
        ]
        tasks.append(record)
    return tasks


def experiment_id(tree: SourceTree, explicit: str | None = None) -> str:
    if explicit:
        return check_id(explicit, "Experiment id")
    name = tree.subdir.rstrip("/").split("/")[-1] if tree.subdir else tree.repo.resolve().name
    return check_id(
        re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._") or "experiment", "Experiment id"
    )


def build_version(
    tree: SourceTree,
    *,
    publisher: Mapping[str, str],
    exp_id: str | None = None,
    repo_label: str = "",
    overrides: Mapping[str, Any] | None = None,
    docs_tree: SourceTree | None = None,
    overlay: Mapping[str, Any] | None = None,
    library: Mapping[str, Any] | None = None,
    store_art: Callable[[str, bytes, str], str] | None = None,
    pushed: bool | None = None,
    now_ms: int | None = None,
) -> BuiltVersion:
    """The catalogue version of ``tree``. ``docs_tree`` is the same experiment at the ref
    its proposed documentation lives on (``None``: documentation from ``tree`` itself).
    ``overlay`` is a guide overlay ({bundleId, tasks}); it is applied only when its
    bundleId equals this version's bundle id. ``library`` is the pinned alhazen's source
    files ({"alhazen/...py": bytes}), used only to list defaults inherited from alhazen.
    ``store_art(name, data, suffix)`` stores the listing picture and returns its name."""
    warnings: list[str] = []
    exp = experiment_id(tree, exp_id)
    publisher = {
        "uid": check_id(publisher["uid"], "Publisher id"),
        "name": str(publisher.get("name") or ""),
    }
    t = now_ms if now_ms is not None else int(time.time() * 1000)
    over = {k: v for k, v in dict(overrides or {}).items() if k in OVERRIDABLE}

    entries, excluded = split_blobs(tree.blobs())
    extra = {"repo": repo_label, "commit": tree.commit}
    if tree.subdir:
        extra["subdir"] = tree.subdir
    bundle = build_bundle(entries, excluded, exp, extra)

    doc_source = docs_tree or tree
    doc = _json(doc_source.text("docs/experiment.json"), "docs/experiment.json", warnings)
    listing_json = _json(tree.text("docs/listing.json"), "docs/listing.json", warnings)
    pyproject = tree.text("pyproject.toml")
    run_py = tree.text("run.py")
    readme = tree.text("README.md")
    methods_path = doc.get("methods") if isinstance(doc.get("methods"), str) else "docs/methods.md"
    methods = strip_sections(doc_source.text(methods_path))
    if docs_tree is not None:
        methods = re.sub(
            r"^These methods are a proposal.*?\n\n", "", methods, count=1, flags=re.S | re.M
        )
        main_ref = tree.ref.replace("origin/", "")
        docsource = (
            f"proposed documentation ({docs_tree.ref} branch, not yet reviewed or on {main_ref})"
        )
    elif doc:
        docsource = "docs/experiment.json and docs/methods.md " + (
            "in the folder" if tree.subdir else "at this commit"
        )
    else:
        docsource = "README at this commit"
    tasks = normalised_tasks(doc)
    summary = doc.get("summary") or first_paragraph(readme)
    summary = re.sub(
        r"\s*Proposed documentation drawn from.*$", "", str(summary), flags=re.I | re.S
    ).strip()

    hardware_src = (
        listing_json.get("hardware") if isinstance(listing_json.get("hardware"), dict) else None
    )
    original = (
        listing_json.get("original") if isinstance(listing_json.get("original"), dict) else {}
    )
    category = listing_json.get("category") or over.get("category") or "attention"
    art_image = None
    art_path = listing_json.get("art") or "docs/preview.png"
    if store_art is not None and art_path in tree.files:
        suffix = "." + art_path.rsplit(".", 1)[-1].lower() if "." in art_path else ".png"
        try:
            art_image = store_art(exp, tree.files[art_path], suffix)
        except PlatformError as error:
            warnings.append(f"The listing picture {art_path} was not stored: {error}")

    source: dict[str, Any] = {
        "kind": "repo",
        "repo": repo_label,
        "ref": tree.ref.replace("origin/", ""),
        "commit": (tree.folder_commit or tree.commit) if tree.subdir else tree.commit,
        "commitDate": tree.commit_date,
        "subject": tree.subject,
    }
    if tree.subdir:
        source.update({"repoCommit": tree.commit, "subdir": tree.subdir})
    if pushed is not None:
        source["pushed"] = pushed
    if docs_tree is not None:
        source["docsRef"] = docs_tree.ref
        source["docsCommit"] = docs_tree.commit

    listing: dict[str, Any] = {
        "id": exp,
        "title": doc.get("title") or exp,
        "summary": summary,
        "art": "fixation",
        "featured": False,
        "category": category,
        "tags": list(dict.fromkeys([category, *(listing_json.get("tags") or [])])),
        "subjects": listing_json.get("subjects") or ["human"],
        "hardware": {k: bool((hardware_src or {}).get(k)) for k in HW_KEYS}
        if hardware_src is not None
        else {},
        "platforms": list(PLATFORMS),
        "license": license_text(doc.get("license"))
        if doc.get("license")
        else "All rights reserved",
        "publisher": publisher,
        "source": source,
        "version": project_version(pyproject),
        "alhazen": alhazen_tag(pyproject),
        "parameterSets": registry_keys(run_py, "PARAMETERS"),
        "ladders": registry_keys(run_py, "LADDERS"),
        "bundle": {
            "id": bundle.id,
            "fileCount": bundle.meta["fileCount"],
            "bytes": bundle.meta["bytes"],
            "binaries": bundle.meta.get("binaries", 0),
            "excluded": len(json.loads(bundle.meta["excluded"])),
        },
        "docsource": docsource,
        "publishedAt": t,
        "updatedAt": t,
    }
    if listing_json:
        listing.update(
            {
                "status": listing_json.get("status"),
                "statusReason": listing_json.get("status_reason") or "",
                "collection": tree.subdir.split("/")[0]
                if tree.subdir
                else listing_json.get("collection"),
                "original": {
                    k: v
                    for k, v in dict(
                        original, short=short_citation(original.get("citation"))
                    ).items()
                    if k in ("citation", "year", "doi", "url", "experiments", "short")
                    and v is not None
                },
                "authors": listing_json.get("authors") or [],
                "counts": {
                    "assumed": len(doc.get("assumed") or []),
                    "deviations": len(doc.get("deviations") or []),
                },
                "licenses": doc.get("license") or {},
            }
        )
    elif tree.subdir:
        listing["collection"] = tree.subdir.split("/")[0]
    if art_image:
        listing["artImage"] = art_image
    listing.update(over)
    listing = {k: v for k, v in listing.items() if v is not None}

    docs: dict[str, Any] = {
        "methods": methods,
        "readme": readme[:README_LIMIT],
        "references": json.dumps(doc.get("references") or []),
        "tasks": json.dumps(tasks),
    }
    if listing_json or doc.get("assumed") or doc.get("deviations"):
        docs["assumed"] = json.dumps(doc.get("assumed") or [])
        docs["deviations"] = json.dumps(doc.get("deviations") or [])

    parameter_guide = _parameter_guide(exp, tree, doc, docsource, library, listing, warnings)
    if parameter_guide is not None:
        docs["parameterGuide"] = json.dumps(parameter_guide, ensure_ascii=False)
    guide = _reader_guide(listing, docs, tree, doc_source, doc, overlay, warnings)
    if guide is not None:
        docs["guide"] = json.dumps(guide, ensure_ascii=False)
        diagram_task = next((task for task in guide.get("tasks", []) if task.get("diagram")), None)
        if diagram_task is not None:
            listing["methodDiagram"] = json.dumps(diagram_task["diagram"], ensure_ascii=False)
            listing["methodDiagramBundle"] = bundle.id
    if warnings:
        docs["warnings"] = list(warnings)
    return BuiltVersion(exp, bundle, listing, docs, warnings)


def _parameter_guide(
    exp: str,
    tree: SourceTree,
    doc: Mapping[str, Any],
    docsource: str,
    library: Mapping[str, Any] | None,
    listing: Mapping[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    if "run.py" not in tree.files:
        warnings.append("No run.py: there are no registered parameter sets to describe")
        return None
    from alhazen.experiment_platform import parameters

    try:
        return parameters.build_parameter_guide(
            exp,
            tree.files,
            source_commit=tree.commit,
            docs=dict(doc) if doc else None,
            docs_origin=docsource,
            library=library,
            source_root=tree.subdir,
            pinned=listing.get("alhazen") or None,
            listed_sets=list(listing.get("parameterSets") or []),
            listed_ladders=list(listing.get("ladders") or []),
        )
    except PlatformError as error:
        warnings.append(f"The parameter guide could not be built: {error}")
        return None


def _reader_guide(
    listing: Mapping[str, Any],
    docs: Mapping[str, Any],
    tree: SourceTree,
    doc_source: SourceTree,
    doc: Mapping[str, Any],
    overlay: Mapping[str, Any] | None,
    warnings: list[str],
) -> dict[str, Any] | None:
    if not doc.get("tasks"):
        return None
    from alhazen.experiment_platform import guides

    files = dict(tree.files)
    if doc_source is not tree:
        files.update({p: b for p, b in doc_source.files.items() if p.startswith("docs/")})
    try:
        guide = guides.build_guide(
            dict(listing), dict(docs), files, descriptor=dict(doc), overlay=overlay
        )
    except PlatformError as error:
        warnings.append(f"The task guides could not be resolved for this version: {error}")
        return None
    warnings.extend(str(w) for w in guide.get("warnings") or [] if "overlay" in str(w))
    return guide


def docs_status(listing: Mapping[str, Any], docs: Mapping[str, Any] | None) -> str:
    """``current``, ``partial``, ``stale`` or ``missing`` (design.md §4)."""
    if not docs:
        return "missing"
    bundle = (listing.get("bundle") or {}).get("id")
    source = listing.get("source") or {}
    commits = {c for c in (source.get("commit"), source.get("repoCommit")) if c}
    stale = False
    guide_ok = params_ok = False
    try:
        guide = json.loads(docs["guide"]) if docs.get("guide") else None
        params = json.loads(docs["parameterGuide"]) if docs.get("parameterGuide") else None
    except (TypeError, ValueError):
        return "stale"
    if guide is not None:
        if guide.get("bundleId") == bundle:
            guide_ok = True
        else:
            stale = True
    if params is not None:
        if params.get("sourceCommit") in commits:
            params_ok = True
        else:
            stale = True
    if stale:
        return "stale"
    if guide_ok and params_ok:
        return "current"
    if (
        params_ok
        or guide_ok
        or docs.get("methods")
        or docs.get("tasks") not in (None, "[]")
        or docs.get("readme")
    ):
        return "partial"
    return "missing"
