"""Refresh: rebuild catalogue versions from experiment source, and check a catalogue.

The source axis of design.md §4. ``refresh`` reads each experiment at an exact commit,
builds its version (bundle, listing, documentation bound to that commit and bundle) and
stores it; an unchanged experiment writes nothing. Earlier versions and every private
copy stay exactly as they were: a version is immutable, the listing pointer moves last.
``check`` is the release/CI gate: it fails when a listing's documentation is stale or
missing, or when the source has moved past the listing.

The sources can be named on the command line or in a TOML file::

    [[experiment]]
    repo = "../kde-vergence"        # relative to the TOML file
    ref = "origin/main"
    docs_ref = "hub/import"         # optional: documentation proposed on another ref
    repo_label = "lab/kde-vergence"  # optional: shown as the source repository
    overlay = "overlays/kde.json"   # optional: a guide overlay bound to one bundle id
    [experiment.overrides]          # optional: what the repository does not say itself
    art = "cylinder"
    featured = true

    [[collection]]
    repo = "../alhazen-experiments"
    ref = "main"
    folders = ["classics/stroop-1935"]   # optional: default every experiment folder
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.experiment_platform import catalog, sources
from alhazen.experiment_platform.errors import InvalidRequest, PlatformError, SourceError
from alhazen.experiment_platform.store import FileStore

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib


@dataclass
class SourceSpec:
    """One experiment to refresh: a repository (or a folder of a collection) at a ref."""

    repo: Path
    ref: str = "HEAD"
    subdir: str = ""
    exp_id: str | None = None
    docs_ref: str | None = None
    repo_label: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    overlay: dict[str, Any] | None = None


@dataclass
class RefreshResult:
    exp: str
    publisher: str
    bundle_id: str
    commit: str
    status: str  # "created" (new version), "reused" (an earlier version again), "unchanged"
    docs_status: str
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        note = (
            f" ({len(self.warnings)} warning{'s' if len(self.warnings) != 1 else ''})"
            if self.warnings
            else ""
        )
        head = f"{self.publisher}/{self.exp}: {self.status} {self.bundle_id} at {self.commit[:7]}"
        return f"{head}, docs {self.docs_status}{note}"


def repo_label_of(repo: Path) -> str:
    """``owner/name`` from the origin remote when it is a hosted URL, else the folder name.
    Credentials in a URL are never kept."""
    try:
        url = sources._git(Path(repo), "remote", "get-url", "origin").decode().strip()
    except SourceError:
        return Path(repo).resolve().name
    m = re.search(r"[:/]([^/:@]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else Path(repo).resolve().name


def _overlay_file(path: Path) -> dict[str, Any]:
    from alhazen.experiment_platform import guides

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InvalidRequest(f"The guide overlay {path.name} could not be read: {error}") from error
    overlay = guides.parse_overlay(raw)
    if not overlay or not isinstance(overlay.get("bundleId"), str):
        raise InvalidRequest(f"The guide overlay {path.name} has no bundleId")
    return overlay


def load_config(path: Path) -> list[SourceSpec]:
    """The sources a TOML file names, collections expanded later by ``expand``."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InvalidRequest(f"{path.name} could not be read: {error}") from error
    base = path.parent
    specs: list[SourceSpec] = []
    for item in data.get("experiment", []):
        if "repo" not in item:
            raise InvalidRequest(f"{path.name}: every [[experiment]] needs repo")
        specs.append(
            SourceSpec(
                repo=(base / item["repo"]).resolve(),
                ref=item.get("ref", "HEAD"),
                subdir=item.get("folder", ""),
                exp_id=item.get("id"),
                docs_ref=item.get("docs_ref"),
                repo_label=item.get("repo_label"),
                overrides=dict(item.get("overrides", {})),
                overlay=_overlay_file(base / item["overlay"]) if item.get("overlay") else None,
            )
        )
    for item in data.get("collection", []):
        if "repo" not in item:
            raise InvalidRequest(f"{path.name}: every [[collection]] needs repo")
        repo = (base / item["repo"]).resolve()
        ref = item.get("ref", "HEAD")
        folders = item.get("folders") or sources.collection_folders(repo, ref)
        overlays = {}
        for name, rel in (item.get("overlays") or {}).items():
            overlays[name] = _overlay_file(base / rel)
        for folder in folders:
            exp = folder.rstrip("/").split("/")[-1]
            specs.append(
                SourceSpec(
                    repo=repo,
                    ref=ref,
                    subdir=folder,
                    repo_label=item.get("repo_label"),
                    overlay=overlays.get(exp),
                )
            )
    return specs


_LIBRARY_CACHE: dict[tuple[str, str], dict[str, bytes] | None] = {}


def alhazen_library(tag: str, alhazen_repo: Path | None) -> dict[str, bytes] | None:
    """alhazen's ``src`` files at ``tag`` ({"alhazen/...py": bytes}): from a git clone of
    alhazen when one is given (preferred: exactly the tag), else from the installed alhazen
    when its version is the tag's. None when neither is available (the parameter guide
    then says that defaults inherited from alhazen are not listed)."""
    if not tag:
        return None
    key = (str(alhazen_repo or ""), tag)
    if key in _LIBRARY_CACHE:
        return _LIBRARY_CACHE[key]
    from alhazen.experiment_platform import parameters

    found: dict[str, bytes] | None = None
    if alhazen_repo is not None:
        try:
            found = parameters.library_files(sources.read_tree(alhazen_repo, tag, "src"))
        except PlatformError:
            found = None
    if found is None:
        files, version = parameters.installed_library()
        if parameters.pin_matches(version, tag):
            found = files
    _LIBRARY_CACHE[key] = found
    return found


def _art_writer(store: FileStore) -> Callable[[str, bytes, str], str]:
    def write(name: str, data: bytes, suffix: str) -> str:
        return store.put_art(name, data, suffix)

    return write


def refresh(
    store: FileStore,
    specs: Iterable[SourceSpec],
    publisher: Mapping[str, str],
    *,
    alhazen_repo: Path | None = None,
    force_docs_ref_check: bool = True,
    now_ms: int | None = None,
) -> list[RefreshResult]:
    """Refresh every source; one failure does not stop the others (it is raised after
    the rest, naming each failed experiment)."""
    results: list[RefreshResult] = []
    failures: list[str] = []
    for spec in specs:
        try:
            results.append(
                refresh_one(store, spec, publisher, alhazen_repo=alhazen_repo, now_ms=now_ms)
            )
        except PlatformError as error:
            where = spec.subdir or spec.repo.name
            failures.append(f"{where}: {error}")
    if failures:
        raise SourceError("Some experiments could not be refreshed:\n  " + "\n  ".join(failures))
    return results


def refresh_one(
    store: FileStore,
    spec: SourceSpec,
    publisher: Mapping[str, str],
    *,
    alhazen_repo: Path | None = None,
    now_ms: int | None = None,
) -> RefreshResult:
    tree = sources.read_tree(spec.repo, spec.ref, spec.subdir)
    docs_tree = sources.read_tree(spec.repo, spec.docs_ref, spec.subdir) if spec.docs_ref else None
    label = spec.repo_label or repo_label_of(spec.repo)
    try:
        pushed: bool | None = sources.is_pushed(spec.repo, tree.commit)
    except SourceError:
        pushed = None
    tag = catalog.alhazen_tag(tree.text("pyproject.toml"))
    built = catalog.build_version(
        tree,
        publisher=publisher,
        exp_id=spec.exp_id,
        repo_label=label,
        overrides=spec.overrides,
        docs_tree=docs_tree,
        overlay=spec.overlay,
        library=alhazen_library(tag, alhazen_repo),
        store_art=_art_writer(store),
        pushed=pushed,
        now_ms=now_ms,
    )
    pub = built.listing["publisher"]["uid"]
    t = now_ms if now_ms is not None else int(time.time() * 1000)
    from alhazen.version import get_version

    bid = built.bundle.id
    docs_commit = (built.listing.get("source") or {}).get("docsCommit")
    # A version is the files plus the documentation describing them. Documentation
    # normally comes from the same commit, so the bundle id names the version; when it is
    # read from another ref, that ref's commit is part of the version's key.
    key = bid if not docs_commit else f"{bid}.d{docs_commit[:12]}"
    with store.lock():
        current = store.listing(pub, built.exp)
        if current and current.get("versionKey", (current.get("bundle") or {}).get("id")) == key:
            version = store.version(pub, built.exp, key) or {}
            return RefreshResult(
                built.exp,
                pub,
                bid,
                tree.commit,
                "unchanged",
                catalog.docs_status(current, version.get("docs")),
                built.warnings,
            )
        record = store.version(pub, built.exp, key)
        if record is not None:
            status = "reused"
        else:
            status = "created"
            store.put_bundle(None, built.bundle)
            record = {
                "key": key,
                "bundleId": bid,
                "listing": built.listing,
                "docs": built.docs,
                "builtAt": t,
                "builtWith": {"alhazen": get_version()},
            }
            store.put_version(pub, built.exp, key, record)
        listing = dict(record["listing"])
        versions = dict((current or {}).get("versions") or {})
        commit7 = str(listing["source"].get("commit", ""))[:7]
        # History is keyed by version (files plus documentation), so a documentation-only
        # refresh adds an entry rather than overwriting the earlier one.
        versions[key] = {
            "version": listing.get("version", ""),
            "commit": commit7,
            "at": (versions.get(key) or {}).get("at", t),
            "key": key,
            "bundle": bid,
        }
        if docs_commit:
            versions[key]["docsCommit"] = str(docs_commit)[:7]
        listing["versions"] = versions
        listing["versionKey"] = key
        listing["publishedAt"] = (
            (current or {}).get("publishedAt") or listing.get("publishedAt") or t
        )
        listing["updatedAt"] = t
        store.put_listing(pub, built.exp, listing)
        return RefreshResult(
            built.exp,
            pub,
            bid,
            tree.commit,
            status,
            catalog.docs_status(listing, record.get("docs")),
            built.warnings,
        )


@dataclass
class CheckProblem:
    where: str
    problem: str


def check(
    store: FileStore, specs: Iterable[SourceSpec] = (), *, publisher: str | None = None
) -> list[CheckProblem]:
    """Problems that should block a release: every listing whose documentation is stale
    or missing, and every named source whose files have moved past its listing."""
    problems: list[CheckProblem] = []
    for pub, by_exp in store.listings().items():
        for exp, listing in by_exp.items():
            if (listing.get("source") or {}).get("kind") != "repo":
                continue
            key = listing.get("versionKey") or (listing.get("bundle") or {}).get("id")
            version = store.version(pub, exp, key) if key else None
            status = catalog.docs_status(listing, (version or {}).get("docs"))
            if status in ("stale", "missing"):
                problems.append(CheckProblem(f"{pub}/{exp}", f"documentation is {status}"))
    for spec in specs:
        tree = sources.read_tree(spec.repo, spec.ref, spec.subdir)
        exp = catalog.experiment_id(tree, spec.exp_id)
        pubs = [publisher] if publisher else list(store.listings())
        found: dict[str, Any] | None = None
        for p in pubs:
            found = found or store.listing(p, exp)
        if found is None:
            problems.append(CheckProblem(exp, "not in the catalogue"))
            continue
        from alhazen.experiment_platform.bundles import build_bundle, split_blobs

        entries, excluded = split_blobs(tree.blobs())
        if build_bundle(entries, excluded, exp).id != found["bundle"]["id"]:
            problems.append(
                CheckProblem(
                    exp,
                    f"the source at {tree.commit[:7]} differs from the listed version; refresh it",
                )
            )
    return problems
