"""Private session uploads: reservation, verified chunks, seal and commit.

Hides the raw-upload state machine (review gate B1):

    staging --complete--> sealing --durable install--> committed
       |                     |
       +--abort/expiry-->  aborted / expired      (reservation released)

- `init` reserves the whole session's bytes against the owner's quota in the
  transaction that creates it; one (owner, client_session_id) names one
  manifest forever, so a replay returns the same session and different
  content is a conflict.
- `put_chunk` holds the session row lock (PostgreSQL ``FOR UPDATE``; SQLite's
  database write lock) for the whole chunk, so PUTs and a completion of one
  session never interleave. Bytes are written after truncating any
  unacknowledged tail, synced, and only then is progress committed with a
  durable chunk record (session, path, offset, length, sha256). A retry of
  a recorded chunk is a replay; anything else at a covered offset is a
  conflict. A file is hashed in full when its last byte arrives.
- `complete` claims the seal with a lease, re-verifies every staged file
  outside any transaction, installs the tree into its immutable final
  directory (synced files, folders, rename, parents), and commits the
  database pointer last. An existing final directory is verified, never
  overwritten. A crash anywhere leaves a state the next `complete` or the
  maintenance worker finishes or reports.
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
import shutil
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from alhazen.hub import packages
from alhazen.hub.auth import Principal, audit
from alhazen.hub.catalog import may_collect_with
from alhazen.hub.context import Hub, new_id
from alhazen.hub.errors import HubError, conflict, invalid, not_found, too_large
from alhazen.hub.quota import lock_owner, require_room
from alhazen.hub.schema import data_sessions, session_chunks, session_files
from alhazen.hub.storage import StorageError, sha256_file
from alhazen.hub.timefmt import iso
from alhazen.hub.trials import is_trials_table

log = logging.getLogger(__name__)

_SHA = re.compile(r"^[0-9a-f]{64}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
METADATA_KEYS = {"subject_code": 64, "mode": 32, "rig_alias": 64, "started_at": 40}
DURABILITY = "verified on the hub's primary storage; not an independent backup"


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
    """Canonical identity of an upload (docs/hub/server.md): sha256 of the
    compact, key-sorted UTF-8 JSON of experiment_id, version_id, the files
    sorted by path and the metadata."""
    canonical = {
        "experiment_id": parsed["experiment_id"],
        "version_id": parsed["version_id"],
        "files": parsed["files"],
        "metadata": parsed["metadata"],
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# -- views --------------------------------------------------------------------


def _files(conn: Connection, session_id: str) -> list[Any]:
    return list(
        conn.execute(
            select(session_files)
            .where(session_files.c.session_id == session_id)
            .order_by(session_files.c.path)
        ).all()
    )


def session_row_view(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "client_session_id": row.client_session_id,
        "status": row.status,
        "metadata": json.loads(row.metadata),
        "created_at": iso(row.created_at),
        "completed_at": iso(row.completed_at),
        "manifest_sha256": row.manifest_sha256,
        "total_bytes": int(row.total_bytes),
        "file_count": int(row.file_count),
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
    return {
        "id": row.id,
        "status": row.status,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "client_session_id": row.client_session_id,
        "manifest_sha256": row.manifest_sha256,
        "file_count": int(row.file_count),
        "total_bytes": int(row.total_bytes),
        "completed_at": iso(row.completed_at),
        "durability": DURABILITY,
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


# -- init ---------------------------------------------------------------------


def init_session(
    hub: Hub, principal: Principal, body: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    parsed = parse_init(hub, body)
    digest = manifest_digest(parsed)
    limits = hub.settings.limits
    now = hub.clock()
    stale: list[str] = []
    try:
        with hub.db.transaction() as conn:
            lock_owner(conn, principal.user_id)
            existing = conn.execute(
                select(data_sessions).where(
                    data_sessions.c.owner_id == principal.user_id,
                    data_sessions.c.client_session_id == parsed["client_session_id"],
                )
            ).first()
            if existing is not None:
                if existing.manifest_sha256 != digest:
                    raise conflict(
                        "session_conflict",
                        "This client_session_id already names an upload with different content",
                    )
                return progress_view(conn, existing), False
            may_collect_with(conn, principal, parsed["experiment_id"], parsed["version_id"])
            stale = _expire_owner_stale(hub, conn, principal.user_id, now)
            active = conn.execute(
                select(func.count())
                .select_from(data_sessions)
                .where(
                    data_sessions.c.owner_id == principal.user_id,
                    data_sessions.c.status.in_(("staging", "sealing")),
                )
            ).scalar_one()
            if active >= limits.max_staging_sessions:
                raise HubError(
                    429,
                    "upload_limit",
                    f"You have {active} unfinished uploads, the most allowed at once; finish or "
                    "abort one first",
                )
            require_room(conn, principal.user_id, parsed["total"], limits.user_quota_bytes)
            _require_disk(hub, conn, parsed["total"])
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
                    index_status="none",
                    index_claimed_until=None,
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
                        "verified": False,
                    }
                    for f in parsed["files"]
                ],
            )
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
                },
            )
            row = conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()
            view = progress_view(conn, row)
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
    return view, True


def _require_disk(hub: Hub, conn: Connection, adding: int) -> None:
    outstanding = conn.execute(
        select(func.coalesce(func.sum(session_files.c.size - session_files.c.received), 0))
        .select_from(
            session_files.join(data_sessions, data_sessions.c.id == session_files.c.session_id)
        )
        .where(data_sessions.c.status.in_(("staging", "sealing")))
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
        select(data_sessions.c.id).where(
            data_sessions.c.owner_id == owner_id,
            data_sessions.c.status == "staging",
            data_sessions.c.updated_at < horizon,
        )
    ).all()
    ids = [r.id for r in rows]
    for session_id in ids:
        _close(conn, session_id, "expired", now, actor="system")
    return ids


def _close(conn: Connection, session_id: str, status: str, now: int, *, actor: str) -> None:
    conn.execute(
        update(data_sessions)
        .where(data_sessions.c.id == session_id)
        .values(status=status, updated_at=now, seal_lease_until=None)
    )
    conn.execute(delete(session_chunks).where(session_chunks.c.session_id == session_id))
    audit(conn, now, actor, f"session.{status}", session_id, {})


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
        ids = [r.id for r in rows]
        for session_id in ids:
            _close(conn, session_id, "expired", now, actor="system")
    for session_id in ids:
        hub.store.remove_staging(session_id)
    return len(ids)


def abort_session(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, session_id, lock=True)
        if row.status in ("aborted", "expired"):
            return session_row_view(row)
        if row.status != "staging":
            raise conflict(
                f"session_{row.status}",
                "Only an unfinished upload can be aborted; committed sessions are kept",
            )
        _close(conn, session_id, "aborted", now, actor=f"user:{principal.user_id}")
        row = conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()
        view = session_row_view(row)
    hub.store.remove_staging(session_id)
    return view


def progress(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        return progress_view(conn, _owned(conn, principal, session_id, lock=False))


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
        if offset + length > size:
            raise invalid("The chunk runs past the end of the file", "chunk_out_of_range")
        if length == 0 and not (size == 0 and offset == 0):
            raise invalid("An empty chunk is only valid for an empty file", "empty_chunk")
        recorded = conn.execute(
            select(session_chunks).where(
                session_chunks.c.session_id == session_id,
                session_chunks.c.path == path,
                session_chunks.c.offset == offset,
            )
        ).first()
        if recorded is not None and length > 0:
            if int(recorded.length) == length and recorded.sha256 == chunk_sha:
                return _chunk_answer(file.path, size, received, bool(file.verified), replay=True)
            raise conflict(
                "chunk_conflict",
                "Different bytes were already received at this offset",
                received=received,
            )
        if file.verified and size == 0:
            return _chunk_answer(file.path, size, received, True, replay=True)
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
        if length:
            conn.execute(
                insert(session_chunks).values(
                    session_id=session_id, path=path, offset=offset, length=length, sha256=chunk_sha
                )
            )
        if new_received == size:
            whole = sha256_file(hub.store.staging_file(session_id, path))
            if whole == file.sha256:
                verified = True
            else:
                failed_hash = True
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
        raise HubError(410, "upload_expired", "This upload expired; start a new one")
    if row.status in ("aborted", "expired"):
        raise HubError(410, f"upload_{row.status}", "This upload is closed; start a new one")
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


def complete(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    now = hub.clock()
    lease = hub.settings.limits.seal_lease_seconds * 1000
    with hub.db.transaction() as conn:
        row = _owned(conn, principal, session_id, lock=True)
        if row.status == "committed":
            return receipt(row)
        if row.status in ("aborted", "expired"):
            raise HubError(410, f"upload_{row.status}", "This upload is closed; start a new one")
        if (
            row.status == "sealing"
            and row.seal_lease_until is not None
            and row.seal_lease_until > now
        ):
            raise HubError(
                409,
                "sealing_in_progress",
                "This upload is being sealed; retry shortly",
                headers={"Retry-After": "10"},
            )
        if row.status == "staging":
            files = _files(conn, session_id)
            missing = [f.path for f in files if not f.verified or f.received != f.size]
            if missing:
                raise conflict(
                    "session_incomplete",
                    f"{len(missing)} file(s) are not fully received and verified",
                    missing=missing[:20],
                )
        conn.execute(
            update(data_sessions)
            .where(data_sessions.c.id == session_id)
            .values(status="sealing", seal_lease_until=now + lease, updated_at=now)
        )
    return seal(hub, session_id, actor=f"user:{principal.user_id}")


def seal(hub: Hub, session_id: str, *, actor: str) -> dict[str, Any]:
    """Phases 2 and 3 for a session this caller holds the seal lease on."""
    with hub.db.transaction() as conn:
        row = conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()
        files = _files(conn, session_id)
    manifest = _manifest(row, files)
    try:
        with hub.transfers.slot():
            bad = _install(hub, row, files, manifest)
    except BaseException as exc:
        # Let the next attempt take the seal at once instead of waiting out the
        # lease; the state stays "sealing", so no PUT can slip in meanwhile.
        with hub.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session_id, data_sessions.c.status == "sealing")
                .values(seal_lease_until=None)
            )
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HubError(
                507, "insufficient_storage", "The hub ran out of space while sealing; retry later"
            ) from None
        raise
    now = hub.clock()
    with hub.db.transaction() as conn:
        current = conn.execute(
            select(data_sessions).where(data_sessions.c.id == session_id).with_for_update()
        ).one()
        if current.status == "committed":
            return receipt(current)
        if current.status != "sealing":
            raise conflict(f"session_{current.status}", f"This upload is {current.status}")
        if bad:
            for path in bad:
                _reset_file(hub, conn, session_id, path)
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session_id)
                .values(status="staging", seal_lease_until=None, updated_at=now)
            )
            audit(conn, now, actor, "session.seal_failed", session_id, {"files": bad[:20]})
        else:
            has_trials = any(is_trials_table(f.path) for f in files)
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session_id)
                .values(
                    status="committed",
                    storage_key=hub.store.session_key(row.experiment_id, session_id),
                    completed_at=now,
                    updated_at=now,
                    seal_lease_until=None,
                    index_status="pending" if has_trials else "none",
                )
            )
            audit(conn, now, actor, "session.commit", session_id, {"manifest": row.manifest_sha256})
        final = conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()
    if bad:
        raise conflict(
            "file_hash_mismatch",
            f"{len(bad)} staged file(s) failed verification and were discarded; send them again",
            paths=bad[:20],
        )
    return receipt(final)


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


def _install(hub: Hub, row: Any, files: list[Any], manifest: dict[str, Any]) -> list[str]:
    """Verify and durably install; return the paths that failed verification."""
    store = hub.store
    final = store.final_dir(row.experiment_id, row.id)
    if not final.exists():
        bad = []
        for f in files:
            staged = store.staging_file(row.id, f.path)
            if (
                not staged.is_file()
                or staged.stat().st_size != f.size
                or sha256_file(staged) != f.sha256
            ):
                bad.append(f.path)
        if bad:
            return bad
        try:
            store.install_session(row.id, row.experiment_id, manifest)
        except (StorageError, FileNotFoundError):
            # Another sealer installed it first (lease takeover); verify theirs.
            if not final.exists():
                raise
    _verify_final(hub, row, files, manifest)
    return []


def _verify_final(hub: Hub, row: Any, files: list[Any], manifest: dict[str, Any]) -> None:
    """An existing final directory must match exactly; it is never replaced."""
    stored = hub.store.read_final_manifest(row.experiment_id, row.id)
    problem = None
    if stored != manifest:
        problem = "its manifest.json differs from the database record"
    else:
        for f in files:
            path = hub.store.final_file(row.experiment_id, row.id, f.path)
            if not path.is_file() or path.stat().st_size != f.size or sha256_file(path) != f.sha256:
                problem = "a committed file is missing or differs"
                break
    if problem is not None:
        log.error("session %s: final directory refused: %s", row.id, problem)
        raise HubError(
            500,
            "artifact_conflict",
            "The hub found an inconsistent stored copy of this session and kept it untouched; "
            "an operator must reconcile it",
        )


# -- reconciliation -----------------------------------------------------------


def reconcile(hub: Hub) -> dict[str, Any]:
    """Finish abandoned seals, and report committed rows whose files are missing
    and final directories no committed row points to. Never deletes anything."""
    now = hub.clock()
    lease = hub.settings.limits.seal_lease_seconds * 1000
    resumed, failed = 0, 0
    with hub.db.transaction() as conn:
        abandoned = conn.execute(
            select(data_sessions.c.id).where(
                data_sessions.c.status == "sealing",
                (data_sessions.c.seal_lease_until.is_(None))
                | (data_sessions.c.seal_lease_until <= now),
            )
        ).all()
    for item in abandoned:
        with hub.db.transaction() as conn:
            taken = conn.execute(
                update(data_sessions)
                .where(
                    data_sessions.c.id == item.id,
                    data_sessions.c.status == "sealing",
                    (data_sessions.c.seal_lease_until.is_(None))
                    | (data_sessions.c.seal_lease_until <= now),
                )
                .values(seal_lease_until=now + lease)
            ).rowcount
        if not taken:
            continue
        try:
            seal(hub, item.id, actor="system:reconcile")
            resumed += 1
        except HubError as exc:
            failed += 1
            log.warning("reconcile: session %s not sealed: %s", item.id, exc.code)
    missing: list[str] = []
    with hub.db.transaction() as conn:
        committed = conn.execute(
            select(data_sessions.c.id, data_sessions.c.experiment_id).where(
                data_sessions.c.status == "committed"
            )
        ).all()
        known = {
            (r.experiment_id, r.id)
            for r in conn.execute(
                select(data_sessions.c.id, data_sessions.c.experiment_id).where(
                    data_sessions.c.status.in_(("committed", "sealing"))
                )
            ).all()
        }
    for item in committed:
        if hub.store.read_final_manifest(item.experiment_id, item.id) is None:
            missing.append(item.id)
    orphans = [sid for eid, sid in hub.store.final_session_ids() if (eid, sid) not in known]
    if missing:
        log.error("reconcile: %d committed session(s) have no stored files", len(missing))
    if orphans:
        log.warning("reconcile: %d stored session folder(s) have no committed record", len(orphans))
    return {
        "checked_at": iso(now),
        "seals_resumed": resumed,
        "seals_failed": failed,
        "missing_artifacts": len(missing),
        "orphan_artifacts": len(orphans),
    }
