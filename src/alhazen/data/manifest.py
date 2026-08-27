"""The run manifest: every artifact a run produced, hashed.

Written last at teardown, after all other files. The manifest is the anchor
for provenance and later syncing/archiving: a run directory whose files match
its manifest is complete and untampered; one that doesn't tells you exactly
which file to distrust.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(run_dir: Path, manifest_path: Path) -> None:
    artifacts = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path == manifest_path:
            continue
        artifacts.append(
            {
                # Forward slashes on every platform: the manifest is part
                # of the run's record, and a run written on Windows must
                # verify on the machine that analyses it.
                "path": path.relative_to(run_dir).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest_path.write_text(
        yaml.safe_dump(
            {"schema_version": MANIFEST_SCHEMA_VERSION, "artifacts": artifacts},
            sort_keys=False,
        )
    )


def verify_manifest(run_dir: Path, manifest_path: Path) -> list[str]:
    """Return a list of problems (empty = verified). Missing files and hash
    mismatches are reported; extra files are reported too — a run directory
    is append-only by manifest rewrite, never by unrecorded files."""
    manifest = yaml.safe_load(manifest_path.read_text())
    problems: list[str] = []
    listed: set[str] = set()
    for entry in manifest["artifacts"]:
        rel, expected = entry["path"], entry["sha256"]
        listed.add(rel)
        path = run_dir / rel
        if not path.exists():
            problems.append(f"missing: {rel}")
        elif _sha256(path) != expected:
            problems.append(f"hash mismatch: {rel}")
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path != manifest_path:
            rel = path.relative_to(run_dir).as_posix()
            if rel not in listed:
                problems.append(f"unlisted file: {rel}")
    return problems
