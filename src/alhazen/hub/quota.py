"""One owner's byte budget: reserved staging uploads, committed sessions and
stored package versions all count (review gate M4).

Callers check inside the transaction that also creates the new bytes'
record, after `lock_owner`, so two concurrent requests cannot both pass a
check that only one of them fits.
"""

from __future__ import annotations

from sqlalchemy import Connection, func, select

from alhazen.hub.errors import too_large
from alhazen.hub.schema import data_sessions, experiments, users, versions

HOLDING = ("staging", "sealing", "committed")


def lock_owner(conn: Connection, user_id: str) -> None:
    """Serialize quota decisions for one owner (row lock on PostgreSQL; the
    SQLite development backend already holds the database write lock)."""
    conn.execute(select(users.c.id).where(users.c.id == user_id).with_for_update()).first()


def owner_bytes(conn: Connection, user_id: str) -> int:
    sessions = conn.execute(
        select(func.coalesce(func.sum(data_sessions.c.total_bytes), 0)).where(
            data_sessions.c.owner_id == user_id, data_sessions.c.status.in_(HOLDING)
        )
    ).scalar_one()
    packages = conn.execute(
        select(func.coalesce(func.sum(versions.c.size), 0))
        .select_from(versions.join(experiments, experiments.c.id == versions.c.experiment_id))
        .where(experiments.c.owner_id == user_id)
    ).scalar_one()
    return int(sessions) + int(packages)


def require_room(conn: Connection, user_id: str, adding: int, quota: int) -> None:
    used = owner_bytes(conn, user_id)
    if used + adding > quota:
        raise too_large(
            f"This would exceed your storage quota ({quota} bytes; {used} in use)",
            "quota_exceeded",
        )
