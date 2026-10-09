"""Operator actions on a hub, run on the server host with its configuration.

Every action is audited with the operator-supplied ``actor`` label. There is
no email: an invite code or a reset password is shown once to the operator,
who hands it to the person through a channel they already trust.

Schema changes are explicit: `init_database` creates the schema in an empty
database, `migrate_database` upgrades an existing one after the operator has
taken a backup. Neither ever drops or resets data, and the service itself
does neither on start.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update

from alhazen.hub import auth
from alhazen.hub.context import Hub, new_id, system_clock
from alhazen.hub.database import Database
from alhazen.hub.errors import HubError
from alhazen.hub.schema import SCHEMA_VERSION, create_schema, invites, migrate, read_version, users
from alhazen.hub.settings import HubSettings
from alhazen.hub.storage import ArtifactStore
from alhazen.hub.timefmt import iso


class AdminError(RuntimeError):
    """An operator action was refused; the message says why."""


@dataclass(frozen=True)
class InviteCode:
    id: str
    code: str  # shown once; only its hash is stored
    expires_at: str


def _hub(settings: HubSettings) -> Hub:
    return Hub(
        settings=settings,
        db=Database(settings),
        store=ArtifactStore(settings.artifact_root),
        clock=system_clock,
    )


def _actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 90:
        raise AdminError("actor must name the operator (1-90 characters)")
    return f"admin:{actor.strip()}"


def init_database(settings: HubSettings) -> int:
    """Create the schema in a database that has none. Idempotent at the
    current version; refuses any other existing state."""
    db = Database(settings)
    try:
        with db.engine.begin() as conn:
            found = read_version(conn)
            if found == SCHEMA_VERSION:
                return found
            if found is not None:
                raise AdminError(
                    f"the database is at hub schema {found}; "
                    "use migrate_database, not init_database"
                )
            create_schema(conn)
        ArtifactStore(settings.artifact_root)
        return SCHEMA_VERSION
    finally:
        db.dispose()


def migrate_database(settings: HubSettings) -> int:
    """Apply pending schema steps in one transaction. Take a backup first."""
    db = Database(settings)
    try:
        with db.engine.begin() as conn:
            return migrate(conn)
    finally:
        db.dispose()


def create_invite(
    settings: HubSettings, *, actor: str, note: str = "", expires_days: int | None = None
) -> InviteCode:
    who = _actor(actor)
    days = settings.auth.invite_days if expires_days is None else expires_days
    if not 1 <= days <= 90:
        raise AdminError("expires_days must be between 1 and 90")
    if len(note) > 200:
        raise AdminError("note must be at most 200 characters")
    hub = _hub(settings)
    try:
        code = "inv-" + secrets.token_urlsafe(18)
        now = hub.clock()
        invite_id = new_id()
        expires = now + days * 86_400_000
        with hub.db.transaction() as conn:
            conn.execute(
                invites.insert().values(
                    id=invite_id,
                    code_hash=auth.invite_hash(code),
                    note=note,
                    created_by=who,
                    created_at=now,
                    expires_at=expires,
                    used_at=None,
                    used_by=None,
                    revoked_at=None,
                )
            )
            auth.audit(
                conn, now, who, "invite.create", f"invite:{invite_id}", {"note": note, "days": days}
            )
        return InviteCode(id=invite_id, code=code, expires_at=iso(expires) or "")
    finally:
        hub.db.dispose()


def list_invites(settings: HubSettings) -> list[dict[str, Any]]:
    hub = _hub(settings)
    try:
        with hub.db.transaction() as conn:
            rows = conn.execute(select(invites).order_by(invites.c.created_at.desc())).all()
        now = hub.clock()
        return [
            {
                "id": r.id,
                "note": r.note,
                "created_by": r.created_by,
                "created_at": iso(r.created_at),
                "expires_at": iso(r.expires_at),
                "used_at": iso(r.used_at),
                "used_by": r.used_by,
                "revoked_at": iso(r.revoked_at),
                "usable": r.used_at is None and r.revoked_at is None and r.expires_at > now,
            }
            for r in rows
        ]
    finally:
        hub.db.dispose()


def revoke_invite(settings: HubSettings, invite_id: str, *, actor: str) -> bool:
    who = _actor(actor)
    hub = _hub(settings)
    try:
        now = hub.clock()
        with hub.db.transaction() as conn:
            changed = conn.execute(
                update(invites)
                .where(
                    invites.c.id == invite_id,
                    invites.c.revoked_at.is_(None),
                    invites.c.used_at.is_(None),
                )
                .values(revoked_at=now)
            ).rowcount
            if changed:
                auth.audit(conn, now, who, "invite.revoke", f"invite:{invite_id}", {})
        return bool(changed)
    finally:
        hub.db.dispose()


def _user_id(conn: Any, username: str) -> str:
    row = conn.execute(
        select(users.c.id).where(users.c.username == username.strip().lower())
    ).first()
    if row is None:
        raise AdminError(f"no user named {username!r}")
    return str(row.id)


def reset_password(settings: HubSettings, username: str, new_password: str, *, actor: str) -> int:
    """Set a new password and revoke every session of the account; return how
    many sessions were revoked."""
    who = _actor(actor)
    hub = _hub(settings)
    try:
        name = username.strip().lower()
        try:
            auth.check_password(hub, new_password, name)
        except HubError as exc:
            raise AdminError(exc.message) from None
        hashed = auth.hash_password(hub, new_password)
        now = hub.clock()
        with hub.db.transaction() as conn:
            user_id = _user_id(conn, name)
            conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(password_hash=hashed, password_changed_at=now)
            )
            revoked = auth.revoke_all(conn, user_id, now)
            auth.audit(
                conn,
                now,
                who,
                "user.reset_password",
                f"user:{user_id}",
                {"sessions_revoked": revoked},
            )
        return revoked
    finally:
        hub.db.dispose()


def disable_user(settings: HubSettings, username: str, *, actor: str) -> int:
    """Refuse every future sign-in and revoke current sessions. Data is kept."""
    who = _actor(actor)
    hub = _hub(settings)
    try:
        now = hub.clock()
        with hub.db.transaction() as conn:
            user_id = _user_id(conn, username)
            conn.execute(update(users).where(users.c.id == user_id).values(disabled_at=now))
            revoked = auth.revoke_all(conn, user_id, now)
            auth.audit(
                conn, now, who, "user.disable", f"user:{user_id}", {"sessions_revoked": revoked}
            )
        return revoked
    finally:
        hub.db.dispose()


def enable_user(settings: HubSettings, username: str, *, actor: str) -> None:
    who = _actor(actor)
    hub = _hub(settings)
    try:
        now = hub.clock()
        with hub.db.transaction() as conn:
            user_id = _user_id(conn, username)
            conn.execute(update(users).where(users.c.id == user_id).values(disabled_at=None))
            auth.audit(conn, now, who, "user.enable", f"user:{user_id}", {})
    finally:
        hub.db.dispose()


def reconcile(settings: HubSettings) -> dict[str, Any]:
    """Run seal reconciliation now and return its report (never deletes)."""
    from alhazen.hub.uploads import reconcile as run

    hub = _hub(settings)
    try:
        return run(hub)
    finally:
        hub.db.dispose()


def reindex_sessions(settings: HubSettings, session_id: str | None = None) -> int:
    """Rebuild derived trial rows from the committed raw files (all sessions,
    or one); return how many were indexed."""
    from sqlalchemy import select as sql_select

    from alhazen.hub.schema import data_sessions
    from alhazen.hub.trials import claim_next, index_session, request_reindex

    hub = _hub(settings)
    try:
        with hub.db.transaction() as conn:
            query = sql_select(data_sessions.c.id).where(data_sessions.c.status == "committed")
            if session_id is not None:
                query = query.where(data_sessions.c.id == session_id)
            ids = [r.id for r in conn.execute(query).all()]
        done = 0
        for sid in ids:
            request_reindex(hub, None, sid)
            claimed = claim_next(hub, sid)
            if claimed is not None:
                index_session(hub, claimed)
                done += 1
        return done
    finally:
        hub.db.dispose()


def expire_stale_uploads(settings: HubSettings) -> int:
    """Expire unfinished uploads untouched past the retention period now."""
    from alhazen.hub.uploads import expire_stale

    hub = _hub(settings)
    try:
        return expire_stale(hub)
    finally:
        hub.db.dispose()
