"""Private session uploads: reservation, verified chunks, seal and commit.

Hides the raw-upload state machine (review gate B1, data review fixes):

    staging --complete--> sealing --durable install--> committed
       |                     |
       +--abort/expiry-->  aborted / expired      (reservation released)

- `init_session` reserves the whole session's bytes against the owner's quota
  in the transaction that creates it, and creates every empty file of the
  manifest at once (an empty file never receives a chunk). One (owner,
  client_session_id) names one OPEN or COMMITTED upload: an identical init
  returns it; different content is a conflict. A closed attempt (aborted or
  expired) is never reopened or overwritten: the next init with that client
  id starts a NEW attempt row that names the closed one
  (``previous_attempt_id``); the closed row keeps its history and gives up
  the unique key (docs/hub/server.md "Retrying a closed upload").
- `put_chunk` holds the session row lock (PostgreSQL ``FOR UPDATE``; SQLite's
  database write lock) for the chunk, so PUTs and a seal claim of one session
  never interleave. Bytes are written after truncating any unacknowledged
  tail, synced, and only then is progress committed with a durable chunk
  record (session, path, offset, length, sha256). A file at most
  `EARLY_HASH_BYTES` long is hashed when its last byte arrives; every file is
  hashed at the seal.
- Sealing is FENCED: the claim stores a random ``seal_token`` with a lease;
  the sealer renews the lease while it hashes and every later step (renewal,
  commit, reset, lease release, problem mark) is a database update
  conditional on that token. A sealer that lost its claim stops without
  changing anything. Files are re-hashed outside any transaction, the tree is
  installed into its immutable final directory (synced files, folders,
  rename, parents) and the database pointer is committed last. An existing
  final directory is verified, never overwritten; one that disagrees marks
  the upload ``artifact_conflict`` for the owner (abort) or an operator.
- `reconcile` finishes abandoned seals, marks committed sessions whose files
  are missing, removes leftovers it can prove are safe to remove, and
  reports what it cannot.
- Derived trial indexing runs after the commit, separately (trials.py).

A receipt certifies verified files on the hub's primary storage. It is not
a backup.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import re
import secrets
import shutil
import time
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from alhazen.hub import packages, protocol
from alhazen.hub.auth import Principal, audit
from alhazen.hub.catalog import may_collect_with
from alhazen.hub.context import Hub, new_id
from alhazen.hub.errors import HubError, conflict, invalid, not_found, too_large
from alhazen.hub.quota import lock_owner, require_room
from alhazen.hub.schema import data_sessions, session_chunks, session_files, versions
from alhazen.hub.storage import StorageError, sha256_file
from alhazen.hub.timefmt import iso
from alhazen.hub.trials import is_trials_table

log = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{64}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
METADATA_KEYS = protocol.METADATA_LIMITS  # the four keys and their limits, one definition
DURABILITY = "verified on the hub's primary storage; not an independent backup"
MISSING = "the stored copy of this session is missing; an operator must restore it"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# Files up to this size are hashed in full when their last byte arrives (an
# early, cheap answer); larger ones only at the seal, so no PUT does long work.
EARLY_HASH_BYTES = 64 * 1024 * 1024
OPEN = ("staging", "sealing")
CLOSED = ("aborted", "expired")
_RETIRED_PREFIX = "~closed~"  # '~' never appears in a valid client id


class SealLost(Exception):
    """This sealer's claim was taken over (its token no longer matches)."""


class ArtifactConflict(Exception):
    """An existing final directory disagrees with the session record."""


# -- validation ---------------------------------------------------------------


def parse_init(hub: Hub, body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"experiment_id", "version_id", "client_session_id", "files", "metadata", "consent"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    if body.get("consent") is not True:
        raise invalid("consent must be true to upload a session", "consent_required")
    for key in ("experiment_id", "version_id"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise invalid(f"{key} is required")
    client_id = body.get("client_session_id")
    if not isinstance(client_id, str) or not _CLIENT_ID.match(client_id):
        raise invalid("client_session_id must be 1-128 of A-Z a-z 0-9 . _ : -")
    metadata = body.get("metadata", {})
    if not isinstance(metadata, dict):
        raise invalid("metadata must be an object")
    bad = sorted(set(metadata) - set(METADATA_KEYS))
    if bad:
        raise invalid(f"unknown metadata field(s): {', '.join(bad)}", "unknown_field")
    clean_meta: dict[str, str | None] = {}
    for key, limit in METADATA_KEYS.items():
        value = metadata.get(key)
        if value is None:
            clean_meta[key] = None
            continue
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise invalid(f"metadata.{key} must be a string of at most {limit} characters")
        clean_meta[key] = value
    files = body.get("files")
    limits = hub.settings.limits
    if not isinstance(files, list) or not files:
        raise invalid("files must be a non-empty list")
    if len(files) > limits.max_session_files:
        raise too_large(f"A session may hold at most {limits.max_session_files} files")
    seen: dict[str, str] = {}
    out = []
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise invalid("each file is {path, size, sha256}")
        path, size, digest = entry["path"], entry["size"], entry["sha256"]
        if not isinstance(path, str):
            raise invalid("file path must be a string")
        try:
            normal = packages.safe_relative(path)
        except packages.PackageError as exc:
            raise invalid(f"file path {path!r} is not allowed: {exc}", "invalid_path") from None
        if normal != path:
            raise invalid(
                f"file path {path!r} must be given in normal form ({normal!r})", "invalid_path"
            )
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise invalid(f"size of {path!r} must be a non-negative integer")
        if not isinstance(digest, str) or not _SHA.match(digest):
            raise invalid(f"sha256 of {path!r} must be 64 lowercase hex characters")
        if size == 0 and digest != EMPTY_SHA256:
            raise invalid(
                f"{path!r} is empty, so its sha256 must be the empty digest {EMPTY_SHA256}"
            )
        folded = path.casefold()
        if folded in seen:
            raise invalid(f"{path!r} duplicates {seen[folded]!r} (paths must differ beyond case)")
        seen[folded] = path
        total += size
        out.append({"path": path, "size": size, "sha256": digest})
    folded_paths = set(seen)
    for folded in folded_paths:
        parts = folded.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in folded_paths:
                raise invalid(
                    f"{seen[folded]!r} lies inside a path that is also a file", "invalid_path"
                )
    if total > limits.max_session_bytes:
        raise too_large(f"A session may hold at most {limits.max_session_bytes} bytes")
    out.sort(key=lambda f: str(f["path"]))
    return {
        "experiment_id": body["experiment_id"],
        "version_id": body["version_id"],
        "client_session_id": client_id,
        "files": out,
        "metadata": clean_meta,
        "total": total,
    }


def manifest_digest(parsed: dict[str, Any]) -> str:
    """Canonical identity of an upload: the shared protocol's definition
    (alhazen.hub.protocol.manifest_sha256), the same function the rig uses
    to check a receipt."""
    return protocol.manifest_sha256(
        parsed["experiment_id"], parsed["version_id"], parsed["files"], parsed["metadata"]
    )


# -- views --------------------------------------------------------------------


def _files(conn: Connection, session_id: str) -> list[Any]:
    return list(
        conn.execute(
            select(session_files)
            .where(session_files.c.session_id == session_id)
            .order_by(session_files.c.path)
        ).all()
    )


def _problem(row: Any) -> dict[str, Any] | None:
    if not row.problem_code:
        return None
    messages = {
        "artifact_conflict": "A stored copy of this upload disagrees with its record and was kept "
        "untouched; abort the upload and send it again, or ask an operator",
        "artifact_missing": MISSING,
    }
    return {
        "code": row.problem_code,
        "message": messages.get(row.problem_code, row.problem_code),
        "since": iso(row.problem_at),
    }


def session_row_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "client_session_id": row.retired_client_id or row.client_session_id,
        "status": row.status,
        "metadata": json.loads(row.metadata),
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
        "completed_at": iso(row.completed_at),
        "manifest_sha256": row.manifest_sha256,
        "total_bytes": int(row.total_bytes),
        "file_count": int(row.file_count),
        "previous_attempt_id": row.previous_attempt_id,
        "problem": _problem(row),
        "index": {
            "status": row.index_status,
            "rows": int(row.index_rows),
            "error": row.index_error,
        },
    }


def progress_view(conn: Connection, row: Any) -> dict[str, Any]:
    files = _files(conn, row.id)
    view = session_row_view(row)
    view["files"] = [
        {
            "path": f.path,
            "size": int(f.size),
            "sha256": f.sha256,
            "received": int(f.received),
            "verified": bool(f.verified),
        }
        for f in files
    ]
    view["received_bytes"] = sum(int(f.received) for f in files)
    return view


def receipt(row: Any) -> dict[str, Any]:
    missing = row.problem_code == "artifact_missing"
    return {
        "id": row.id,
        "status": row.status,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "client_session_id": row.retired_client_id or row.client_session_id,
        "manifest_sha256": row.manifest_sha256,
        "file_count": int(row.file_count),
        "total_bytes": int(row.total_bytes),
        "completed_at": iso(row.completed_at),
        "durability": MISSING if missing else DURABILITY,
        "problem": _problem(row),
        "index": {
            "status": row.index_status,
            "rows": int(row.index_rows),
            "error": row.index_error,
        },
    }


def _owned(conn: Connection, principal: Principal, session_id: str, *, lock: bool) -> Any:
    query = select(data_sessions).where(
        data_sessions.c.id == session_id, data_sessions.c.owner_id == principal.user_id
    )
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).first()
    if row is None:
        raise not_found("Upload not found")
    return row


def _row(conn: Connection, session_id: str) -> Any:
    return conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()


# -- init ---------------------------------------------------------------------


def init_session(
    hub: Hub, principal: Principal, body: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Create (201, True) or return (200, False) the caller's upload."""
    parsed = parse_init(hub, body)
    digest = manifest_digest(parsed)
    now = hub.clock()
    stale: list[str] = []
    try:
        with hub.db.transaction() as conn:
            lock_owner(conn, principal.user_id)
            # Stale uploads expire first, so a retry of one starts a new attempt.
            stale = _expire_owner_stale(hub, conn, principal.user_id, now)
            view, created = _init_locked(hub, conn, principal, parsed, digest, now)
    except IntegrityError:
        # A concurrent init with the same client_session_id won the insert.
        with hub.db.transaction() as conn:
            existing = conn.execute(
                select(data_sessions).where(
                    data_sessions.c.owner_id == principal.user_id,
                    data_sessions.c.client_session_id == parsed["client_session_id"],
                )
            ).first()
            if existing is None or existing.manifest_sha256 != digest:
                raise conflict(
                    "session_conflict",
                    "This client_session_id already names an upload with different content",
                ) from None
            return progress_view(conn, existing), False
    for session_id in stale:
        hub.store.remove_staging(session_id)
    return view, created


def _init_locked(
    hub: Hub, conn: Connection, principal: Principal, parsed: dict[str, Any], digest: str, now: int
) -> tuple[dict[str, Any], bool]:
    """The init decision and insert, under the owner's lock."""
    limits = hub.settings.limits
    existing = conn.execute(
        select(data_sessions)
        .where(
            data_sessions.c.owner_id == principal.user_id,
            data_sessions.c.client_session_id == parsed["client_session_id"],
        )
        .with_for_update()
    ).first()
    previous = None
    if existing is not None:
        if existing.status not in CLOSED:
            if existing.manifest_sha256 != digest:
                raise conflict(
                    "session_conflict",
                    "This client_session_id already names an upload with different "
                    "content"
                    + (
                        "; abort it before sending other content"
                        if existing.status in OPEN
                        else "; a committed session is never replaced"
                    ),
                    session_id=existing.id,
                    status=existing.status,
                )
            return progress_view(conn, existing), False
        previous = existing.id
    may_collect_with(conn, principal, parsed["experiment_id"], parsed["version_id"])
    active = conn.execute(
        select(func.count())
        .select_from(data_sessions)
        .where(
            data_sessions.c.owner_id == principal.user_id,
            data_sessions.c.status.in_(OPEN),
        )
    ).scalar_one()
    if active >= limits.max_staging_sessions:
        raise HubError(
            429,
            "upload_limit",
            f"You have {active} unfinished uploads, the most allowed at once; finish or "
            "abort one first (GET /sessions lists them)",
        )
    require_room(conn, principal.user_id, parsed["total"], limits.user_quota_bytes)
    _require_disk(hub, conn, parsed["total"])
    if previous is not None:
        # Retire the closed attempt's key; its row and history stay.
        conn.execute(
            update(data_sessions)
            .where(data_sessions.c.id == previous)
            .values(
                client_session_id=_RETIRED_PREFIX + previous,
                retired_client_id=parsed["client_session_id"],
            )
        )
    session_id = new_id()
    conn.execute(
        insert(data_sessions).values(
            id=session_id,
            owner_id=principal.user_id,
            experiment_id=parsed["experiment_id"],
            version_id=parsed["version_id"],
            client_session_id=parsed["client_session_id"],
            status="staging",
            metadata=json.dumps(parsed["metadata"], sort_keys=True),
            subject_code=parsed["metadata"]["subject_code"],
            mode=parsed["metadata"]["mode"],
            manifest_sha256=digest,
            total_bytes=parsed["total"],
            file_count=len(parsed["files"]),
            storage_key=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
            seal_lease_until=None,
            seal_token=None,
            problem_code=None,
            problem_at=None,
            previous_attempt_id=previous,
            retired_client_id=None,
            index_status="none",
            index_claimed_until=None,
            index_token=None,
            index_rows=0,
            index_error=None,
            index_columns=None,
        )
    )
    conn.execute(
        insert(session_files),
        [
            {
                "session_id": session_id,
                "path": f["path"],
                "size": f["size"],
                "sha256": f["sha256"],
                "received": 0,
                "verified": f["size"] == 0,
            }
            for f in parsed["files"]
        ],
    )
    # Empty files are complete now: create them durably before the
    # row that calls them verified commits.
    for f in parsed["files"]:
        if f["size"] == 0:
            hub.store.create_empty(session_id, f["path"])
    audit(
        conn,
        now,
        f"user:{principal.user_id}",
        "session.init",
        session_id,
        {
            "experiment": parsed["experiment_id"],
            "files": len(parsed["files"]),
            "bytes": parsed["total"],
            "previous_attempt": previous,
        },
    )
    return progress_view(conn, _row(conn, session_id)), True


def _require_disk(hub: Hub, conn: Connection, adding: int) -> None:
    outstanding = conn.execute(
        select(func.coalesce(func.sum(session_files.c.size - session_files.c.received), 0))
        .select_from(
            session_files.join(data_sessions, data_sessions.c.id == session_files.c.session_id)
        )
        .where(data_sessions.c.status.in_(OPEN))
    ).scalar_one()
    free = shutil.disk_usage(hub.store.root).free
    if free - int(outstanding) - adding < hub.settings.min_free_bytes:
        raise HubError(
            507,
            "insufficient_storage",
            "The hub does not have room for this session now; nothing was reserved",
        )


def _expire_owner_stale(hub: Hub, conn: Connection, owner_id: str, now: int) -> list[str]:
    horizon = now - hub.settings.limits.staging_retention_seconds * 1000
    rows = conn.execute(
        select(data_sessions.c.id)
        .where(
            data_sessions.c.owner_id == owner_id,
            data_sessions.c.status == "staging",
            data_sessions.c.updated_at < horizon,
        )
        .with_for_update()
    ).all()
    return [
        r.id for r in rows if _close(conn, r.id, "expired", now, actor="system", horizon=horizon)
    ]


def _close(
    conn: Connection,
    session_id: str,
    status: str,
    now: int,
    *,
    actor: str,
    horizon: int | None = None,
    allow_problem_seal: bool = False,
) -> bool:
    """Close an unfinished upload; True if this call closed it.

    Only a STAGING row closes (or, for an owner's abort, a sealing row stuck
    on a recorded problem whose lease is not live). The guard is in the
    UPDATE itself, so a row a concurrent complete moved to sealing is never
    closed under it. ``horizon`` additionally requires the row to be stale.
    """
    condition = data_sessions.c.status == "staging"
    if allow_problem_seal:
        condition = condition | (
            (data_sessions.c.status == "sealing")
            & data_sessions.c.problem_code.is_not(None)
            & (
                data_sessions.c.seal_lease_until.is_(None)
                | (data_sessions.c.seal_lease_until <= now)
            )
        )
    query = update(data_sessions).where(data_sessions.c.id == session_id, condition)
    if horizon is not None:
        query = query.where(data_sessions.c.updated_at < horizon)
    closed = conn.execute(
        query.values(status=status, updated_at=now, seal_lease_until=None, seal_token=None)
    ).rowcount
    if not closed:
        return False
    conn.execute(delete(session_chunks).where(session_chunks.c.session_id == session_id))
    audit(conn, now, actor, f"session.{status}", session_id, {})
    return True


def expire_stale(hub: Hub) -> int:
    """Expire every upload untouched past the retention period (maintenance)."""
    now = hub.clock()
    horizon = now - hub.settings.limits.staging_retention_seconds * 1000
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(data_sessions.c.id)
            .where(data_sessions.c.status == "staging", data_sessions.c.updated_at < horizon)
            .with_for_update()
        ).all()
        ids = [
            r.id
            for r in rows
            if _close(conn, r.id, "expired", now, actor="system", horizon=horizon)
        ]
    for session_id in ids:
        hub.store.remove_staging(session_id)
    return len(ids)


def abort_session(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    """Close the caller's unfinished upload and delete its partial bytes.

    Never touches committed data, nor a seal in progress; an upload stuck on
    ``artifact_conflict`` may be aborted (its disputed final copy is kept for
    the operator)."""
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, session_id, lock=True)
        if row.status in CLOSED:
            return session_row_view(row)
        if not _close(
            conn,
            session_id,
            "aborted",
            now,
            actor=f"user:{principal.user_id}",
            allow_problem_seal=True,
        ):
            if row.status == "sealing":
                raise conflict(
                    "sealing_in_progress", "This upload is being sealed and cannot be aborted now"
                )
            raise conflict(
                f"session_{row.status}",
                "Only an unfinished upload can be aborted; committed sessions are kept",
            )
        view = session_row_view(_row(conn, session_id))
    hub.store.remove_staging(session_id)
    return view


def progress(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        return progress_view(conn, _owned(conn, principal, session_id, lock=False))


def list_unfinished(hub: Hub, principal: Principal, limit: int, offset: int) -> dict[str, Any]:
    """The caller's open uploads (staging or sealing, including stuck ones),
    newest first, with enough state to resume, wait or abort each."""
    now = hub.clock()
    retention = hub.settings.limits.staging_retention_seconds * 1000
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(data_sessions)
            .where(
                data_sessions.c.owner_id == principal.user_id,
                data_sessions.c.status.in_(OPEN),
            )
            .order_by(data_sessions.c.created_at.desc(), data_sessions.c.id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
        items = []
        for row in rows[:limit]:
            counts = conn.execute(
                select(
                    func.coalesce(func.sum(session_files.c.received), 0),
                    func.count().filter(session_files.c.verified.is_(True)),
                ).where(session_files.c.session_id == row.id)
            ).one()
            view = session_row_view(row)
            live = (
                row.status == "sealing"
                and row.seal_lease_until is not None
                and row.seal_lease_until > now
            )
            view.update(
                received_bytes=int(counts[0]),
                files_verified=int(counts[1]),
                sealing_active=live,
                expires_at=iso(row.updated_at + retention) if row.status == "staging" else None,
                expired=row.status == "staging" and row.updated_at + retention <= now,
                can_abort=row.status == "staging" or (row.problem_code is not None and not live),
            )
            items.append(view)
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


# -- chunks -------------------------------------------------------------------


def put_chunk(
    hub: Hub,
    principal: Principal,
    session_id: str,
    path: str,
    offset: int,
    data: bytes,
    chunk_sha: str,
) -> dict[str, Any]:
    if not _SHA.match(chunk_sha):
        raise invalid("X-Chunk-SHA256 must be 64 lowercase hex characters", "invalid_chunk")
    if hashlib.sha256(data).hexdigest() != chunk_sha:
        raise invalid("The chunk does not match its X-Chunk-SHA256", "chunk_hash_mismatch")
    if offset < 0:
        raise invalid("offset must be a non-negative integer")
    length = len(data)
    now = hub.clock()
    retention = hub.settings.limits.staging_retention_seconds * 1000
    failed_hash = False
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, session_id, lock=True)
        _require_staging(row, now, retention)
        file = conn.execute(
            select(session_files)
            .where(session_files.c.session_id == session_id, session_files.c.path == path)
            .with_for_update()
        ).first()
        if file is None:
            raise not_found("That path is not in this upload's manifest")
        size, received = int(file.size), int(file.received)
        if size == 0 and length == 0 and offset == 0:
            # Created and verified at init; an empty PUT is a harmless replay.
            return _chunk_answer(file.path, 0, 0, True, replay=True)
        if offset + length > size:
            raise invalid("The chunk runs past the end of the file", "chunk_out_of_range")
        if length == 0:
            raise invalid("An empty chunk is only valid for an empty file", "empty_chunk")
        recorded = conn.execute(
            select(session_chunks).where(
                session_chunks.c.session_id == session_id,
                session_chunks.c.path == path,
                session_chunks.c.offset == offset,
            )
        ).first()
        if recorded is not None:
            if int(recorded.length) == length and recorded.sha256 == chunk_sha:
                return _chunk_answer(file.path, size, received, bool(file.verified), replay=True)
            raise conflict(
                "chunk_conflict",
                "Different bytes were already received at this offset",
                received=received,
            )
        if offset != received:
            raise conflict(
                "offset_mismatch",
                f"The hub has {received} bytes of this file; send the chunk at that offset",
                received=received,
            )
        try:
            hub.store.write_chunk(session_id, path, offset, data)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise HubError(
                    507,
                    "insufficient_storage",
                    "The hub ran out of space; nothing was acknowledged",
                ) from None
            raise
        new_received = received + length
        verified = False
        conn.execute(
            insert(session_chunks).values(
                session_id=session_id, path=path, offset=offset, length=length, sha256=chunk_sha
            )
        )
        if new_received == size:
            # Complete: every chunk matched its digest. A small file is also
            # checked whole now; every file is checked whole at the seal.
            verified = True
            if size <= EARLY_HASH_BYTES:
                whole = sha256_file(hub.store.staging_file(session_id, path))
                if whole != file.sha256:
                    failed_hash = True
                    verified = False
                    _reset_file(hub, conn, session_id, path)
        if not failed_hash:
            conn.execute(
                update(session_files)
                .where(session_files.c.session_id == session_id, session_files.c.path == path)
                .values(received=new_received, verified=verified)
            )
        conn.execute(
            update(data_sessions).where(data_sessions.c.id == session_id).values(updated_at=now)
        )
    if failed_hash:
        raise conflict(
            "file_hash_mismatch",
            f"{path} did not match its declared sha256; the hub discarded it, send it again from 0",
            path=path,
            received=0,
        )
    return _chunk_answer(path, size, new_received, verified, replay=False)


def _chunk_answer(
    path: str, size: int, received: int, verified: bool, *, replay: bool
) -> dict[str, Any]:
    return {
        "path": path,
        "size": size,
        "received": received,
        "verified": verified,
        "replay": replay,
    }


def _require_staging(row: Any, now: int, retention: int) -> None:
    if row.status == "staging" and row.updated_at < now - retention:
        raise HubError(410, "upload_expired", "This upload expired; start it again (init)")
    if row.status in CLOSED:
        raise HubError(410, f"upload_{row.status}", "This upload is closed; start it again (init)")
    if row.status != "staging":
        raise conflict(
            f"session_{row.status}", f"This upload is {row.status}; no more bytes are accepted"
        )


def _reset_file(hub: Hub, conn: Connection, session_id: str, path: str) -> None:
    """Forget a file that failed verification so it is sent again from byte 0."""
    hub.store.reset_file(session_id, path)
    conn.execute(
        delete(session_chunks).where(
            session_chunks.c.session_id == session_id, session_chunks.c.path == path
        )
    )
    conn.execute(
        update(session_files)
        .where(session_files.c.session_id == session_id, session_files.c.path == path)
        .values(received=0, verified=False)
    )


# -- seal and commit ----------------------------------------------------------


def begin_complete(
    hub: Hub, principal: Principal, session_id: str
) -> tuple[str, dict[str, Any] | str]:
    """Claim the seal of the caller's upload.

    Returns ("done", receipt) for a committed session, ("busy", view) while
    another sealer holds a live claim, or ("seal", token) when this caller now
    holds the claim and must run `seal` with the token.
    """
    now = hub.clock()
    lease = hub.settings.limits.seal_lease_seconds * 1000
    retention = hub.settings.limits.staging_retention_seconds * 1000
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, session_id, lock=True)
        if row.status == "committed":
            return "done", receipt(row)
        if row.status in CLOSED:
            raise HubError(
                410, f"upload_{row.status}", "This upload is closed; start it again (init)"
            )
        if row.status == "staging":
            if row.updated_at < now - retention:
                raise HubError(410, "upload_expired", "This upload expired; start it again (init)")
            files = _files(conn, session_id)
            missing = [f.path for f in files if not f.verified or f.received != f.size]
            if missing:
                raise conflict(
                    "session_incomplete",
                    f"{len(missing)} file(s) are not fully received",
                    missing=missing[:20],
                )
        elif row.problem_code == "artifact_conflict":
            raise HubError(
                409,
                "artifact_conflict",
                "A stored copy of this upload disagrees with its record and was kept untouched; "
                "abort the upload and send it again, or ask an operator",
            )
        elif row.seal_lease_until is not None and row.seal_lease_until > now:
            return "busy", session_row_view(row)
        token = secrets.token_hex(16)
        conn.execute(
            update(data_sessions)
            .where(data_sessions.c.id == session_id)
            .values(
                status="sealing", seal_token=token, seal_lease_until=now + lease, updated_at=now
            )
        )
    return "seal", token


def complete(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    """Claim and seal in the calling thread; the receipt or a typed refusal.

    (The HTTP route runs `seal` on the maintenance sealer instead and answers
    202 while it takes long.)"""
    state, value = begin_complete(hub, principal, session_id)
    if state == "done":
        assert isinstance(value, dict)
        return value
    if state == "busy":
        raise HubError(
            409,
            "sealing_in_progress",
            "This upload is being sealed; retry shortly",
            headers={"Retry-After": "5"},
        )
    assert isinstance(value, str)
    return seal(hub, session_id, value, actor=f"user:{principal.user_id}")


def _renewer(hub: Hub, session_id: str, token: str) -> Any:
    lease = hub.settings.limits.seal_lease_seconds * 1000

    def renew() -> None:
        with hub.db.transaction() as conn:
            kept = conn.execute(
                update(data_sessions)
                .where(
                    data_sessions.c.id == session_id,
                    data_sessions.c.status == "sealing",
                    data_sessions.c.seal_token == token,
                )
                .values(seal_lease_until=hub.clock() + lease)
            ).rowcount
        if not kept:
            raise SealLost(session_id)

    return renew


def _release(hub: Hub, session_id: str, token: str) -> bool:
    """Give up this sealer's claim so a retry can seal at once (only if it is
    still ours). Returns False when the database could not be reached; the
    claim's lease then runs out and reconciliation takes over, which is the
    designed fallback, so callers only log it before re-raising their own
    error."""
    try:
        with hub.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(
                    data_sessions.c.id == session_id,
                    data_sessions.c.status == "sealing",
                    data_sessions.c.seal_token == token,
                )
                .values(seal_lease_until=None)
            )
    except HubError:
        return False
    return True


def _release_or_note(hub: Hub, session_id: str, token: str) -> None:
    if not _release(hub, session_id, token):
        log.warning("session %s: the seal claim could not be released; it will lapse", session_id)


def _settled(hub: Hub, session_id: str) -> dict[str, Any]:
    """What a sealer that lost its claim reports: the committed receipt if
    the session got committed meanwhile, else a retryable refusal."""
    with hub.db.transaction() as conn:
        row = _row(conn, session_id)
    if row.status == "committed":
        return receipt(row)
    raise HubError(
        409,
        "sealing_in_progress",
        "This upload is being sealed by another attempt; retry shortly",
        headers={"Retry-After": "5"},
    )


def seal(hub: Hub, session_id: str, token: str, *, actor: str) -> dict[str, Any]:
    """Verify, install and commit an upload this caller holds the claim on."""
    with hub.db.transaction() as conn:
        row = _row(conn, session_id)
        files = _files(conn, session_id)
    if row.status == "committed":
        return receipt(row)
    if row.seal_token != token:
        return _settled(hub, session_id)
    manifest = _manifest(row, files)
    renew = _renewer(hub, session_id, token)
    try:
        bad = _install(hub, row, files, manifest, renew)
    except SealLost:
        return _settled(hub, session_id)
    except ArtifactConflict:
        _mark_conflict(hub, session_id, token, actor)
        raise HubError(
            409,
            "artifact_conflict",
            "A stored copy of this upload disagrees with its record and was kept untouched; "
            "abort the upload and send it again, or ask an operator",
        ) from None
    except BaseException as exc:
        _release_or_note(hub, session_id, token)
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HubError(
                507, "insufficient_storage", "The hub ran out of space while sealing; retry later"
            ) from None
        raise
    now = hub.clock()
    try:
        with hub.db.transaction() as conn:
            current = conn.execute(
                select(data_sessions).where(data_sessions.c.id == session_id).with_for_update()
            ).one()
            if current.status != "sealing" or current.seal_token != token:
                outcome = None
            elif bad:
                conn.execute(
                    update(data_sessions)
                    .where(data_sessions.c.id == session_id, data_sessions.c.seal_token == token)
                    .values(
                        status="staging", seal_lease_until=None, seal_token=None, updated_at=now
                    )
                )
                for path in bad:
                    _reset_file(hub, conn, session_id, path)
                audit(conn, now, actor, "session.seal_failed", session_id, {"files": bad[:20]})
                outcome = "reset"
            else:
                has_trials = any(is_trials_table(f.path) for f in files)
                conn.execute(
                    update(data_sessions)
                    .where(data_sessions.c.id == session_id, data_sessions.c.seal_token == token)
                    .values(
                        status="committed",
                        storage_key=hub.store.session_key(row.experiment_id, session_id),
                        completed_at=now,
                        updated_at=now,
                        seal_lease_until=None,
                        seal_token=None,
                        index_status="pending" if has_trials else "none",
                    )
                )
                audit(
                    conn,
                    now,
                    actor,
                    "session.commit",
                    session_id,
                    {"manifest": row.manifest_sha256},
                )
                outcome = "committed"
    except BaseException:
        _release_or_note(hub, session_id, token)
        raise
    if outcome is None:
        return _settled(hub, session_id)
    if outcome == "reset":
        raise conflict(
            "file_hash_mismatch",
            f"{len(bad)} staged file(s) failed verification and were discarded; send them again",
            paths=bad[:20],
        )
    # A takeover may have re-created staging beside the committed tree (bytes
    # sent again after a lost claim); the committed copy is the record.
    hub.store.remove_staging(session_id)
    with hub.db.transaction() as conn:
        return receipt(_row(conn, session_id))


def _mark_conflict(hub: Hub, session_id: str, token: str, actor: str) -> None:
    now = hub.clock()
    with hub.db.transaction() as conn:
        marked = conn.execute(
            update(data_sessions)
            .where(data_sessions.c.id == session_id, data_sessions.c.seal_token == token)
            .values(problem_code="artifact_conflict", problem_at=now, seal_lease_until=None)
        ).rowcount
        if marked:
            audit(conn, now, actor, "session.artifact_conflict", session_id, {})


def _manifest(row: Any, files: list[Any]) -> dict[str, Any]:
    return {
        "format": "alhazen-hub-session",
        "format_version": 1,
        "session_id": row.id,
        "owner_id": row.owner_id,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "client_session_id": row.client_session_id,
        "manifest_sha256": row.manifest_sha256,
        "metadata": json.loads(row.metadata),
        "files": [{"path": f.path, "size": int(f.size), "sha256": f.sha256} for f in files],
    }


def _install(
    hub: Hub, row: Any, files: list[Any], manifest: dict[str, Any], renew: Any
) -> list[str]:
    """Verify and durably install; return the paths that failed verification.

    A staged file that is missing or vanishes while being hashed is never
    called bad on that alone: another sealer may have installed the tree.
    """
    store = hub.store
    final = store.final_dir(row.experiment_id, row.id)
    if final.exists():
        _verify_final(hub, row, files, manifest, renew)
        return []
    bad, missing = [], False
    for f in files:
        renew()
        staged = store.staging_file(row.id, f.path)
        try:
            if staged.stat().st_size != f.size or sha256_file(staged, renew) != f.sha256:
                bad.append(f.path)
        except FileNotFoundError:
            missing = True
    if missing and final.exists():
        _verify_final(hub, row, files, manifest, renew)
        return []
    if missing:
        return [f.path for f in files if not store.staging_file(row.id, f.path).is_file()] + bad
    if bad:
        return bad
    renew()
    try:
        store.install_session(row.id, row.experiment_id, manifest)
    except (StorageError, FileNotFoundError):
        # Another sealer installed it first; verify theirs instead.
        if not final.exists():
            raise
        _verify_final(hub, row, files, manifest, renew)
        return []
    # Our own rename of the verified tree: the bytes are the ones just hashed.
    if store.read_final_manifest(row.experiment_id, row.id) != manifest:
        raise ArtifactConflict(row.id)
    return []


def _verify_final(
    hub: Hub, row: Any, files: list[Any], manifest: dict[str, Any], renew: Any
) -> None:
    """An existing final directory must match exactly; it is never replaced."""
    stored = hub.store.read_final_manifest(row.experiment_id, row.id)
    problem = None
    if stored != manifest:
        problem = "its manifest.json differs from the database record"
    else:
        for f in files:
            renew()
            path = hub.store.final_file(row.experiment_id, row.id, f.path)
            if (
                not path.is_file()
                or path.stat().st_size != f.size
                or sha256_file(path, renew) != f.sha256
            ):
                problem = "a committed file is missing or differs"
                break
    if problem is not None:
        log.error("session %s: final directory refused: %s", row.id, problem)
        raise ArtifactConflict(row.id)


# -- reconciliation -----------------------------------------------------------


TEMP_AGE_S = 24 * 3600


def reconcile(hub: Hub) -> dict[str, Any]:
    """Finish abandoned seals and make the stored state's truth visible.

    - Seals whose lease ran out (and that are not stuck on a recorded
      problem) are taken over with a new token and finished.
    - A committed session whose stored manifest is missing is marked
      ``artifact_missing`` (receipt, detail and list say so); the mark clears
      when the files are restored.
    - Leftovers it can prove safe are removed: staging folders of committed
      (final present), aborted or expired sessions, and interrupted package
      uploads (tmp/*.part) older than a day.
    - Folders and files no record explains are reported, never deleted.
    """
    now = hub.clock()
    lease = hub.settings.limits.seal_lease_seconds * 1000
    resumed, failed = 0, 0
    with hub.db.transaction() as conn:
        abandoned = conn.execute(
            select(data_sessions.c.id).where(
                data_sessions.c.status == "sealing",
                data_sessions.c.problem_code.is_(None),
                data_sessions.c.seal_lease_until.is_(None)
                | (data_sessions.c.seal_lease_until <= now),
            )
        ).all()
    for item in abandoned:
        token = secrets.token_hex(16)
        with hub.db.transaction() as conn:
            taken = conn.execute(
                update(data_sessions)
                .where(
                    data_sessions.c.id == item.id,
                    data_sessions.c.status == "sealing",
                    data_sessions.c.problem_code.is_(None),
                    data_sessions.c.seal_lease_until.is_(None)
                    | (data_sessions.c.seal_lease_until <= now),
                )
                .values(seal_token=token, seal_lease_until=now + lease)
            ).rowcount
        if not taken:
            continue
        try:
            seal(hub, item.id, token, actor="system:reconcile")
            resumed += 1
        except HubError as exc:
            failed += 1
            log.warning("reconcile: session %s not sealed: %s", item.id, exc.code)

    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(
                data_sessions.c.id,
                data_sessions.c.experiment_id,
                data_sessions.c.status,
                data_sessions.c.problem_code,
            )
        ).all()
        known_versions = {r.storage_key for r in conn.execute(select(versions.c.storage_key)).all()}
    by_id = {r.id: r for r in rows}
    missing_ids: list[str] = []
    restored = 0
    for r in rows:
        if r.status != "committed":
            continue
        present = hub.store.read_final_manifest(r.experiment_id, r.id) is not None
        if not present:
            missing_ids.append(r.id)
        if present != (r.problem_code != "artifact_missing"):
            restored += _set_missing(hub, r.id, missing=not present, now=now)
    stuck_ids = [r.id for r in rows if r.status == "sealing" and r.problem_code]
    held = {(r.experiment_id, r.id) for r in rows if r.status in ("committed", "sealing")}
    orphan_ids = [sid for eid, sid in hub.store.final_session_ids() if (eid, sid) not in held]

    removed_staging = 0
    unknown_staging: list[str] = []
    for sid in hub.store.staging_session_ids():
        owner_row = by_id.get(sid)
        if owner_row is None:
            unknown_staging.append(sid)
        elif owner_row.status in CLOSED or (
            owner_row.status == "committed"
            and hub.store.read_final_manifest(owner_row.experiment_id, owner_row.id) is not None
        ):
            hub.store.remove_staging(sid)
            removed_staging += 1
    removed_temps = 0
    for temp in hub.store.stale_temps(TEMP_AGE_S, time.time()):
        temp.unlink(missing_ok=True)
        removed_temps += 1
    orphan_releases = [k for k in hub.store.release_keys() if k not in known_versions]

    if missing_ids:
        log.error("reconcile: committed sessions without stored files: %s", ", ".join(missing_ids))
    if orphan_ids:
        log.warning("reconcile: stored session folders without a record: %s", ", ".join(orphan_ids))
    if stuck_ids:
        log.warning("reconcile: uploads stuck on artifact_conflict: %s", ", ".join(stuck_ids))
    return {
        "checked_at": iso(now),
        "seals_resumed": resumed,
        "seals_failed": failed,
        "missing_artifacts": len(missing_ids),
        "missing_artifact_ids": missing_ids[:50],
        "restored_marks_changed": restored,
        "orphan_artifacts": len(orphan_ids),
        "orphan_artifact_ids": orphan_ids[:50],
        "stuck_seals": len(stuck_ids),
        "stuck_seal_ids": stuck_ids[:50],
        "staging_removed": removed_staging,
        "unknown_staging": len(unknown_staging),
        "unknown_staging_ids": unknown_staging[:50],
        "temps_removed": removed_temps,
        "orphan_releases": len(orphan_releases),
    }


def _set_missing(hub: Hub, session_id: str, *, missing: bool, now: int) -> int:
    with hub.db.transaction() as conn:
        query = update(data_sessions).where(
            data_sessions.c.id == session_id, data_sessions.c.status == "committed"
        )
        if missing:
            changed = conn.execute(
                query.where(data_sessions.c.problem_code.is_(None)).values(
                    problem_code="artifact_missing", problem_at=now
                )
            ).rowcount
        else:
            changed = conn.execute(
                query.where(data_sessions.c.problem_code == "artifact_missing").values(
                    problem_code=None, problem_at=None
                )
            ).rowcount
        if changed:
            audit(
                conn,
                now,
                "system:reconcile",
                "session.artifact_missing" if missing else "session.artifact_restored",
                session_id,
                {},
            )
    return int(changed or 0)
