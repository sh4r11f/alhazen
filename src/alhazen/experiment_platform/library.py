"""The library as a rig sees it: the copies a person keeps on a platform, and what of
them is on this machine's disk.

A dashboard on a rig lists the experiments in its owner's library (their copies on an
Experiment Platform host, or in Mana) and downloads one only when it is to be run. This
module hides both sides of that:

- the **platform side**: where the connection is kept (``library.json`` in the dashboard's
  state folder, readable by its owner only, never in an experiment folder) and the client of
  the three requests a rig makes of the platform's JSON API (design.md Appendix A):
  ``GET /api/forks``, ``GET /api/bundles/<handle>/archive`` and, when the dashboard closes,
  ``POST /api/machines/events``. The same client talks to ``alhazen platform local`` (its
  ``#token=`` URL, or a machine token), a lab's ``alhazen platform serve`` (a machine token
  made on its Machines page) and Mana's library service (a bearer token).
- the **disk side**: the marker every download writes (``.alhazen-hub/source.json``: which
  copy and which revision the folder holds), the session data a folder holds, and the rule
  that a folder holding data is never overwritten unasked (:class:`CopyConflict`).

It imports nothing that runs an experiment; the dashboard (alhazen.cli.workspace_library)
builds environments and registers copies.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alhazen.experiment_platform import bundles
from alhazen.experiment_platform.errors import BundleError
from alhazen.experiment_platform.safety import is_id

SOURCE_FILE = "library.json"
SOURCE_SCHEMA = 1
MARKER = Path(".alhazen-hub") / "source.json"
MARKER_SCHEMA = 1
AUTH_KINDS = ("alhazen-token", "bearer")
# A downloaded archive is bounded like an upload (bundles.MAX_ARCHIVE_EXPANDED bounds what
# it may expand to); this bounds the bytes read off the network.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
# What a session leaves in its run folder (alhazen.data.paths.RunFolder): either names one.
SESSION_FILES = ("session.json", "config_snapshot.yaml")
# How far the data scan looks before it stops counting (a copy with data is found long
# before; the bound keeps a huge unrelated tree from stalling a page).
_SCAN_DIRS = 20_000
_SCAN_DEPTH = 8
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# A run job named in the connection (Mana writes the job a run belongs to, so the report a
# closing dashboard sends can say which job it finishes).
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_RUN_JOBS = 50


class LibraryError(Exception):
    """The library cannot be read: no usable connection, the platform refused or could
    not be reached, or an answer was not what the API promises. The message says which,
    in words for the person at the rig; it never contains the token."""


class CopyConflict(LibraryError):
    """A folder already holds another revision (or an unknown one) and session data: it is
    not overwritten unless the person says so (``replace=True``)."""

    def __init__(self, state: CopyState):
        self.state = state
        what = (
            f"revision {state.marker.get('bundle')}"
            if state.marker
            else "files this library did not write"
        )
        super().__init__(
            f"{state.root} already holds {what} with {state.data.sessions} saved session"
            f"{'' if state.data.sessions == 1 else 's'}. The data is kept either way; say "
            "whether to update the experiment's files beside it."
        )


# ---------------------------------------------------------------------- the connection
@dataclass(frozen=True)
class LibrarySource:
    """Where a dashboard reads its owner's library from.

    ``url`` is the platform's base address (``<url>/api/forks``); ``auth`` says how
    ``token`` is sent (``alhazen-token``: the X-Alhazen-Token header of a local platform;
    ``bearer``: an Authorization header); ``label`` names the source on the page;
    ``copies`` is the folder downloads go into, one subfolder per copy; ``extras`` are the
    optional dependency groups installed when an experiment declares them; ``machine`` is
    this machine's name as the platform knows it (a report names it), and ``run_jobs`` the
    platform's run jobs this dashboard serves (Mana's kit writes them)."""

    url: str
    token: str
    auth: str = "bearer"
    label: str = "Library"
    copies: Path = field(default_factory=lambda: Path.home() / "alhazen-experiments")
    extras: tuple[str, ...] = ("psychopy",)
    expires: int | None = None
    machine: str | None = None
    run_jobs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LibraryError(f"The library address must be an http(s) URL, not {self.url!r}")
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
            # The token would cross the network in clear text.
            raise LibraryError("A library on another machine must be reached over https")
        if parts.query or parts.fragment:
            raise LibraryError("The library address must not carry a query or fragment")
        if not isinstance(self.token, str) or not self.token.strip():
            raise LibraryError("The library connection has no token")
        if self.auth not in AUTH_KINDS:
            raise LibraryError(f"Unknown library authentication {self.auth!r}")
        if not Path(self.copies).expanduser().is_absolute():
            raise LibraryError("The copies folder must be an absolute path")
        if not all(isinstance(x, str) and is_id(x) for x in self.extras):
            raise LibraryError("Extras must be names of optional dependency groups")
        if self.machine is not None and (
            not isinstance(self.machine, str) or not 0 < len(self.machine) <= 80
        ):
            raise LibraryError("The machine name must be 1 to 80 characters")
        if len(self.run_jobs) > MAX_RUN_JOBS or not all(
            isinstance(x, str) and _JOB_ID.match(x) for x in self.run_jobs
        ):
            raise LibraryError("The run jobs must be short ids")

    @property
    def fingerprint(self) -> str:
        """Which library this is (address and credential): a change means the copies listed
        so far belong to another connection."""
        import hashlib

        return hashlib.sha256(f"{self.url}\n{self.auth}\n{self.token}".encode()).hexdigest()[:16]

    @property
    def origin(self) -> str:
        """The platform's address without its path: what the page may show (the path of a
        Mana ingress address is itself a credential)."""
        parts = urllib.parse.urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def copies_dir(self) -> Path:
        return Path(self.copies).expanduser()

    def copy_dir(self, copy_id: str) -> Path:
        if not is_id(copy_id):
            raise LibraryError(f"{copy_id!r} is not a copy id")
        return self.copies_dir / copy_id

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": SOURCE_SCHEMA,
            "url": self.url,
            "token": self.token,
            "auth": self.auth,
            "label": self.label,
            "copies": str(self.copies),
            "extras": list(self.extras),
        }
        if self.expires is not None:
            record["expires"] = self.expires
        if self.machine is not None:
            record["machine"] = self.machine
        if self.run_jobs:
            record["runJobs"] = list(self.run_jobs)
        return record

    @classmethod
    def from_record(cls, record: Any) -> LibrarySource:
        if not isinstance(record, dict) or record.get("schema") != SOURCE_SCHEMA:
            raise LibraryError("The library connection file is not one this alhazen can read")
        try:
            return cls(
                url=str(record["url"]).rstrip("/"),
                token=record["token"],
                auth=record.get("auth", "bearer"),
                label=str(record.get("label") or "Library")[:60],
                copies=Path(str(record.get("copies") or Path.home() / "alhazen-experiments")),
                extras=tuple(record.get("extras", ("psychopy",))),
                expires=int(record["expires"]) if record.get("expires") is not None else None,
                machine=str(record["machine"])[:80] if record.get("machine") else None,
                run_jobs=tuple(record.get("runJobs") or ()),
            )
        except KeyError as error:
            raise LibraryError(f"The library connection file has no {error}") from error
        except (TypeError, ValueError) as error:
            raise LibraryError(f"The library connection file is malformed: {error}") from error

    @classmethod
    def from_url(cls, url: str, token: str | None = None, **fields: Any) -> LibrarySource:
        """A source from a platform address. A ``#token=`` fragment (what ``alhazen
        platform local`` prints) is a local platform's token; otherwise ``token`` is a
        bearer token."""
        parts = urllib.parse.urlsplit(url.strip())
        fragment = urllib.parse.parse_qs(parts.fragment).get("token", [None])[0]
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
        if fragment:
            return cls(url=base, token=fragment, auth="alhazen-token", **fields)
        if not token:
            raise LibraryError("Give the library's token (the address carries none)")
        return cls(url=base, token=token, auth="bearer", **fields)


def load_source(state_dir: Path) -> LibrarySource | None:
    """The dashboard's library connection, or None when it has none. An unreadable file
    is an error, not "no library": the person connected one and should hear why it fails."""
    path = Path(state_dir) / SOURCE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LibraryError(f"The library connection cannot be read: {error}") from error
    try:
        record = json.loads(text)
    except ValueError as error:
        raise LibraryError(f"The library connection file is not JSON: {error}") from error
    return LibrarySource.from_record(record)


def save_source(state_dir: Path, source: LibrarySource) -> Path:
    """Write the connection beside the dashboard's registry, owner-only, atomically."""
    folder = Path(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SOURCE_FILE
    _write_private(path, json.dumps(source.as_record(), indent=2))
    return path


def forget_source(state_dir: Path) -> bool:
    path = Path(state_dir) / SOURCE_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def _write_private(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if sys.platform != "win32":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------- the client
Opener = Callable[..., Any]


class LibraryClient:
    """The platform's JSON API, the part a rig uses. ``opener`` is ``urlopen``-shaped (a
    test passes its own)."""

    def __init__(self, source: LibrarySource, opener: Opener | None = None, timeout: float = 30):
        self.source = source
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def _get(self, path: str, limit: int) -> tuple[bytes, str]:
        return self._send("GET", path, limit)

    def _send(
        self, method: str, path: str, limit: int, body: dict[str, Any] | None = None
    ) -> tuple[bytes, str]:
        headers = {"Accept": "application/json, application/zip", "User-Agent": "alhazen-dashboard"}
        if self.source.auth == "alhazen-token":
            headers["X-Alhazen-Token"] = self.source.token
        else:
            headers["Authorization"] = f"Bearer {self.source.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.source.url + path, data=data, headers=headers, method=method
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                data = response.read(limit + 1)
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            raise LibraryError(self._refusal(error)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise LibraryError(f"{self.source.label} cannot be reached: {reason}") from None
        if len(data) > limit:
            raise LibraryError(f"{self.source.label} sent more than {limit} bytes")
        return data, kind

    def _refusal(self, error: urllib.error.HTTPError) -> str:
        detail = ""
        # A refusal without a JSON body (a proxy's HTML page, a cut connection) still gets
        # its status reported; only the detail is missing.
        with contextlib.suppress(ValueError, OSError, AttributeError, TypeError):
            body = json.loads(error.read(64 * 1024))
            got = body.get("error") if isinstance(body, dict) else None
            detail = (
                got.get("message", "")
                if isinstance(got, dict)
                else str(got or body.get("detail", ""))
            )
        if error.code in (401, 403):
            return (
                f"{self.source.label} refused this dashboard's token ({error.code}"
                f"{': ' + detail if detail else ''}). Connect the library again."
            )
        if error.code == 404:
            return (
                f"{self.source.label} has no such copy or revision{': ' + detail if detail else ''}"
            )
        return f"{self.source.label} answered {error.code}{': ' + detail if detail else ''}"

    def forks(self) -> dict[str, dict[str, Any]]:
        """The owner's copies by id, as ``GET /api/forks`` lists them."""
        data, _ = self._get("/api/forks", 16 * 1024 * 1024)
        try:
            body = json.loads(data)
        except ValueError as error:
            raise LibraryError(f"{self.source.label} did not answer with JSON") from error
        forks = body.get("forks") if isinstance(body, dict) else None
        if not isinstance(forks, dict):
            raise LibraryError(f"{self.source.label}'s answer lists no copies")
        return {
            k: v for k, v in forks.items() if is_id(k) and isinstance(v, dict) and v.get("title")
        }

    def report(self, event: dict[str, Any]) -> dict[str, Any]:
        """Tell the platform what happened on this machine (``POST /api/machines/events``):
        the platform's answer, ``{}`` when it sent none. A platform without the route answers
        404, raised as a LibraryError like any refusal."""
        data, _ = self._send("POST", "/api/machines/events", 64 * 1024, event)
        try:
            answer = json.loads(data or b"{}")
        except ValueError:
            return {}
        return answer if isinstance(answer, dict) else {}

    def archive(self, handle: str) -> bytes:
        """A revision's files as a zip (everything under one folder)."""
        if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9~._-]{3,300}", handle):
            raise LibraryError("The copy names no downloadable revision")
        data, _ = self._get(
            f"/api/bundles/{urllib.parse.quote(handle, safe='~')}/archive", MAX_ARCHIVE_BYTES
        )
        return data


# ---------------------------------------------------------------------- the disk
@dataclass(frozen=True)
class DataSummary:
    """Session data under a copy's folder: how many run folders, in which top-level
    folders (``data``, ``data-rehearsal``, ``data-training``...). ``complete`` is False
    when the scan stopped at its bound."""

    sessions: int = 0
    folders: tuple[str, ...] = ()
    complete: bool = True

    def as_json(self) -> dict[str, Any]:
        return {"sessions": self.sessions, "folders": list(self.folders), "complete": self.complete}


@dataclass(frozen=True)
class CopyState:
    """What a copy's folder holds, against the revision the library has now.

    ``state`` is ``absent`` (no folder), ``current`` (the marker names ``head``),
    ``outdated`` (the marker names another revision) or ``unknown`` (a folder without a
    readable marker: copied by hand or by an older tool)."""

    root: Path
    state: str
    marker: dict[str, Any] | None
    data: DataSummary
    environment: bool

    def as_json(self) -> dict[str, Any]:
        marker = None
        if self.marker:
            marker = {k: self.marker.get(k) for k in ("bundle", "writtenAt", "source")}
        return {
            "path": str(self.root),
            "state": self.state,
            "marker": marker,
            "data": self.data.as_json(),
            "environment": self.environment,
        }


def environment_python(root: Path) -> Path | None:
    """The interpreter of a copy's own environment (``uv sync`` builds ``.venv``)."""
    for candidate in (root / ".venv" / "bin" / "python", root / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


def read_marker(root: Path) -> dict[str, Any] | None:
    """The marker a download wrote, or None when there is none or it cannot be read (an
    unreadable marker makes the folder ``unknown``, which is the safe reading)."""
    try:
        record = json.loads((Path(root) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != MARKER_SCHEMA:
        return None
    if not isinstance(record.get("bundle"), str):
        return None
    return record


def find_data(root: Path) -> DataSummary:
    """Count the run folders under ``root``: folders holding ``session.json`` or
    ``config_snapshot.yaml``. Environments, version control and caches are skipped."""
    root = Path(root)
    if not root.is_dir():
        return DataSummary()
    sessions, folders = 0, set()
    complete = True
    for visited, (current, dirs, files) in enumerate(os.walk(root), start=1):
        rel = Path(current).relative_to(root)
        depth = len(rel.parts)
        dirs[:] = [
            d
            for d in dirs
            if d not in bundles._SKIP_DIRS and not d.startswith(".") and depth < _SCAN_DEPTH
        ]
        if any(name in files for name in SESSION_FILES) and rel.parts:
            sessions += 1
            folders.add(rel.parts[0])
            dirs[:] = []  # a run folder's subfolders (media, figures) are its own
        if visited >= _SCAN_DIRS:
            complete = False
            break
    return DataSummary(sessions, tuple(sorted(folders)), complete)


def inspect_copy(root: Path, head: str | None) -> CopyState:
    root = Path(root)
    if not root.exists():
        return CopyState(root, "absent", None, DataSummary(), False)
    marker = read_marker(root)
    if marker is None:
        state = "unknown"
    elif head and marker.get("bundle") == head:
        state = "current"
    else:
        state = "outdated"
    return CopyState(root, state, marker, find_data(root), environment_python(root) is not None)


def _archive_entries(archive: bytes) -> list[bundles.Entry]:
    """The archive's files as bundle entries, every one kept (text as text, binaries as
    base64); paths checked by ``bundles.blobs_from_zip``."""
    try:
        blobs = bundles.blobs_from_zip(archive)
    except BundleError as error:
        raise LibraryError(f"The downloaded revision is not a usable archive: {error}") from error
    entries: list[bundles.Entry] = []
    for path, raw, executable in blobs:
        entry: bundles.Entry
        try:
            entry = [path, raw.decode("utf-8"), 1 if executable else 0]
            if "\0" in entry[1]:
                raise UnicodeDecodeError("utf-8", raw, 0, 1, "NUL")
        except UnicodeDecodeError:
            import base64

            entry = [path, base64.b64encode(raw).decode("ascii"), 1 if executable else 0, "b64"]
        entries.append(entry)
    if not any(e[0] in ("run.py", "pyproject.toml") for e in entries):
        raise LibraryError("The downloaded revision is not an alhazen experiment")
    return entries


def _matches(root: Path, entries: list[bundles.Entry]) -> bool:
    """True when every file of the revision is on disk with the same bytes."""
    for e in entries:
        target = root / e[0]
        try:
            if not target.is_file() or target.read_bytes() != bundles.entry_bytes(e):
                return False
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class WriteResult:
    """What writing a revision did: ``written`` (a new folder), ``updated`` (another
    revision's files replaced, data kept), ``adopted`` (the folder already held exactly
    these files; only the marker was written) or ``unchanged`` (already current)."""

    action: str
    state: CopyState


def write_copy(
    root: Path,
    archive: bytes,
    *,
    copy_id: str,
    bundle_id: str,
    title: str,
    source: str,
    replace: bool = False,
) -> WriteResult:
    """Put revision ``bundle_id`` of copy ``copy_id`` into ``root``.

    The disk is checked first: a folder already at this revision is left alone; a folder
    holding exactly these files is adopted (marker only); a folder holding another
    revision, or unknown files, and session data raises :class:`CopyConflict` unless
    ``replace``. Writing overwrites the revision's files and deletes nothing, so data,
    environments and files the person added stay."""
    root = Path(root)
    before = inspect_copy(root, bundle_id)
    if before.state == "current":
        return WriteResult("unchanged", before)
    entries = _archive_entries(archive)
    if before.state in ("outdated", "unknown"):
        if _matches(root, entries):
            _write_marker(root, copy_id, bundle_id, title, source, entries, before.marker)
            return WriteResult("adopted", inspect_copy(root, bundle_id))
        if before.data.sessions and not replace:
            raise CopyConflict(before)
    try:
        bundles.materialize(entries, root)
    except BundleError as error:
        raise LibraryError(f"The revision cannot be written to {root}: {error}") from error
    _write_marker(root, copy_id, bundle_id, title, source, entries, before.marker)
    return WriteResult(
        "written" if before.state == "absent" else "updated", inspect_copy(root, bundle_id)
    )


def _write_marker(
    root: Path,
    copy_id: str,
    bundle_id: str,
    title: str,
    source: str,
    entries: list[bundles.Entry],
    previous: dict[str, Any] | None,
) -> None:
    history = list((previous or {}).get("previous") or [])[-9:]
    if previous:
        history.append({"bundle": previous.get("bundle"), "writtenAt": previous.get("writtenAt")})
    record = {
        "schema": MARKER_SCHEMA,
        "id": copy_id,
        "bundle": bundle_id,
        "title": title,
        "source": source,
        "writtenAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": sorted(e[0] for e in entries),
        "previous": history,
    }
    path = root / MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, json.dumps(record, indent=1))
