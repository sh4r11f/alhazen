"""Opt-in upload of completed local sessions to the hub: preview, consent, outbox.

Secret it hides: the transfer protocol's client side and its durable state —
what identifies a session upload, how consent is bound, where progress is
kept across restarts, and when work may run.

Invariants (accepted review gates B2 and M5):

* Consent is bound. A preview (:class:`Previews`) records the hub base, the
  collector's remote user id, the experiment/version, the exact file listing
  (path, size, mtime) and the stable ``client_session_id``; an upload must
  name its preview, and any change requires a new one.
* A job keeps that binding for life. The worker never uses "whatever
  credential is current": before every request the stored sign-in must be
  for the job's own hub and user, else the job pauses with
  ``auth_context_changed`` (or ``signed_out``). A credential change bumps
  ``RigState.epoch`` and fences in-flight work.
* Local files are never changed or deleted. A file that changed after the
  preview pauses the job (``local_changed``); it is never re-hashed under the
  same identity.
* Heavy work (hashing, chunk transfer) starts only while no session runs on
  this rig (``busy()``); the job shows ``waiting`` meanwhile.
* Completion is the hub's receipt, never a local guess.

What callers must NOT rely on: rig timing. Waiting while a session runs keeps
transfers out of the way of a session started from this workspace, not out of
the way of anything else on the machine; no hardware timing has been measured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from alhazen.data.atomic import replace_atomically
from alhazen.hub.client import HubClient, HubError, api_path
from alhazen.hub.credentials import Credential, RigState
from alhazen.hub.protocol import COMMITTED, canonical_metadata, receipt_problems, usable_metadata

log = logging.getLogger(__name__)

CHUNK_BYTES = 8 * 1024 * 1024
PREVIEW_TTL_S = 30 * 60
MAX_PREVIEWS = 64
MAX_SESSION_FILES = 10_000
MAX_SESSION_BYTES = 20 * 1024**3
MANIFEST = "manifest.yaml"
ACTIVE = ("queued", "waiting", "hashing", "uploading", "completing")
TERMINAL = ("completed", "failed", "cancelled")
# Codes after which the same account signing in again may resume a job.
RESUMABLE_PAUSES = ("signed_out", "auth_context_changed", "interrupted", "unauthenticated")
IN_PROGRESS = ("staging", "sealing")
# The fields of session.json that identify a person or this machine, named in
# the preview's privacy warning when present.
PRIVATE_CARD_FIELDS = {
    ("subject", "initials"): "subject initials",
    ("subject", "age"): "subject age",
    ("subject", "sex"): "subject sex",
    ("experimenter",): "experimenter name",
    ("rig", "file"): "local rig file path",
    ("params_file",): "local parameter file path",
    ("command",): "the command line, with local paths",
}
PRIVACY_WARNING = (
    "These files are copied to your private storage on the hub exactly as recorded: "
    "they are not anonymised. Session records can name the subject's code, initials, age "
    "and sex, the experimenter, this computer's paths, rig settings and logs. Only your "
    "account can read them; the experiment's author gains no access. Local files are kept."
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LocalFile:
    path: str
    size: int
    mtime_ns: int


def session_files(folder: Path) -> list[LocalFile]:
    """Every file of a completed session folder, sorted, with size and mtime.

    Complete means its ``manifest.yaml`` exists (written at teardown). A
    symlink anywhere refuses the session: it could name a file outside it."""
    if not (folder / MANIFEST).is_file():
        raise SyncError(
            409, "incomplete_session", "This session has no manifest: it never finished"
        )
    found: list[LocalFile] = []
    total = 0
    for directory, dirs, names in os.walk(folder):
        here = Path(directory)
        for name in sorted(dirs + names):
            if (here / name).is_symlink():
                raise SyncError(
                    409,
                    "unsafe_session",
                    f"{(here / name).relative_to(folder).as_posix()} is a link; sessions with "
                    "links are not uploaded",
                )
        dirs.sort()
        for name in sorted(names):
            path = here / name
            if not path.is_file():
                continue
            info = path.stat()
            found.append(
                LocalFile(path.relative_to(folder).as_posix(), info.st_size, info.st_mtime_ns)
            )
            total += info.st_size
            if len(found) > MAX_SESSION_FILES or total > MAX_SESSION_BYTES:
                raise SyncError(
                    413,
                    "too_large",
                    f"Sessions are limited to {MAX_SESSION_FILES} files and "
                    f"{MAX_SESSION_BYTES // 1024**3} GiB",
                )
    return sorted(found, key=lambda f: f.path)


def _card(folder: Path) -> dict[str, Any]:
    try:
        value = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def session_metadata(folder: Path, run_id: str) -> dict[str, Any]:
    """The contract's session metadata, from the run id and session.json:
    always the four keys, a value the hub would refuse (too long, control
    characters) sent as None. The files themselves keep every value."""
    card = _card(folder)
    parts = run_id.split("/")
    raw = card.get("rig")
    rig: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return usable_metadata(
        {
            "subject_code": parts[-3].removeprefix("sub-") if len(parts) >= 3 else None,
            "mode": card.get("mode"),
            "rig_alias": rig.get("name"),
            "started_at": card.get("created"),
        }
    )


def privacy_fields(folder: Path) -> list[str]:
    card = _card(folder)
    present = []
    for keys, label in PRIVATE_CARD_FIELDS.items():
        value: Any = card
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value not in (None, "", [], {}):
            present.append(label)
    return present


def client_session_id(rig_id: str, folder: Path) -> str:
    """The session's stable upload identity: this rig and this folder."""
    digest = hashlib.sha256(f"{rig_id}\0{folder.resolve()}".encode()).hexdigest()
    return f"rig-{digest[:40]}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def files_digest(listing: list[dict[str, Any]]) -> str:
    """sha256 of the canonical file list ``[{path,size,sha256}]`` sorted by
    path, compact JSON. Proposed to the hub as the receipt's
    ``manifest_sha256`` definition (not yet in the contract)."""
    rows = sorted(
        ({"path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in listing),
        key=lambda f: f["path"],
    )
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def check_against_manifest(folder: Path, hashed: list[dict[str, Any]]) -> list[str]:
    """Problems between the hashed files and the session's own manifest.yaml
    (the semantics of alhazen.data.manifest.verify_manifest, without a second
    read of every byte): missing, changed and unlisted files."""
    try:
        manifest = yaml.safe_load((folder / MANIFEST).read_text(encoding="utf-8"))
        listed = {str(a["path"]): str(a["sha256"]) for a in manifest["artifacts"]}
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        return [f"manifest.yaml cannot be read: {exc}"]
    actual = {f["path"]: f["sha256"] for f in hashed if f["path"] != MANIFEST}
    problems = [f"missing: {p}" for p in sorted(set(listed) - set(actual))]
    problems += [
        f"hash mismatch: {p}" for p in sorted(listed) if p in actual and actual[p] != listed[p]
    ]
    problems += [f"unlisted file: {p}" for p in sorted(set(actual) - set(listed))]
    return problems


# -- previews -----------------------------------------------------------------------


class Previews:
    """Short-lived, in-memory consent previews (opaque ids; nothing on disk)."""

    def __init__(self, ttl_s: float = PREVIEW_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._items: dict[str, tuple[dict[str, Any], float]] = {}

    def create(self, binding: dict[str, Any]) -> str:
        preview_id = secrets.token_urlsafe(18)
        moment = time.monotonic()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v[1] > moment}
            if len(self._items) >= MAX_PREVIEWS:
                del self._items[min(self._items, key=lambda k: self._items[k][1])]
            self._items[preview_id] = (binding, moment + self._ttl)
        return preview_id

    def get(self, preview_id: Any) -> dict[str, Any]:
        with self._lock:
            entry = self._items.get(preview_id) if isinstance(preview_id, str) else None
        if entry is None or entry[1] <= time.monotonic():
            raise SyncError(409, "preview_stale", "This preview has expired; preview again")
        return entry[0]


# -- the outbox ------------------------------------------------------------------------


def job_id_for(base: str, user_id: str, session_key: str) -> str:
    return hashlib.sha256(f"{base}\0{user_id}\0{session_key}".encode()).hexdigest()[:24]


PUBLIC_JOB_FIELDS = (
    "id",
    "status",
    "project_id",
    "root_id",
    "run_id",
    "experiment_id",
    "version_id",
    "base_url",
    "user_id",
    "created_at",
    "updated_at",
    "bytes_done",
    "bytes_total",
    "files_done",
    "files_total",
    "error",
    "receipt",
    "session_id",
    "files_digest",
    "manifest_digest",
    "release_source",
)


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """A job as the page sees it: no local paths, no file listing."""
    return {key: job.get(key) for key in PUBLIC_JOB_FIELDS}


class Outbox:
    """Durable upload jobs, one JSON file each under ``<hub>/outbox``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._problems: list[str] = []

    def _path(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise SyncError(404, "not_found", "No such upload job")
        return self.directory / f"{job_id}.json"

    def load(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                value = json.loads(self._path(job_id).read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as exc:
                raise SyncError(
                    500, "outbox_unreadable", f"Upload job record unreadable: {exc}"
                ) from exc
        return value if isinstance(value, dict) else None

    def save(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job["updated_at"] = now()
            replace_atomically(self._path(job["id"]), json.dumps(job, indent=1))
        return job

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            job = self.load(job_id)
            if job is None:
                raise SyncError(404, "not_found", "No such upload job")
            job.update(fields)
            return self.save(job)

    def all(self) -> list[dict[str, Any]]:
        """Every readable job, newest first. An unreadable record is left on
        disk untouched and named in :meth:`problems` (shown on the page),
        so one damaged file never hides the others."""
        jobs = []
        problems = []
        with self._lock:
            for path in sorted(self.directory.glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    problems.append(f"Upload job record {path.name} cannot be read: {exc}")
                    continue
                if isinstance(value, dict):
                    jobs.append(value)
                else:
                    problems.append(f"Upload job record {path.name} is not an object")
            self._problems = problems
        return sorted(jobs, key=lambda j: str(j.get("created_at", "")), reverse=True)

    def problems(self) -> list[str]:
        """Records the last listing could not read."""
        self.all()
        with self._lock:
            return list(self._problems)

    def visible(self, base: str | None, user_id: str | None) -> list[dict[str, Any]]:
        """The jobs bound to exactly this hub and account."""
        if not base or not user_id:
            return []
        return [j for j in self.all() if j.get("base_url") == base and j.get("user_id") == user_id]

    def recover(self) -> int:
        """After a restart, jobs that were active are paused as interrupted
        (resumed only for their own account; see Uploader.resume_for)."""
        count = 0
        for job in self.all():
            if job.get("status") in ACTIVE:
                job.update(
                    status="paused",
                    error={
                        "code": "interrupted",
                        "message": "The dashboard stopped during this upload",
                        "retryable": True,
                    },
                )
                self.save(job)
                count += 1
        return count


class _Fenced(Exception):
    """The job's binding no longer matches the signed-in hub account."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Cancelled(Exception):
    pass


class _Paused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


ClientFactory = Callable[[Credential], HubClient]


class Uploader:
    """One background worker draining queued jobs, one at a time."""

    def __init__(
        self,
        outbox: Outbox,
        state: RigState,
        client_for: ClientFactory,
        busy: Callable[[], bool],
        *,
        wait_s: float = 1.0,
        backoff_s: tuple[float, ...] = (1, 2, 4, 8, 16),
    ) -> None:
        self.outbox = outbox
        self.state = state
        self.client_for = client_for
        self.busy = busy
        self.wait_s = wait_s
        self.backoff_s = backoff_s
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._cancel: set[str] = set()
        self._pause: dict[str, tuple[str, str]] = {}
        self._flags = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="hub-uploader", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    # -- control ------------------------------------------------------------------------

    def submit(self, job_id: str) -> None:
        with self._flags:
            self._cancel.discard(job_id)
            self._pause.pop(job_id, None)
        self._queue.put(job_id)

    def cancel(self, job_id: str) -> None:
        with self._flags:
            self._cancel.add(job_id)

    def pause(
        self, job_id: str, code: str = "paused", message: str = "Paused by the operator"
    ) -> None:
        """Stop a job at its next unit of work, as ``paused`` with ``code``
        (one of RESUMABLE_PAUSES lets the same account's sign-in resume it)."""
        with self._flags:
            self._pause[job_id] = (code, message)

    def resume_for(self, base: str, user_id: str) -> int:
        """Queue this account's jobs that paused for sign-in reasons."""
        count = 0
        for job in self.outbox.visible(base, user_id):
            code = (job.get("error") or {}).get("code")
            if job.get("status") == "paused" and code in RESUMABLE_PAUSES:
                self.outbox.update(job["id"], status="queued", error=None)
                self.submit(job["id"])
                count += 1
        return count

    # -- the worker -----------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(job_id)
            except Exception as exc:  # a bug must not kill the worker for every later job
                log.exception("Upload job %s failed unexpectedly", job_id)
                self._finish(job_id, "failed", "internal_error", f"Unexpected error: {exc}", False)

    def _finish(self, job_id: str, status: str, code: str, message: str, retryable: bool) -> None:
        self.outbox.update(
            job_id, status=status, error={"code": code, "message": message, "retryable": retryable}
        )

    def _gate(self, job: dict[str, Any], epoch: list[int]) -> None:
        """Between units of work: honour cancel/pause, wait while a session
        runs, and re-check the binding when the credential changed."""
        while True:
            with self._flags:
                if job["id"] in self._cancel:
                    raise _Cancelled()
                if job["id"] in self._pause:
                    raise _Paused(*self._pause.pop(job["id"]))
            if self._stop.is_set():
                raise _Paused("interrupted", "The dashboard is stopping")
            if not self.busy():
                break
            if job.get("status") != "waiting":
                self.outbox.update(job["id"], status="waiting")
                job["status"] = "waiting"
            self._stop.wait(self.wait_s)
        if self.state.epoch != epoch[0]:
            self._credential(job)
            epoch[0] = self.state.epoch

    def _credential(self, job: dict[str, Any]) -> Credential:
        credential = self.state.credential()
        if credential is None:
            raise _Fenced("signed_out", "Signed out of the hub; sign in again to resume")
        if credential.base != job["base_url"] or credential.user_id != job["user_id"]:
            raise _Fenced(
                "auth_context_changed",
                "A different hub or account is signed in; this upload resumes only for the "
                "account and hub it was approved for",
            )
        return credential

    def _set(self, job: dict[str, Any], **fields: Any) -> None:
        job.update(fields)
        self.outbox.save(job)

    def _process(self, job_id: str) -> None:
        job = self.outbox.load(job_id)
        if job is None or job.get("status") not in (
            "queued",
            "waiting",
            "hashing",
            "uploading",
            "completing",
        ):
            return
        epoch = [self.state.epoch]
        try:
            self._credential(job)
            self._gate(job, epoch)
            if not job.get("files"):
                self._hash(job, epoch)
            self._transfer(job, epoch)
        except _Cancelled:
            self._finish(job_id, "cancelled", "cancelled", "Cancelled; local files are kept", True)
        except _Paused as exc:
            self._finish(job_id, "paused", exc.code, str(exc), True)
        except _Fenced as exc:
            self._finish(job_id, "paused", exc.code, str(exc), True)
        except SyncError as exc:
            # A changed session needs a new preview and consent: never resumable.
            status = "paused" if exc.code == "local_changed" else "failed"
            self._finish(job_id, status, exc.code, exc.message, False)
        except HubError as exc:
            if exc.status == 401:
                self.state.clear_credential()
                self._finish(job_id, "paused", "unauthenticated", "Sign in to the hub again", True)
            else:
                self._finish(job_id, "failed", exc.code, exc.message, exc.retryable)

    def _hash(self, job: dict[str, Any], epoch: list[int]) -> None:
        folder = Path(job["folder"])
        self._set(job, status="hashing")
        hashed = []
        for entry in job["listing"]:
            self._gate(job, epoch)
            path = folder / entry["path"]
            try:
                info = path.stat()
            except OSError:
                info = None
            if (
                info is None
                or path.is_symlink()
                or info.st_size != entry["size"]
                or info.st_mtime_ns != entry["mtime_ns"]
            ):
                raise SyncError(
                    409,
                    "local_changed",
                    f"{entry['path']} changed after the preview; preview the session again",
                )
            hashed.append(
                {"path": entry["path"], "size": entry["size"], "sha256": file_sha256(path)}
            )
        problems = check_against_manifest(folder, hashed)
        if problems:
            raise SyncError(
                409,
                "manifest_mismatch",
                "The session no longer matches its manifest: " + "; ".join(problems[:5]),
            )
        self._set(job, files=hashed, files_digest=files_digest(hashed))

    def _call(self, job: dict[str, Any], epoch: list[int], fn: Callable[[HubClient], Any]) -> Any:
        """One idempotent request, retried on transient failures with the
        same identity; the binding is re-checked before every attempt."""
        attempt = 0
        while True:
            self._gate(job, epoch)
            client = self.client_for(self._credential(job))
            try:
                return fn(client)
            except HubError as exc:
                if not exc.retryable or attempt >= len(self.backoff_s):
                    raise
                delay = self.backoff_s[attempt]
                attempt += 1
                self._set(
                    job,
                    error={
                        "code": exc.code,
                        "message": f"{exc.message}; retrying in {delay:g} s",
                        "retryable": True,
                    },
                )
                if self._stop.wait(delay):
                    raise _Paused("interrupted", "The dashboard is stopping") from None

    def _transfer(self, job: dict[str, Any], epoch: list[int]) -> None:
        folder = Path(job["folder"])
        files = job["files"]
        self._set(
            job,
            status="uploading",
            bytes_total=sum(f["size"] for f in files),
            files_total=len(files),
            error=None,
        )
        if not job.get("session_id"):
            body = {
                "experiment_id": job["experiment_id"],
                "version_id": job["version_id"],
                "client_session_id": job["client_session_id"],
                "files": files,
                "metadata": canonical_metadata(job["metadata"]),
                "consent": True,
            }
            session = _unwrap(
                self._call(
                    job,
                    epoch,
                    lambda c: c.json("POST", api_path("sessions", "init"), json_body=body),
                ),
                "session",
            )
            answered = session.get("client_session_id")
            if answered is not None and answered != job["client_session_id"]:
                raise SyncError(
                    502,
                    "receipt_mismatch",
                    "The hub answered for another upload identity; nothing was sent",
                )
            self._set(job, session_id=str(session["id"]))
        progress = _unwrap(
            self._call(
                job,
                epoch,
                lambda c: c.json("GET", api_path("sessions", job["session_id"], "upload")),
            ),
            "session",
        )
        received = {
            str(f.get("path")): int(f.get("received", 0))
            for f in progress.get("files", [])
            if isinstance(f, dict)
        }
        done = sum(min(received.get(f["path"], 0), f["size"]) for f in files)
        self._set(
            job,
            bytes_done=done,
            files_done=sum(received.get(f["path"], 0) >= f["size"] for f in files),
        )
        if str(progress.get("status")) != COMMITTED:
            for entry in files:
                offset = received.get(entry["path"], 0)
                while offset < entry["size"]:
                    self._gate(job, epoch)
                    offset = self._chunk(job, epoch, folder, entry, offset)
                    received[entry["path"]] = offset
                    self._set(
                        job,
                        bytes_done=sum(min(received.get(f["path"], 0), f["size"]) for f in files),
                        files_done=sum(received.get(f["path"], 0) >= f["size"] for f in files),
                    )
        self._set(job, status="completing")
        for attempt in range(len(self.backoff_s) + 1):
            try:
                receipt = _unwrap(
                    self._call(
                        job,
                        epoch,
                        lambda c: c.json(
                            "POST", api_path("sessions", job["session_id"], "complete")
                        ),
                    ),
                    "receipt",
                )
            except HubError as exc:
                # Another request holds the seal: the same call later.
                if exc.code != "sealing_in_progress" or attempt >= len(self.backoff_s):
                    raise
                if self._stop.wait(self.backoff_s[attempt]):
                    raise _Paused("interrupted", "The dashboard is stopping") from None
                continue
            status = str(receipt.get("status"))
            if status == COMMITTED:
                self._accept(job, receipt)
                return
            if status not in IN_PROGRESS or attempt >= len(self.backoff_s):
                raise HubError(
                    502, "hub_bad_response", f"The hub did not commit the session ({status})"
                )
            if self._stop.wait(self.backoff_s[attempt]):
                raise _Paused("interrupted", "The dashboard is stopping")

    def _accept(self, job: dict[str, Any], receipt: dict[str, Any]) -> None:
        """Completed only for a receipt that certifies exactly the bound
        upload (alhazen.hub.protocol.receipt_problems)."""
        problems = receipt_problems(
            receipt,
            session_id=job["session_id"],
            client_session_id=job["client_session_id"],
            experiment_id=job["experiment_id"],
            version_id=job["version_id"],
            files=job["files"],
            metadata=job["metadata"],
        )
        if problems:
            self._set(job, rejected_receipt=receipt)
            raise SyncError(
                502,
                "receipt_mismatch",
                "The hub's receipt does not match this upload: " + "; ".join(problems[:4]),
            )
        self._set(
            job,
            status="completed",
            receipt=receipt,
            error=None,
            bytes_done=job["bytes_total"],
            files_done=len(job["files"]),
        )

    def _chunk(
        self,
        job: dict[str, Any],
        epoch: list[int],
        folder: Path,
        entry: dict[str, Any],
        offset: int,
    ) -> int:
        path = folder / entry["path"]
        info = path.stat()
        listed = next(f for f in job["listing"] if f["path"] == entry["path"])
        if info.st_size != entry["size"] or info.st_mtime_ns != listed["mtime_ns"]:
            raise SyncError(
                409, "local_changed", f"{entry['path']} changed during the upload; preview again"
            )
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(min(CHUNK_BYTES, entry["size"] - offset))
        if not data:
            raise SyncError(
                409, "local_changed", f"{entry['path']} is shorter than when it was hashed"
            )
        digest = hashlib.sha256(data).hexdigest()
        answer = self._call(
            job,
            epoch,
            lambda c: c.json(
                "PUT",
                api_path("sessions", job["session_id"], "files"),
                query={"path": entry["path"], "offset": str(offset)},
                data=data,
                content_type="application/octet-stream",
                headers={"X-Chunk-SHA256": digest},
            ),
        )
        reported = _received(answer, entry["path"])
        return reported if reported is not None and reported > offset else offset + len(data)


def _unwrap(answer: Any, key: str) -> dict[str, Any]:
    if isinstance(answer, dict) and isinstance(answer.get(key), dict):
        return answer[key]
    if isinstance(answer, dict) and "id" in answer:
        return answer
    raise HubError(502, "hub_bad_response", f"The hub's answer has no {key}")


def _received(answer: Any, path: str) -> int | None:
    if not isinstance(answer, dict):
        return None
    if isinstance(answer.get("received"), int):
        return int(answer["received"])
    file = answer.get("file")
    if isinstance(file, dict) and isinstance(file.get("received"), int):
        return int(file["received"])
    for item in answer.get("files", []) if isinstance(answer.get("files"), list) else []:
        if (
            isinstance(item, dict)
            and item.get("path") == path
            and isinstance(item.get("received"), int)
        ):
            return int(item["received"])
    return None
