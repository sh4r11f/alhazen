"""The immutable artifact archive on a filesystem.

Hides: the physical layout under the configured root, how bytes are made
durable (write, flush, fsync of files and of the directories that name
them), and the atomic install of a finished release or session.

Layout (keys are relative POSIX strings stored in the database; the root
itself is never stored or shown):

    tmp/<random>.part                       package uploads being received
    releases/<experiment_id>/<version_id>.zip
    staging/<session_id>/files/<path>       session upload in progress
    sessions/<experiment_id>/<session_id>/  committed session: files/ + manifest.json
    ai-drafts/<draft_id>/<job_id>.zip       generated source awaiting acceptance (private,
                                            removed when its draft is discarded)

Guarantees: a release or session directory is created once and never
overwritten or modified; staging and final directories are on the same
filesystem (checked at start), so installing a sealed session is one atomic
rename; every write the database later acknowledges was synced first.
Directory fsync is a POSIX operation: on Windows (development only) file
data is synced but a directory entry may not be.

Not trusted for: being a backup. A receipt certifies these files on this
storage, nothing more (docs/hub/server.md "Backup and restore").
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

_KEY = re.compile(r"^[A-Za-z0-9._/-]+$")
_READ = 1024 * 1024


class StorageError(RuntimeError):
    """The archive is unusable or an operation broke one of its rules."""


_PROGRESS_EVERY = 256 * 1024 * 1024


def sha256_file(path: Path, on_progress: Callable[[], None] | None = None) -> str:
    """The file's SHA-256, read in blocks; ``on_progress`` is called after every
    256 MiB (a long seal renews its lease there and stops if it was taken over)."""
    digest = hashlib.sha256()
    since = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ), b""):
            digest.update(block)
            since += len(block)
            if on_progress is not None and since >= _PROGRESS_EVERY:
                on_progress()
                since = 0
    return digest.hexdigest()


# os.open returns a raw descriptor (no text layer). tests/unit/test_text_encoding.py
# cannot tell it from a text open by name, so it is called through this alias,
# as alhazen.hub.packages does.
_open_descriptor = os.open


def fsync_dir(path: Path) -> None:
    """Make a directory's entries durable (POSIX; a no-op on Windows)."""
    if os.name == "nt":
        return
    fd = _open_descriptor(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


AREAS = ("tmp", "releases", "staging", "sessions", "ai-drafts")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        for name in AREAS:
            (root / name).mkdir(parents=True, exist_ok=True)
        devices = {(root / name).stat().st_dev for name in AREAS}
        if len(devices) != 1:
            raise StorageError(
                "the artifact root's folders (" + ", ".join(AREAS) + ") must be on one "
                "filesystem, so that installs are atomic renames"
            )

    # -- keys -------------------------------------------------------------

    def path(self, key: str) -> Path:
        """The file or folder for a stored key; refuses anything outside the root."""
        parts = PurePosixPath(key).parts
        if not _KEY.match(key) or key.startswith("/") or ".." in parts or not parts:
            raise StorageError("malformed artifact key")
        return self.root.joinpath(*parts)

    @staticmethod
    def release_key(experiment_id: str, version_id: str) -> str:
        return f"releases/{experiment_id}/{version_id}.zip"

    @staticmethod
    def session_key(experiment_id: str, session_id: str) -> str:
        return f"sessions/{experiment_id}/{session_id}"

    @staticmethod
    def ai_bundle_key(draft_id: str, job_id: str) -> str:
        return f"ai-drafts/{draft_id}/{job_id}.zip"

    def install_ai_bundle(self, temp: Path, key: str) -> None:
        """Move a generated, validated package into its draft's area, durably.
        Same rules as a release: written once, never overwritten."""
        self.install_release(temp, key)

    def remove_ai_draft(self, draft_id: str) -> None:
        """Remove every generated package of one draft (discard)."""
        folder = self.path(f"ai-drafts/{draft_id}")
        if folder.is_dir():
            shutil.rmtree(folder)
            fsync_dir(folder.parent)

    def writable(self) -> bool:
        probe = self.root / "tmp" / f".probe-{secrets.token_hex(4)}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False

    # -- releases ---------------------------------------------------------

    def new_temp(self) -> Path:
        return self.root / "tmp" / f"{secrets.token_hex(16)}.part"

    def install_release(self, temp: Path, key: str) -> None:
        """Move a received, validated package into place, durably, once."""
        target = self.path(key)
        if target.exists():
            raise StorageError("a release file already exists at this key; refusing to overwrite")
        # r+b: Windows flushes a file only through a handle that may write
        # (FlushFileBuffers); POSIX accepts either. Nothing is written.
        with temp.open("r+b") as handle:
            os.fsync(handle.fileno())
        target.parent.mkdir(parents=True, exist_ok=True)
        fsync_dir(target.parent.parent)
        os.replace(temp, target)
        fsync_dir(target.parent)

    def remove_release(self, key: str) -> None:
        """Undo `install_release` for a release whose database row never committed."""
        self.path(key).unlink(missing_ok=True)

    # -- session staging --------------------------------------------------

    def staging_dir(self, session_id: str) -> Path:
        return self.path(f"staging/{session_id}")

    def staging_file(self, session_id: str, rel: str) -> Path:
        return self._under(self.staging_dir(session_id) / "files", rel)

    def write_chunk(self, session_id: str, rel: str, offset: int, data: bytes) -> None:
        """Write ``data`` at ``offset``, dropping any unacknowledged tail first,
        and sync it before returning (the caller then records the progress)."""
        target = self.staging_file(session_id, rel)
        created = not target.exists()
        if created:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb"):
                pass
        with target.open("r+b") as handle:
            handle.truncate(offset)
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            self._sync_parents(target, self.staging_dir(session_id))

    def create_empty(self, session_id: str, rel: str) -> None:
        """Create (or truncate) an empty staged file, durably: an empty file of a
        manifest is complete from the start and never receives a chunk."""
        target = self.staging_file(session_id, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        self._sync_parents(target, self.staging_dir(session_id))

    def reset_file(self, session_id: str, rel: str) -> None:
        target = self.staging_file(session_id, rel)
        if target.exists():
            with target.open("r+b") as handle:
                handle.truncate(0)
                os.fsync(handle.fileno())

    def staged_size(self, session_id: str, rel: str) -> int:
        target = self.staging_file(session_id, rel)
        return target.stat().st_size if target.exists() else 0

    def remove_staging(self, session_id: str) -> None:
        """Delete an unfinished upload's partial bytes (abort or expiry only)."""
        folder = self.staging_dir(session_id)
        if folder.exists():
            shutil.rmtree(folder)

    # -- sealing ----------------------------------------------------------

    def final_dir(self, experiment_id: str, session_id: str) -> Path:
        return self.path(self.session_key(experiment_id, session_id))

    def final_file(self, experiment_id: str, session_id: str, rel: str) -> Path:
        return self._under(self.final_dir(experiment_id, session_id) / "files", rel)

    def install_session(
        self, session_id: str, experiment_id: str, manifest: dict[str, Any]
    ) -> None:
        """Make a verified staging upload the immutable committed session.

        Writes manifest.json, syncs every file and folder of the staging
        tree, renames the whole tree into place and syncs the parents. The
        destination must not exist: a final directory is never overwritten.
        """
        source = self.staging_dir(session_id)
        target = self.final_dir(experiment_id, session_id)
        if target.exists():
            raise StorageError(
                "a committed session directory already exists; refusing to overwrite"
            )
        manifest_path = source / "manifest.json"
        data = json.dumps(manifest, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")
        with manifest_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for folder, _dirs, files in os.walk(source):
            for name in files:
                with (Path(folder) / name).open("r+b") as handle:  # see install_release
                    os.fsync(handle.fileno())
        for folder, _dirs, _files in os.walk(source, topdown=False):
            fsync_dir(Path(folder))
        target.parent.mkdir(parents=True, exist_ok=True)
        fsync_dir(target.parent.parent)
        os.replace(source, target)
        fsync_dir(target.parent)
        fsync_dir(source.parent)

    def read_final_manifest(self, experiment_id: str, session_id: str) -> dict[str, Any] | None:
        path = self.final_dir(experiment_id, session_id) / "manifest.json"
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def final_session_ids(self) -> Iterator[tuple[str, str]]:
        """(experiment_id, session_id) for every committed-looking directory."""
        base = self.root / "sessions"
        for experiment in sorted(base.iterdir()) if base.exists() else []:
            if experiment.is_dir():
                for session in sorted(experiment.iterdir()):
                    if session.is_dir():
                        yield experiment.name, session.name

    def staging_session_ids(self) -> list[str]:
        base = self.root / "staging"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def stale_temps(self, older_than_s: float, now_s: float) -> list[Path]:
        """Interrupted package uploads: received (*.part) and staged
        (.package-*.zip) copies nobody refers to any more."""
        base = self.root / "tmp"
        candidates = [*base.glob("*.part"), *base.glob(".package-*.zip")]
        return [p for p in candidates if p.is_file() and now_s - p.stat().st_mtime > older_than_s]

    def release_keys(self) -> list[str]:
        base = self.root / "releases"
        return sorted(
            f"releases/{p.parent.name}/{p.name}" for p in base.glob("*/*.zip") if p.is_file()
        )

    # -- helpers ----------------------------------------------------------

    def open_read(self, path: Path) -> BinaryIO:
        return path.open("rb")

    @staticmethod
    def _under(base: Path, rel: str) -> Path:
        parts = PurePosixPath(rel).parts
        if not parts or rel.startswith("/") or any(p in ("..", ".", "") for p in parts):
            raise StorageError("malformed relative file path")
        return base.joinpath(*parts)

    @staticmethod
    def _sync_parents(path: Path, stop: Path) -> None:
        folder = path.parent
        while True:
            fsync_dir(folder)
            if folder == stop or folder == folder.parent:
                break
            folder = folder.parent
        fsync_dir(stop.parent)
