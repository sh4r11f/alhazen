"""The collecting user's private view of their committed sessions.

Hides: the queries behind /data. Every function takes the caller and scopes
each lookup to sessions THEY collected (owner_id), whoever wrote the code
that produced them (review gate M2). Only committed sessions appear here;
uploads in progress are visible only through the upload protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from alhazen.hub.auth import Principal
from alhazen.hub.context import Hub
from alhazen.hub.errors import HubError, invalid, not_found
from alhazen.hub.schema import data_sessions, experiments, library, publications, session_files
from alhazen.hub.uploads import receipt, session_row_view


def _committed(conn: Any, principal: Principal, session_id: str) -> Any:
    row = conn.execute(
        select(data_sessions).where(
            data_sessions.c.id == session_id,
            data_sessions.c.owner_id == principal.user_id,
            data_sessions.c.status == "committed",
        )
    ).first()
    if row is None:
        raise not_found("Session not found")
    return row


def _experiment_title(conn: Any, principal: Principal, experiment_id: str) -> str | None:
    """A title the caller is entitled to see: their own, the public one, or
    the one they saw when pinning it."""
    own = conn.execute(
        select(experiments.c.title).where(
            experiments.c.id == experiment_id, experiments.c.owner_id == principal.user_id
        )
    ).scalar_one_or_none()
    if own is not None:
        return str(own)
    public = conn.execute(
        select(publications.c.title).where(publications.c.experiment_id == experiment_id)
    ).scalar_one_or_none()
    if public is not None:
        return str(public)
    pinned = conn.execute(
        select(library.c.title).where(
            library.c.user_id == principal.user_id, library.c.experiment_id == experiment_id
        )
    ).scalar_one_or_none()
    return str(pinned) if pinned is not None else None


def list_sessions(
    hub: Hub,
    principal: Principal,
    *,
    experiment_id: str | None,
    subject_code: str | None,
    mode: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    query = select(data_sessions).where(
        data_sessions.c.owner_id == principal.user_id, data_sessions.c.status == "committed"
    )
    if experiment_id:
        query = query.where(data_sessions.c.experiment_id == experiment_id)
    if subject_code:
        query = query.where(data_sessions.c.subject_code == subject_code)
    if mode:
        query = query.where(data_sessions.c.mode == mode)
    with hub.db.transaction() as conn:
        rows = conn.execute(
            query.order_by(data_sessions.c.completed_at.desc(), data_sessions.c.id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
        items = []
        for row in rows[:limit]:
            view = session_row_view(row)
            view["experiment_title"] = _experiment_title(conn, principal, row.experiment_id)
            items.append(view)
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


def session_detail(hub: Hub, principal: Principal, session_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        row = _committed(conn, principal, session_id)
        files = conn.execute(
            select(session_files)
            .where(session_files.c.session_id == session_id)
            .order_by(session_files.c.path)
        ).all()
        view = session_row_view(row)
        view["experiment_title"] = _experiment_title(conn, principal, row.experiment_id)
    return {
        "session": view,
        "receipt": receipt(row),
        "artifacts": [{"path": f.path, "size": int(f.size), "sha256": f.sha256} for f in files],
        "columns": json.loads(row.index_columns) if row.index_columns else [],
    }


def index_state(hub: Hub, principal: Principal, session_id: str) -> tuple[Any, list[str]]:
    with hub.db.transaction() as conn:
        row = _committed(conn, principal, session_id)
    return row, json.loads(row.index_columns) if row.index_columns else []


def require_indexed(row: Any) -> None:
    if row.index_status != "indexed":
        raise HubError(
            409,
            "index_not_ready",
            f"This session's trial index is {row.index_status}"
            + (f": {row.index_error}" if row.index_error else "")
            + "; its original files remain downloadable",
            extra={"index_status": row.index_status},
        )


def artifact(hub: Hub, principal: Principal, session_id: str, path: str) -> tuple[Path, str, str]:
    """(file, download name, sha256) for one committed file of the caller's session.

    The path must be one the session's manifest names exactly; it is never
    joined onto the filesystem otherwise.
    """
    if not path or len(path) > 1024:
        raise invalid("path is required")
    with hub.db.transaction() as conn:
        row = _committed(conn, principal, session_id)
        file = conn.execute(
            select(session_files).where(
                session_files.c.session_id == session_id, session_files.c.path == path
            )
        ).first()
    if file is None:
        raise not_found("File not found")
    target = hub.store.final_file(row.experiment_id, session_id, file.path)
    if not target.is_file():
        raise HubError(
            500,
            "artifact_missing",
            "The stored copy of this file is missing; an operator must restore it",
        )
    return target, file.path.rsplit("/", 1)[-1], file.sha256
