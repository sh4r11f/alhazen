"""Bundles: an experiment's files at one version, content-addressed.

A bundle is the unit the platform stores, forks, publishes and hands to a rig. Its format
is the one the Mana platform already uses, so the same source gives the same bundle on
every host:

- an entry is ``[path, text, executable]`` for a UTF-8 text file, or ``[path, base64,
  executable, "b64"]`` for a binary file kept in the bundle (at most BINARY_FILE_MAX bytes
  each and BINARY_TOTAL_MAX per bundle; larger binaries are listed in ``excluded`` and stay
  in the source repository);
- entries are stored in chunks of about CHUNK_BYTES, each a JSON string;
- ``bundle_id`` is ``<prefix>-<10 hex>`` of a SHA-256 over the entries; the full digest is
  recorded as ``meta["sha256"]`` and verified whenever a bundle is read back.

The decision this module hides is what makes a set of files safe to store and to write
to disk: every path goes through ``safe_relative``, entries are unique, counts and sizes
are bounded, and materializing refuses to write outside the destination.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import subprocess
import time
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alhazen.experiment_platform.errors import BundleError
from alhazen.experiment_platform.safety import check_id, safe_relative

FORMAT = 1
CHUNK_BYTES = 400_000
BINARY_FILE_MAX = 2_000_000
BINARY_TOTAL_MAX = 12_000_000
MAX_FILES = 4000
MAX_TEXT_BYTES = 64 * 1024 * 1024
# A zip upload may not expand beyond this, whatever its compressed size.
MAX_ARCHIVE_EXPANDED = MAX_TEXT_BYTES + BINARY_TOTAL_MAX + 64 * 1024 * 1024

Entry = list  # [path, text-or-base64, 0|1] or [path, base64, 0|1, "b64"]
Blob = tuple  # (path, raw bytes, executable 0|1)

# Folders a directory snapshot leaves out: version control, environments, caches and
# recorded data (a bundle is source, never session data).
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".alhazen-hub",
    ".alhazen",
}


def is_binary_entry(entry: Sequence[Any]) -> bool:
    return len(entry) > 3 and entry[3] == "b64"


def entry_bytes(entry: Sequence[Any]) -> bytes:
    """The file's content as bytes."""
    if is_binary_entry(entry):
        try:
            return base64.b64decode(entry[1], validate=True)
        except (ValueError, TypeError) as error:
            raise BundleError(f"File {entry[0]!r} is not valid base64") from error
    return str(entry[1]).encode("utf-8")


def split_blobs(blobs: Iterable[Blob]) -> tuple[list[Entry], list[list[Any]]]:
    """``[(path, raw, executable)]`` -> ``(entries, excluded)``: text stays text, binaries
    within the ceilings become base64 entries, the rest are excluded ``[path, bytes]``.
    Paths are checked; entries keep the order given (callers sort)."""
    entries: list[Entry] = []
    excluded: list[list[Any]] = []
    total = 0
    seen: set[str] = set()
    for path, raw, executable in blobs:
        safe_relative(path)
        if path in seen:
            raise BundleError(f"File {path!r} appears twice")
        seen.add(path)
        text = None
        if b"\0" not in raw:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        x = 1 if executable else 0
        if text is not None:
            entries.append([path, text, x])
        elif len(raw) <= BINARY_FILE_MAX and total + len(raw) <= BINARY_TOTAL_MAX:
            entries.append([path, base64.b64encode(raw).decode("ascii"), x, "b64"])
            total += len(raw)
        else:
            excluded.append([path, len(raw)])
    _check_bounds(entries)
    return entries, excluded


def _check_bounds(entries: Sequence[Entry]) -> None:
    if len(entries) > MAX_FILES:
        raise BundleError(
            f"The experiment has {len(entries)} files; at most {MAX_FILES} fit in a bundle"
        )
    text = sum(len(str(e[1])) for e in entries if not is_binary_entry(e))
    if text > MAX_TEXT_BYTES:
        raise BundleError(
            f"The experiment's text files total {text} bytes; at most {MAX_TEXT_BYTES} fit"
        )


def _digest(entries: Sequence[Entry]) -> hashlib._Hash:
    h = hashlib.sha256()
    for e in entries:
        path, text, x = e[0], e[1], e[2]
        h.update(str(path).encode())
        h.update(b"\0")
        h.update(str(text).encode())
        h.update(str(x).encode())
        if is_binary_entry(e):
            h.update(b"\0b64")
    return h


def bundle_sha256(entries: Sequence[Entry]) -> str:
    return _digest(entries).hexdigest()


def bundle_id(entries: Sequence[Entry], prefix: str) -> str:
    """``<prefix>-<10 hex>``: the Mana kit's id for the same entries."""
    check_id(prefix, "Bundle prefix")
    return f"{prefix}-{_digest(entries).hexdigest()[:10]}"


@dataclass(frozen=True)
class Bundle:
    """A built bundle: its id, its meta record and its chunks (JSON strings)."""

    id: str
    meta: dict[str, Any]
    chunks: tuple[str, ...]

    def entries(self) -> list[Entry]:
        return read_entries(self.meta, self.chunks)


def build_bundle(
    entries: Sequence[Entry],
    excluded: Sequence[Sequence[Any]],
    prefix: str,
    extra: dict[str, Any] | None = None,
) -> Bundle:
    """Chunk ``entries`` and describe them. Entries are sorted by path so the id does not
    depend on the order a source listed its files."""
    ordered = sorted((list(e) for e in entries), key=lambda e: e[0])
    _check_bounds(ordered)
    seen: set[str] = set()
    for e in ordered:
        safe_relative(e[0])
        if e[0] in seen:
            raise BundleError(f"File {e[0]!r} appears twice")
        seen.add(e[0])
    chunks: list[list[Entry]] = []
    current: list[Entry] = []
    size = 0
    for e in ordered:
        n = len(e[0]) + len(e[1]) + 16
        if current and size + n > CHUNK_BYTES:
            chunks.append(current)
            current, size = [], 0
        current.append(e)
        size += n
    if current:
        chunks.append(current)
    sizes = [len(entry_bytes(e)) for e in ordered]
    binaries = [s for e, s in zip(ordered, sizes, strict=True) if is_binary_entry(e)]
    bid = bundle_id(ordered, prefix)
    meta: dict[str, Any] = {
        "format": FORMAT,
        "id": bid,
        "sha256": bundle_sha256(ordered),
        "files": json.dumps([[e[0], s] for e, s in zip(ordered, sizes, strict=True)]),
        "excluded": json.dumps([list(x) for x in excluded]),
        "fileCount": len(ordered),
        "bytes": sum(sizes),
        "chunkCount": len(chunks),
        "binaries": len(binaries),
        "binaryBytes": sum(binaries),
        "createdAt": int(time.time() * 1000),
    }
    meta.update(extra or {})
    return Bundle(bid, meta, tuple(json.dumps(c, ensure_ascii=False) for c in chunks))


def read_entries(meta: dict[str, Any], chunks: Sequence[str]) -> list[Entry]:
    """The entries of a stored bundle, verified: chunk count, entry shape, paths, file list
    and (when recorded) the full digest. Raises BundleError on any mismatch."""
    if not isinstance(meta, dict):
        raise BundleError("The bundle has no description")
    if int(meta.get("chunkCount") or 0) != len(chunks):
        raise BundleError("The bundle is incomplete: chunks are missing")
    entries: list[Entry] = []
    for i, chunk in enumerate(chunks):
        try:
            part = json.loads(chunk)
        except (TypeError, ValueError) as error:
            raise BundleError(f"Bundle chunk {i} is not valid JSON") from error
        if not isinstance(part, list):
            raise BundleError(f"Bundle chunk {i} is not a list of files")
        for e in part:
            if (
                not isinstance(e, list)
                or len(e) not in (3, 4)
                or not isinstance(e[1], str)
                or e[2] not in (0, 1)
                or (len(e) == 4 and e[3] != "b64")
            ):
                raise BundleError(f"Bundle chunk {i} has a malformed entry")
            safe_relative(e[0])
            entries.append(e)
    names = [e[0] for e in entries]
    if len(set(names)) != len(names):
        raise BundleError("The bundle lists a file twice")
    _check_bounds(entries)
    try:
        listed = [p for p, _ in json.loads(meta.get("files") or "[]")]
    except (TypeError, ValueError) as error:
        raise BundleError("The bundle's file list is not valid") from error
    if listed and listed != names:
        raise BundleError("The bundle's files do not match its file list")
    recorded = meta.get("sha256")
    if recorded and recorded != bundle_sha256(entries):
        raise BundleError(
            "The bundle's files do not match its digest: it is corrupt or was altered"
        )
    bid = meta.get("id")
    if isinstance(bid, str) and "-" in bid and bid.rsplit("-", 1)[1] != bundle_sha256(entries)[:10]:
        raise BundleError("The bundle's files do not match its id")
    return entries


def materialize(entries: Sequence[Entry], dest: Path) -> Path:
    """Write ``entries`` under ``dest`` (created if missing). Refuses any path that would
    resolve outside ``dest`` (including through an existing symlink) before writing."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    targets = []
    for e in entries:
        rel = safe_relative(e[0])
        target = root / rel
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise BundleError(f"File {rel!r} would be written outside the destination")
        if target.is_symlink():
            raise BundleError(f"File {rel!r} would replace a symbolic link")
        targets.append((target, e))
    for target, e in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise BundleError(f"File {e[0]!r} would be written outside the destination")
        target.write_bytes(entry_bytes(e))
        if e[2]:
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def zip_entries(entries: Sequence[Entry], folder: str) -> bytes:
    """The entries as a zip archive with everything under ``folder/``."""
    check_id(folder, "Archive folder")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for e in entries:
            info = zipfile.ZipInfo(f"{folder}/{e[0]}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((0o755 if e[2] else 0o644) | stat.S_IFREG) << 16
            archive.writestr(info, entry_bytes(e))
    return out.getvalue()


def blobs_from_zip(raw: bytes, max_expanded: int = MAX_ARCHIVE_EXPANDED) -> list[Blob]:
    """The files of an uploaded zip archive, checked. A single top-level folder that holds
    everything (how archives are usually made) is stripped. Refuses symbolic links,
    encrypted entries, unsafe paths, duplicates and archives that expand beyond
    ``max_expanded`` bytes; skips the folders a snapshot leaves out (.git, .venv, ...)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise BundleError("The upload is not a zip archive") from error
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES * 2:
            raise BundleError("The archive has too many files")
        total = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise BundleError(f"{info.filename!r} is encrypted")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise BundleError(f"{info.filename!r} is a symbolic link; links are not accepted")
            total += info.file_size
            if total > max_expanded:
                raise BundleError("The archive expands beyond the size limit")
        names = [i.filename for i in infos]
        prefix = ""
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and all("/" in n for n in names):
            prefix = next(iter(tops)) + "/"
        blobs: list[Blob] = []
        seen: set[str] = set()
        for info in infos:
            name = info.filename[len(prefix) :]
            first = name.split("/", 1)[0]
            if first in _SKIP_DIRS or "/__pycache__/" in "/" + name:
                continue
            path = safe_relative(name)
            if path in seen:
                raise BundleError(f"File {path!r} appears twice in the archive")
            seen.add(path)
            with archive.open(info) as handle:
                data = handle.read(max_expanded + 1)
            if len(data) != info.file_size:
                raise BundleError(f"{path!r} does not match its recorded size")
            mode = info.external_attr >> 16
            blobs.append((path, data, 1 if mode & stat.S_IXUSR else 0))
    return sorted(blobs, key=lambda b: b[0])


def blobs_from_dir(root: Path) -> list[Blob]:
    """The files of a folder: the git-tracked files when it is a git work tree, otherwise
    every regular file outside version-control, environment and data folders. Symbolic
    links are refused, never followed."""
    root = Path(root)
    if not root.is_dir():
        raise BundleError(f"{root.name!r} is not a folder")
    names: list[str]
    if (root / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False)
        if out.returncode != 0:
            raise BundleError(
                "git could not list the folder's files: "
                + out.stderr.decode(errors="replace")[:300]
            )
        names = [n for n in out.stdout.decode("utf-8").split("\0") if n]
    else:
        names = []
        for current, dirs, files in os.walk(root):
            rel_dir = Path(current).relative_to(root).as_posix()
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in _SKIP_DIRS
                and not (rel_dir == "." and (d == "data" or d.startswith("data-")))
            )
            for f in sorted(files):
                rel = f if rel_dir == "." else f"{rel_dir}/{f}"
                names.append(rel)
    blobs: list[Blob] = []
    for name in sorted(names):
        path = root / name
        if path.is_symlink():
            raise BundleError(f"{name!r} is a symbolic link; links are not accepted")
        if not path.is_file():
            continue  # deleted from the work tree but still in the index
        blobs.append((safe_relative(name), path.read_bytes(), 1 if os.access(path, os.X_OK) else 0))
    return blobs
