"""The run manifest: every artifact a run produced, hashed.

Written last at teardown, after all other files. The manifest is the anchor
for provenance and later syncing/archiving: a run directory whose files match
its manifest is complete and untampered; one that doesn't tells you exactly
which file to distrust.

Two ways to write one, for two different moments:

- `write_manifest` hashes everything in the directory. The session's
  teardown calls it once, when every file there is the session's own.
- `add_to_manifest` records the files a later step wrote (a report, a saved
  alignment) and leaves every other entry as the session recorded it. That
  is what keeps a damaged file detectable: re-hashing the whole directory
  after a file changed would record the damage as the truth.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(run_dir: Path, path: Path) -> dict[str, object]:
    """One file's manifest entry."""
    return {
        # Forward slashes on every platform: the manifest is part of the
        # run's record, and a run written on Windows must verify on the
        # machine that analyses it.
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _write(manifest_path: Path, artifacts: list[dict[str, object]]) -> None:
    manifest_path.write_text(
        yaml.safe_dump(
            {"schema_version": MANIFEST_SCHEMA_VERSION, "artifacts": artifacts},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def write_manifest(run_dir: Path, manifest_path: Path) -> None:
    """Hash every file in ``run_dir`` into a new manifest.

    For the session's own teardown. Anything written into a finished run
    afterwards goes through `add_to_manifest` instead: this function would
    also re-hash a file that has changed since, and record the change as
    what the session wrote.
    """
    artifacts = [
        _entry(run_dir, path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path != manifest_path
    ]
    _write(manifest_path, artifacts)


def add_to_manifest(run_dir: Path, manifest_path: Path, written: Iterable[Path]) -> None:
    """Record files just written into a finished run, and touch nothing else.

    Each file in ``written`` gets its entry — added, or replaced when an
    earlier save wrote the same file (a report saved again). Every other
    entry keeps the hash the session recorded, so a file damaged since the
    session still fails `verify_manifest` after a report or an alignment has
    been saved beside it; and a file nobody recorded stays unlisted.

    A run with no manifest is left without one. Its session never finished
    teardown, which is what ``load_run`` reports ("manifest.yaml is
    missing"); a manifest made now would list whatever happens to be in the
    directory and call the run complete. The file is still written — only
    not recorded — and a warning says so.
    """
    files = [Path(path) for path in written]  # a list: it is walked twice
    if not manifest_path.exists():
        log.warning(
            "%s has no manifest (its session never finished teardown), so %s was not "
            "recorded in one; a manifest is not made after the fact",
            run_dir,
            ", ".join(path.name for path in files),
        )
        return
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, object]] = manifest["artifacts"]
    # Where each recorded path sits, so a rewritten file's entry is replaced
    # in place and the rest of the list stays exactly as it was.
    position = {entry["path"]: index for index, entry in enumerate(artifacts)}
    for path in files:
        entry = _entry(run_dir, path)
        index = position.get(entry["path"])
        if index is None:
            position[entry["path"]] = len(artifacts)
            artifacts.append(entry)
        else:
            artifacts[index] = entry
    _write(manifest_path, artifacts)


def verify_manifest(run_dir: Path, manifest_path: Path) -> list[str]:
    """Return a list of problems (empty = verified). Missing files and hash
    mismatches are reported; extra files are reported too — a run directory
    is append-only by manifest rewrite, never by unrecorded files."""
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
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
