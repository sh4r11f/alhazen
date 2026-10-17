"""The catalogue store: the platform's records as files in one directory.

Layout (every name below is a checked identifier):

    platform-store.json                    store format and version
    bundles/<bundleId>/meta.json           published bundles (immutable)
    bundles/<bundleId>/chunks/<n>.json
    catalogue/<publisher>/<exp>/listing.json              current listing (replaced)
    catalogue/<publisher>/<exp>/versions/<bundleId>.json  one version (immutable)
    users/<uid>/forks/<forkId>.json        a private copy (replaced)
    users/<uid>/bundles/<bundleId>/...     its revisions (immutable)
    users/<uid>/docs/<bundleId>.json       docs snapshot of a forked version (immutable)
    art/<name>                             listing pictures, content-hashed (immutable)

The decision this module hides is how records stay consistent without a database:
every file is replaced atomically (written beside, fsynced, renamed over), immutable
records refuse a second write with different content, and every read-modify-write runs
under one store lock that works across threads and processes (a refresh from the command
line while the server is running). Readers never take the lock: they see the old file or
the new one, never half of one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from alhazen.experiment_platform.bundles import Bundle, read_entries
from alhazen.experiment_platform.errors import NotFound, StoreError
from alhazen.experiment_platform.safety import check_id

STORE_SCHEMA = "alhazen-experiment-platform-store"
STORE_VERSION = 1
_MANIFEST = "platform-store.json"
_LOCK_TIMEOUT_S = 60.0
_ART_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")


class _FileLock:
    """An exclusive lock on ``path`` across processes (fcntl or msvcrt) and threads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._thread = threading.RLock()
        self._depth = 0
        self._handle: Any = None

    def acquire(self, timeout: float = _LOCK_TIMEOUT_S) -> None:
        if not self._thread.acquire(timeout=timeout):
            raise StoreError("The store is busy; try again")
        self._depth += 1
        if self._depth > 1:
            return
        handle = open(self._path, "a+b")  # noqa: SIM115 - held until release()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() > deadline:
                    handle.close()
                    self._depth -= 1
                    self._thread.release()
                    raise StoreError("The store is locked by another process; try again") from error
                time.sleep(0.05)
        self._handle = handle

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._handle is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
        self._thread.release()


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FileStore:
    """The catalogue store rooted at ``root``. ``create=True`` makes a new, empty store
    (refusing a non-empty folder that is not a store); otherwise the folder must already
    be a store of a version this alhazen can read."""

    def __init__(self, root: Path, *, create: bool = False) -> None:
        self.root = Path(root)
        manifest = self.root / _MANIFEST
        if create and not manifest.exists():
            if self.root.exists() and any(self.root.iterdir()):
                raise StoreError(
                    "The folder is not empty and is not a platform store; choose another"
                )
            self.root.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                self.root.chmod(0o700)
            self._replace(
                manifest,
                json.dumps(
                    {
                        "schema": STORE_SCHEMA,
                        "version": STORE_VERSION,
                        "createdAt": int(time.time() * 1000),
                    },
                    indent=2,
                ),
            )
        if not manifest.is_file():
            raise StoreError(
                "This folder is not an Experiment Platform store (create one with --init)"
            )
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError as error:
            raise StoreError("The store's manifest is not valid JSON") from error
        if info.get("schema") != STORE_SCHEMA:
            raise StoreError("This folder is not an Experiment Platform store")
        if not isinstance(info.get("version"), int) or info["version"] > STORE_VERSION:
            raise StoreError(
                f"The store is format {info.get('version')}; "
                f"this alhazen reads up to {STORE_VERSION}. "
                "Upgrade alhazen to open it."
            )
        self._lock = _FileLock(self.root / ".lock")

    # ------------------------------------------------------------------ primitives
    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the store lock for a read-modify-write."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def _path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def _replace(self, path: Path, text: str | bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8") if isinstance(text, str) else text
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        _fsync_dir(path.parent)

    def _write_immutable(self, path: Path, text: str | bytes) -> bool:
        """Write ``path`` once. Returns False when it already holds the same content;
        raises StoreError when it holds different content."""
        data = text.encode("utf-8") if isinstance(text, str) else text
        if path.exists():
            if path.read_bytes() == data:
                return False
            raise StoreError(
                f"{path.relative_to(self.root).as_posix()} already exists with other content; "
                "versions and bundles are never rewritten"
            )
        self._replace(path, data)
        return True

    def _read_json(self, path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise StoreError(
                f"{path.relative_to(self.root).as_posix()} is corrupt (not valid JSON)"
            ) from error

    # ------------------------------------------------------------------ bundles
    def _bundle_dir(self, owner: str | None, bundle_id: str) -> Path:
        check_id(bundle_id, "Bundle id")
        if owner is None:
            return self._path("bundles", bundle_id)
        return self._path("users", check_id(owner, "User id"), "bundles", bundle_id)

    def has_bundle(self, owner: str | None, bundle_id: str) -> bool:
        return (self._bundle_dir(owner, bundle_id) / "meta.json").is_file()

    def put_bundle(self, owner: str | None, bundle: Bundle) -> bool:
        """Store ``bundle`` (published when ``owner`` is None, else private to ``owner``).
        Chunks are written before the meta record, so a bundle is complete once its meta
        exists. Returns False when it was already stored."""
        folder = self._bundle_dir(owner, bundle.id)
        read_entries(bundle.meta, bundle.chunks)  # never store what cannot be read back
        if (folder / "meta.json").is_file():
            stored = self._read_json(folder / "meta.json")
            if stored.get("sha256") != bundle.meta.get("sha256"):
                raise StoreError(f"Bundle {bundle.id} already exists with other content")
            return False
        for i, chunk in enumerate(bundle.chunks):
            self._replace(folder / "chunks" / f"{i:04d}.json", chunk)
        meta = dict(bundle.meta)
        meta.pop("createdAt", None)
        meta["storedAt"] = int(time.time() * 1000)
        self._replace(folder / "meta.json", json.dumps(meta, ensure_ascii=False))
        return True

    def bundle_meta(self, owner: str | None, bundle_id: str) -> dict[str, Any]:
        meta = self._read_json(self._bundle_dir(owner, bundle_id) / "meta.json")
        if meta is None:
            raise NotFound(f"Bundle {bundle_id} is not in this store")
        return dict(meta)

    def bundle_chunk(self, owner: str | None, bundle_id: str, index: int) -> str:
        meta = self.bundle_meta(owner, bundle_id)
        if not isinstance(index, int) or not 0 <= index < int(meta.get("chunkCount") or 0):
            raise NotFound(f"Bundle {bundle_id} has no chunk {index}")
        path = self._bundle_dir(owner, bundle_id) / "chunks" / f"{index:04d}.json"
        if not path.is_file():
            raise StoreError(f"Bundle {bundle_id} is missing chunk {index}")
        return path.read_text(encoding="utf-8")

    def bundle(self, owner: str | None, bundle_id: str) -> Bundle:
        """The stored bundle, verified against its digest."""
        meta = self.bundle_meta(owner, bundle_id)
        chunks = tuple(
            self.bundle_chunk(owner, bundle_id, i) for i in range(int(meta.get("chunkCount") or 0))
        )
        read_entries(meta, chunks)
        return Bundle(bundle_id, meta, chunks)

    def copy_bundle(self, src_owner: str | None, dst_owner: str | None, bundle_id: str) -> bool:
        """Copy a verified bundle between the published store and a user's private one."""
        if self.has_bundle(dst_owner, bundle_id):
            return False
        return self.put_bundle(dst_owner, self.bundle(src_owner, bundle_id))

    # ------------------------------------------------------------------ catalogue
    def _entry_dir(self, publisher: str, exp: str) -> Path:
        return self._path(
            "catalogue", check_id(publisher, "Publisher id"), check_id(exp, "Experiment id")
        )

    def listings(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        base = self._path("catalogue")
        if not base.is_dir():
            return out
        for pub in sorted(p for p in base.iterdir() if p.is_dir()):
            for exp in sorted(e for e in pub.iterdir() if e.is_dir()):
                listing = self._read_json(exp / "listing.json")
                if listing is not None:
                    out.setdefault(pub.name, {})[exp.name] = listing
        return out

    def listing(self, publisher: str, exp: str) -> dict[str, Any] | None:
        return self._read_json(self._entry_dir(publisher, exp) / "listing.json")

    def put_listing(self, publisher: str, exp: str, listing: dict[str, Any]) -> None:
        self._replace(
            self._entry_dir(publisher, exp) / "listing.json",
            json.dumps(listing, ensure_ascii=False, indent=1),
        )

    def delete_listing(self, publisher: str, exp: str) -> None:
        """Take a listing off the catalogue. Its versions stay (forks may name them)."""
        path = self._entry_dir(publisher, exp) / "listing.json"
        if path.exists():
            path.unlink()
            _fsync_dir(path.parent)

    def version(self, publisher: str, exp: str, bundle_id: str) -> dict[str, Any] | None:
        check_id(bundle_id, "Bundle id")
        return self._read_json(self._entry_dir(publisher, exp) / "versions" / f"{bundle_id}.json")

    def versions(self, publisher: str, exp: str) -> list[str]:
        folder = self._entry_dir(publisher, exp) / "versions"
        return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []

    def put_version(self, publisher: str, exp: str, bundle_id: str, record: dict[str, Any]) -> bool:
        check_id(bundle_id, "Bundle id")
        path = self._entry_dir(publisher, exp) / "versions" / f"{bundle_id}.json"
        return self._write_immutable(path, canonical_json(record))

    # ------------------------------------------------------------------ users
    def _user_dir(self, uid: str) -> Path:
        return self._path("users", check_id(uid, "User id"))

    def forks(self, uid: str) -> dict[str, dict[str, Any]]:
        folder = self._user_dir(uid) / "forks"
        out: dict[str, dict[str, Any]] = {}
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                value = self._read_json(path)
                if value is not None:
                    out[path.stem] = value
        return out

    def fork(self, uid: str, fork_id: str) -> dict[str, Any] | None:
        return self._read_json(
            self._user_dir(uid) / "forks" / f"{check_id(fork_id, 'Fork id')}.json"
        )

    def put_fork(self, uid: str, fork_id: str, record: dict[str, Any]) -> None:
        path = self._user_dir(uid) / "forks" / f"{check_id(fork_id, 'Fork id')}.json"
        self._replace(path, json.dumps(record, ensure_ascii=False, indent=1))

    def delete_fork(self, uid: str, fork_id: str) -> None:
        path = self._user_dir(uid) / "forks" / f"{check_id(fork_id, 'Fork id')}.json"
        if path.exists():
            path.unlink()
            _fsync_dir(path.parent)

    def fork_docs(self, uid: str, bundle_id: str) -> dict[str, Any] | None:
        return self._read_json(
            self._user_dir(uid) / "docs" / f"{check_id(bundle_id, 'Bundle id')}.json"
        )

    def put_fork_docs(self, uid: str, bundle_id: str, record: dict[str, Any]) -> bool:
        path = self._user_dir(uid) / "docs" / f"{check_id(bundle_id, 'Bundle id')}.json"
        return self._write_immutable(path, canonical_json(record))

    # ------------------------------------------------------------------ art
    def put_art(self, name_hint: str, data: bytes, suffix: str) -> str:
        """Store a picture under a content-hashed name and return that name."""
        check_id(name_hint, "Picture name")
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            raise StoreError(f"Pictures must be PNG, JPEG, GIF, WebP or SVG, not {suffix!r}")
        name = f"{name_hint}-{hashlib.sha256(data).hexdigest()[:10]}{suffix}"
        self._write_immutable(self._path("art", name), data)
        return name

    def art(self, name: str) -> bytes:
        if not isinstance(name, str) or ".." in name or not _ART_NAME.match(name):
            raise NotFound("No such picture")
        path = self._path("art", name)
        if not path.is_file():
            raise NotFound("No such picture")
        return path.read_bytes()

    # ------------------------------------------------------------------ backup
    def backup(self, dest: Path) -> Path:
        """Copy the whole store to ``dest`` (a new folder) under the lock, so the copy is
        a consistent state. The accounts database, if any, is copied by its owner."""
        dest = Path(dest)
        if dest.exists():
            raise StoreError("The backup folder already exists; choose a new one")
        with self.lock():
            shutil.copytree(
                self.root,
                dest,
                ignore=shutil.ignore_patterns(".lock", ".tmp-*", "accounts.sqlite3*"),
            )
        return dest
