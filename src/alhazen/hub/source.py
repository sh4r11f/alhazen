"""Packaging a registered experiment's source for the hub: preview, then pack.

Secret it hides: how the proposed file list, the exclusion summary and the
metadata suggestion are put together for the operator to review, and how an
approved selection becomes a release upload. The archive itself (which paths
are allowed, sensitive-file refusal, hashing, deterministic ZIP) is entirely
``alhazen.hub.packages``; nothing here re-checks it.

The rule this module enforces: only files the operator saw in the preview
(``suggest_files``) can be packed, and packing never publishes.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Protocol

from alhazen.config.experiment import experiment_title
from alhazen.hub.client import HubClient, api_path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

# How much of an experiment folder the exclusion summary walks and lists.
MAX_WALK_ENTRIES = 50_000
MAX_EXCLUDED_LISTED = 200
DOCUMENTATION_PATH = "docs/experiment.json"

METADATA_FIELDS = (
    "name",
    "version",
    "title",
    "description",
    "license",
    "citations",
    "hardware",
    "python_min",
    "alhazen_min",
    "platforms",
    "documentation",
)
DEFAULT_PLATFORMS = ["linux", "darwin", "win32"]


class PackageBuilder(Protocol):
    PackageError: type[Exception]

    def suggest_files(self, source: Path) -> list[str]: ...

    def build_bundle(
        self, source: Path, output: Path, metadata: dict[str, Any], files: list[str]
    ) -> Any: ...


def _reason(relative: str) -> str:
    """Why a file is probably not in the proposal, in words, for the summary.
    Informational only: the package module decides."""
    parts = relative.split("/")
    top = parts[0]
    name = parts[-1]
    if any(p in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") for p in parts):
        return "cache"
    if top in (".venv", "venv", "env", ".tox", "node_modules"):
        return "environment"
    if top.startswith("data") or top in ("runs", "sessions"):
        return "collected data"
    if name.startswith("rig-") and name.endswith((".yaml", ".yml")):
        return "rig-specific configuration"
    if any(p.startswith(".") for p in parts):
        return "hidden file"
    if name.endswith((".sqlite3", ".db", ".edf", ".tsv")):
        return "data or registry file"
    return "not proposed (untracked or excluded)"


def _excluded(root: Path, chosen: set[str]) -> tuple[list[dict[str, str]], int]:
    listed: list[dict[str, str]] = []
    count = 0
    seen = 0
    for directory, dirs, names in os.walk(root):
        here = Path(directory)
        if ".git" in dirs:
            dirs.remove(".git")
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        for name in sorted(names):
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                return listed, count
            relative = (here / name).relative_to(root).as_posix()
            if relative in chosen:
                continue
            count += 1
            if len(listed) < MAX_EXCLUDED_LISTED:
                listed.append({"path": relative, "reason": _reason(relative)})
    return listed, count


def _alhazen_floor(dependencies: Any) -> str | None:
    """The ``>=`` floor of an ``alhazen-vision`` requirement, if declared."""
    for spec in dependencies if isinstance(dependencies, list) else []:
        match = re.match(r"\s*alhazen-vision\b[^;]*?>=\s*([0-9][0-9.]*)", str(spec))
        if match:
            return match.group(1).rstrip(".")
    return None


def suggest_metadata(root: Path, files: list[str] | None = None) -> dict[str, Any]:
    """A starting point from pyproject.toml, read as text (never imported).

    ``files``: the files proposed for the package; the documentation pointer
    is suggested only when the descriptor is one of them."""
    project: dict[str, Any] = {}
    try:
        document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        project = document.get("project") or {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        project = {}
    naming = experiment_title(root)
    slug = re.sub(r"[^a-z0-9-]+", "-", naming.slug.lower()).strip("-")[:64] or "experiment"
    licence = project.get("license")
    if isinstance(licence, dict):
        licence = licence.get("text") or ""
    # The documentation descriptor's conventional place (docs/hub/
    # documentation.md): pointed at when it is packed, so `alhazen hub pack`
    # ships documentation the hub reads rather than an unread file.
    documented = DOCUMENTATION_PATH in (files or [])
    return {
        "name": slug,
        "version": str(project.get("version") or "0.1.0"),
        "title": naming.title,
        "description": str(project.get("description") or ""),
        "license": licence if isinstance(licence, str) else "",
        "citations": [],
        "hardware": {"display": True, "eye_tracker": False, "reward": False},
        "python_min": "3.10",
        "alhazen_min": _alhazen_floor(project.get("dependencies")) or "2.13.0",
        "platforms": list(DEFAULT_PLATFORMS),
        "entrypoint": "run.py",
        **({"documentation": DOCUMENTATION_PATH} if documented else {}),
    }


def preview(root: Path, packages: PackageBuilder) -> dict[str, Any]:
    """The proposed files with sizes, what was left out, and metadata."""
    proposed = packages.suggest_files(root)
    files: list[dict[str, Any]] = []
    for relative in proposed:
        path = root / relative
        if path.is_file() and not path.is_symlink():
            files.append({"path": relative, "size": path.stat().st_size})
    excluded, excluded_count = _excluded(root, {str(f["path"]) for f in files})
    return {
        "files": files,
        "total_bytes": sum(f["size"] for f in files),
        "excluded": excluded,
        "excluded_count": excluded_count,
        "metadata": suggest_metadata(root, [str(f["path"]) for f in files]),
    }


def clean_metadata(metadata: Any) -> dict[str, Any]:
    """The operator's metadata, limited to the manifest's fields and plain
    types; the package module validates the values themselves."""
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    unknown = set(metadata) - set(METADATA_FIELDS) - {"entrypoint", "schema_version"}
    if unknown:
        raise ValueError(f"Unknown metadata fields: {', '.join(sorted(unknown))}")
    if metadata.get("entrypoint", "run.py") != "run.py":
        raise ValueError("The entry point is always run.py")
    clean = {k: metadata[k] for k in METADATA_FIELDS if k in metadata}
    for key in ("name", "version", "title", "license"):
        if not isinstance(clean.get(key), str) or not clean[key].strip():
            raise ValueError(f"metadata.{key} is required text")
    if not isinstance(clean.get("description", ""), str):
        raise ValueError("metadata.description must be text")
    clean.setdefault("description", "")
    clean.setdefault("citations", [])
    clean.setdefault("platforms", list(DEFAULT_PLATFORMS))
    clean.setdefault("python_min", "3.10")
    clean.setdefault("alhazen_min", "2.13.0")
    clean.setdefault("hardware", {"display": False, "eye_tracker": False, "reward": False})
    clean["entrypoint"] = "run.py"
    clean["schema_version"] = 1
    return clean


def pack(
    root: Path,
    packages: PackageBuilder,
    output: Path,
    metadata: Any,
    files: Any,
) -> Any:
    """Build a bundle from exactly ``files``, each one the preview proposed."""
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise ValueError("Choose the files to include")
    if len(set(files)) != len(files):
        raise ValueError("A file was chosen twice")
    proposed = set(packages.suggest_files(root))
    outside = sorted(set(files) - proposed)
    if outside:
        raise ValueError(
            "Only files from the preview can be packed; not proposed: " + ", ".join(outside[:10])
        )
    if "run.py" not in files:
        raise ValueError("run.py must be included")
    return packages.build_bundle(root, output, clean_metadata(metadata), sorted(files))


def pack_and_upload(
    client: HubClient,
    root: Path,
    packages: PackageBuilder,
    scratch: Path,
    *,
    experiment_id: str,
    metadata: Any,
    files: Any,
) -> Any:
    """Pack the approved selection and upload it as a new private release of
    ``experiment_id``. Never publishes. The temporary bundle is removed."""
    scratch.mkdir(parents=True, exist_ok=True)
    output = scratch / f"package-{secrets.token_hex(6)}.zip"
    try:
        info = pack(root, packages, output, metadata, files)
        with output.open("rb") as stream:
            return client.json(
                "POST",
                api_path("experiments", experiment_id, "versions"),
                data=stream,
                length=int(info.size),
                content_type="application/zip",
            )
    finally:
        output.unlink(missing_ok=True)
