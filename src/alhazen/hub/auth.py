"""Accounts, passwords, opaque sessions, invites and sign-in admission.

Hides: the password hash (Argon2id, 64 MiB, t=3, p=1 — review gate M1), how
session tokens are stored (only their SHA-256; the raw token exists in the
client and in the one response that issued it), how CSRF tokens are derived,
and the throttle bookkeeping.

Policy, all from settings.AuthPolicy:
- A browser session ends 12 h after sign-in or 1 h after its last use; a
  bearer token (rig/CLI) ends 12 h after issue. Both are revocable at once.
- Sign-in admission: at most N failed attempts per account and per client
  address in a sliding window, and at most M password hashes per minute
  for the whole service; an exhausted window answers 429 with Retry-After
  and nothing is locked permanently. Unknown usernames cost the same hash
  as known ones and get the same generic answer.
- Admission is atomic (auth-review finding 1): the counts and the new
  attempt rows are written in ONE transaction that every sign-in serialises
  on (SQLite: BEGIN IMMEDIATE; PostgreSQL: a transaction-scoped advisory
  lock). An attempt is recorded as a failure BEFORE its hash runs and turned
  into a success only if the password matched, so guesses still in flight
  count against the account and address limits.
- Invites are single use, stored hashed, expire, and are consumed in the
  same transaction that creates the account.
- Opening a session re-reads the account under a row lock, so a password
  reset or disable that commits while a hash runs wins (finding 4).

Every function that touches the database takes the `Hub`; each opens its
own short transactions, never one across a password hash.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import Connection, and_, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from alhazen.hub.context import Hub, new_id
from alhazen.hub.errors import HubError, conflict, invalid
from alhazen.hub.schema import audit_events, auth_attempts, auth_sessions, invites, users

HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1, type=Type.ID)
_DUMMY_HASH: str | None = None

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
COOKIE_KIND = "cookie"
BEARER_KIND = "bearer"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of one request."""

    user: dict[str, Any]
    session_id: str
    kind: str  # COOKIE_KIND | BEARER_KIND
    csrf_token: str | None

    @property
    def user_id(self) -> str:
        return str(self.user["id"])


# -- validation -------------------------------------------------------------


def normalize_username(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid("username must be a string")
    name = value.strip().lower()
    if not USERNAME.match(name):
        raise invalid(
            "username must be 3-32 characters: lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return name


def check_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise invalid("display_name must be a string")
    name = value.strip()
    if not 1 <= len(name) <= 80 or _has_control(name):
        raise invalid("display_name must be 1-80 printable characters")
    return name


def check_password(hub: Hub, value: Any, username: str) -> str:
    policy = hub.settings.auth
    if not isinstance(value, str):
        raise invalid("password must be a string")
    if not policy.min_password_length <= len(value) <= policy.max_password_length:
        raise invalid(
            f"password must be {policy.min_password_length}-{policy.max_password_length} characters"
        )
    if value.strip().lower() == username:
        raise invalid("password must not be the username")
    return value


# Categories refused in any text a person reads (display names, experiment
# metadata): controls, format characters (bidi overrides and isolates,
# zero-width joiners/spaces, soft hyphen), line/paragraph separators and lone
# surrogates (invalid in UTF-8, so they could not even be stored).
_HIDDEN = frozenset(("Cc", "Cf", "Zl", "Zp", "Cs"))


def has_hidden_characters(text: str, *, multiline: bool = False) -> bool:
    """Whether ``text`` holds a character that is invisible, reorders the
    text around it, or is a control (``\\n`` and ``\\t`` allowed when
    ``multiline``). One rule for every human-readable field (finding 7)."""
    allowed = ("\n", "\t") if multiline else ()
    return any(ch not in allowed and unicodedata.category(ch) in _HIDDEN for ch in text)


def _has_control(text: str) -> bool:
    return has_hidden_characters(text)


# -- tokens -----------------------------------------------------------------


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def csrf_for(raw: str) -> str:
    """The CSRF token bound to one browser session token.

    Derived, not stored: it can only be computed by someone holding the
    HttpOnly session cookie (the server), and it changes with every sign-in.
    """
    return hashlib.sha256(b"alhazen-hub-csrf\x00" + raw.encode("utf-8")).hexdigest()


def same_secret(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def public_user(row: Any) -> dict[str, Any]:
    return {"id": row.id, "username": row.username, "display_name": row.display_name}


# -- passwords --------------------------------------------------------------


def hash_password(hub: Hub, password: str) -> str:
    with hub.hashes.slot():
        return HASHER.hash(password)


def _verify(hub: Hub, stored: str | None, password: str) -> bool:
    global _DUMMY_HASH
    with hub.hashes.slot():
        if stored is None:
            # Same work for an unknown username, so timing does not reveal it.
            if _DUMMY_HASH is None:
                _DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(16))
            try:
                HASHER.verify(_DUMMY_HASH, password)
            except VerifyMismatchError:
                return False
            return False
        try:
            return HASHER.verify(stored, password)
        except VerifyMismatchError:
            return False


# -- admission --------------------------------------------------------------


def _count(conn: Connection, scope: str, subject: str, since: int, *, success: bool | None) -> int:
    query = (
        select(func.count())
        .select_from(auth_attempts)
        .where(
            auth_attempts.c.scope == scope,
            auth_attempts.c.subject == subject,
            auth_attempts.c.at >= since,
        )
    )
    if success is not None:
        query = query.where(auth_attempts.c.success == success)
    return int(conn.execute(query).scalar_one())


def _oldest(conn: Connection, scope: str, subject: str, since: int) -> int:
    value = conn.execute(
        select(func.min(auth_attempts.c.at)).where(
            auth_attempts.c.scope == scope,
            auth_attempts.c.subject == subject,
            auth_attempts.c.at >= since,
        )
    ).scalar_one()
    return int(value) if value is not None else since


def _throttled(retry_ms: int) -> HubError:
    seconds = max(1, (retry_ms + 999) // 1000)
    return HubError(
        429,
        "rate_limited",
        "Too many attempts; wait and try again",
        headers={"Retry-After": str(seconds)},
    )


# One fixed key for the advisory lock every admission takes on PostgreSQL.
_ADMISSION_LOCK_KEY = 0x616C68617A656E31  # "alhazen1"


def _serialize_admission(conn: Connection) -> None:
    """Make this transaction the only one deciding admission right now.

    SQLite needs nothing: every hub transaction starts with BEGIN IMMEDIATE,
    the whole-database write lock. On PostgreSQL a transaction-scoped
    advisory lock is taken (released at commit or rollback; re-entrant in
    one transaction), so a count and the insert that follows it cannot
    interleave with another sign-in's. Admission transactions are a few
    small statements, so serialising them costs nothing at hub scale.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMISSION_LOCK_KEY})


def _admit_hash(hub: Hub, conn: Connection, now: int) -> None:
    """The service-wide password-hash budget, recorded as it is spent."""
    _serialize_admission(conn)
    since = now - 60_000
    if _count(conn, "hash", "*", since, success=None) >= hub.settings.auth.hashes_per_minute:
        raise _throttled(_oldest(conn, "hash", "*", since) + 60_000 - now)
    conn.execute(insert(auth_attempts).values(scope="hash", subject="*", at=now, success=True))


@dataclass(frozen=True)
class _Admission:
    """The attempt rows written for one admitted sign-in, still marked failed."""

    attempt_ids: tuple[int, ...]


def _admit_sign_in(hub: Hub, conn: Connection, username: str, address: str, now: int) -> _Admission:
    """Admit one password check or refuse it, atomically.

    The account and address rows are inserted as failures here, in the same
    serialised transaction as the counts, so concurrent guesses see each
    other; `_settle` marks them successful if the password matched.
    """
    _serialize_admission(conn)
    policy = hub.settings.auth
    window = policy.failure_window_seconds * 1000
    since = now - window
    for scope, subject, limit in (
        ("account", username, policy.failures_per_account),
        ("address", address, policy.failures_per_address),
    ):
        if _count(conn, scope, subject, since, success=False) >= limit:
            raise _throttled(_oldest(conn, scope, subject, since) + window - now)
    _admit_hash(hub, conn, now)
    ids = []
    for scope, subject in (("account", username), ("address", address)):
        ids.append(
            int(
                conn.execute(
                    insert(auth_attempts)
                    .values(scope=scope, subject=subject, at=now, success=False)
                    .returning(auth_attempts.c.id)
                ).scalar_one()
            )
        )
    return _Admission(tuple(ids))


def _settle(conn: Connection, admission: _Admission) -> None:
    """The password matched: these attempts no longer count as failures."""
    conn.execute(
        update(auth_attempts)
        .where(auth_attempts.c.id.in_(admission.attempt_ids))
        .values(success=True)
    )


def _withdraw(hub: Hub, admission: _Admission) -> None:
    """The check never ran (no hashing slot): forget the pending failures,
    so a busy service does not count against the person signing in."""
    with hub.db.transaction() as conn:
        conn.execute(delete(auth_attempts).where(auth_attempts.c.id.in_(admission.attempt_ids)))


def purge_attempts(hub: Hub) -> int:
    """Forget throttle records older than any window that still reads them."""
    horizon = hub.clock() - max(hub.settings.auth.failure_window_seconds * 1000, 3_600_000)
    with hub.db.transaction() as conn:
        result = conn.execute(delete(auth_attempts).where(auth_attempts.c.at < horizon))
        return int(result.rowcount or 0)


PURGE_BATCH = 1000


def purge_sessions(hub: Hub, *, max_batches: int = 100) -> int:
    """Delete sign-in sessions that ended (expired or revoked) more than
    ``auth.session_retention_seconds`` ago; return how many (finding 8).

    A dead session can never authenticate again, so its row is only history;
    the audit log keeps sign-in, reset and disable events. Work is bounded:
    batches of `PURGE_BATCH` rows in their own short transactions, at most
    ``max_batches`` per call (the next housekeeping pass continues).
    """
    horizon = hub.clock() - hub.settings.auth.session_retention_seconds * 1000
    removed = 0
    for _ in range(max_batches):
        with hub.db.transaction() as conn:
            ids = (
                conn.execute(
                    select(auth_sessions.c.id)
                    .where(
                        (auth_sessions.c.expires_at < horizon)
                        | (auth_sessions.c.revoked_at < horizon)
                    )
                    .limit(PURGE_BATCH)
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            conn.execute(delete(auth_sessions).where(auth_sessions.c.id.in_(ids)))
            removed += len(ids)
        if len(ids) < PURGE_BATCH:
            break
    return removed


def housekeeping(hub: Hub) -> dict[str, int]:
    """The auth part of the hourly housekeeping pass."""
    return {"attempts": purge_attempts(hub), "sessions": purge_sessions(hub)}


# -- registration -----------------------------------------------------------


def invite_hash(code: str) -> str:
    return hashlib.sha256(b"alhazen-hub-invite\x00" + code.encode("utf-8")).hexdigest()


def register(
    hub: Hub, *, username: Any, display_name: Any, password: Any, invite_code: Any, address: str
) -> dict[str, Any]:
    name = normalize_username(username)
    display = check_display_name(display_name)
    secret = check_password(hub, password, name)
    if not isinstance(invite_code, str) or not 8 <= len(invite_code) <= 200:
        raise invalid("The invite code is not valid", "invalid_invite")
    code_hash = invite_hash(invite_code.strip())
    now = hub.clock()
    with hub.db.transaction() as conn:
        _serialize_admission(conn)
        hour_ago = now - 3_600_000
        limit = hub.settings.auth.registrations_per_address_per_hour
        if _count(conn, "register", address, hour_ago, success=None) >= limit:
            raise _throttled(_oldest(conn, "register", address, hour_ago) + 3_600_000 - now)
        conn.execute(
            insert(auth_attempts).values(scope="register", subject=address, at=now, success=True)
        )
    with hub.db.transaction() as conn:
        invite = _usable_invite(conn, code_hash, now)
        if invite is None:
            raise invalid("The invite code is not valid", "invalid_invite")
        _admit_hash(hub, conn, now)
    hashed = hash_password(hub, secret)
    now = hub.clock()
    user_id = new_id()
    try:
        with hub.db.transaction() as conn:
            used = conn.execute(
                update(invites)
                .where(
                    invites.c.code_hash == code_hash,
                    invites.c.used_at.is_(None),
                    invites.c.revoked_at.is_(None),
                    invites.c.expires_at > now,
                )
                .values(used_at=now, used_by=user_id)
            ).rowcount
            if used != 1:
                raise invalid("The invite code is not valid", "invalid_invite")
            conn.execute(
                insert(users).values(
                    id=user_id,
                    username=name,
                    display_name=display,
                    password_hash=hashed,
                    created_at=now,
                    password_changed_at=now,
                    disabled_at=None,
                )
            )
            audit(conn, now, f"user:{user_id}", "register", f"user:{user_id}", {"username": name})
    except IntegrityError:
        raise conflict("username_taken", "That username is taken") from None
    return {"id": user_id, "username": name, "display_name": display}


def _usable_invite(conn: Connection, code_hash: str, now: int) -> Any:
    return conn.execute(
        select(invites.c.id).where(
            invites.c.code_hash == code_hash,
            invites.c.used_at.is_(None),
            invites.c.revoked_at.is_(None),
            invites.c.expires_at > now,
        )
    ).first()


# -- sign-in and sessions ---------------------------------------------------


def sign_in(
    hub: Hub, *, username: Any, password: Any, address: str, kind: str
) -> tuple[dict[str, Any], str, int]:
    """Check credentials and open a session; return (user, raw token, expires_at)."""
    if not isinstance(username, str) or not isinstance(password, str):
        raise invalid("username and password are required")
    name = username.strip().lower()[:64]
    if len(password) > hub.settings.auth.max_password_length:
        raise HubError(401, "invalid_credentials", "Username or password is incorrect")
    now = hub.clock()
    with hub.db.transaction() as conn:
        admission = _admit_sign_in(hub, conn, name, address, now)
        row = conn.execute(
            select(users).where(users.c.username == name, users.c.disabled_at.is_(None))
        ).first()
    try:
        ok = _verify(hub, row.password_hash if row is not None else None, password)
    except HubError:
        # No hashing slot within the wait: the check never happened.
        _withdraw(hub, admission)
        raise
    if not ok or row is None:
        # Already recorded as a failure at admission.
        raise HubError(401, "invalid_credentials", "Username or password is incorrect")
    now = hub.clock()
    raw = secrets.token_urlsafe(32)
    policy = hub.settings.auth
    lifetime = policy.browser_max_seconds if kind == COOKIE_KIND else policy.token_seconds
    expires = now + lifetime * 1000
    rehash = HASHER.check_needs_rehash(row.password_hash)
    new_hash = hash_password(hub, password) if rehash else None
    with hub.db.transaction() as conn:
        # The account may have been disabled or its password reset while the
        # hash ran. The row lock (PostgreSQL; SQLite holds the database lock)
        # waits for such a change to commit and then reads it, so a session
        # is opened only against the state the password was checked for.
        current = conn.execute(
            select(users.c.password_changed_at)
            .where(users.c.id == row.id, users.c.disabled_at.is_(None))
            .with_for_update()
        ).first()
        if current is None or current.password_changed_at != row.password_changed_at:
            raise HubError(401, "invalid_credentials", "Username or password is incorrect")
        _settle(conn, admission)
        if new_hash is not None:
            conn.execute(update(users).where(users.c.id == row.id).values(password_hash=new_hash))
        conn.execute(
            insert(auth_sessions).values(
                id=new_id(),
                user_id=row.id,
                token_hash=token_hash(raw),
                kind=kind,
                created_at=now,
                last_seen_at=now,
                expires_at=expires,
                revoked_at=None,
            )
        )
    return public_user(row), raw, expires


def authenticate(hub: Hub, raw: str, kind: str) -> Principal | None:
    """The live session for a presented token of the given transport, or None."""
    if not raw or len(raw) > 200:
        return None
    now = hub.clock()
    idle = hub.settings.auth.browser_idle_seconds * 1000
    with hub.db.transaction() as conn:
        row = conn.execute(
            select(auth_sessions, users.c.username, users.c.display_name)
            .join(users, users.c.id == auth_sessions.c.user_id)
            .where(
                auth_sessions.c.token_hash == token_hash(raw),
                auth_sessions.c.revoked_at.is_(None),
                auth_sessions.c.expires_at > now,
                users.c.disabled_at.is_(None),
            )
        ).first()
        if row is None or row.kind != kind:
            return None
        if kind == COOKIE_KIND and row.last_seen_at + idle <= now:
            return None
        if now - row.last_seen_at >= 60_000:
            conn.execute(
                update(auth_sessions).where(auth_sessions.c.id == row.id).values(last_seen_at=now)
            )
    user = {"id": row.user_id, "username": row.username, "display_name": row.display_name}
    return Principal(
        user=user,
        session_id=row.id,
        kind=kind,
        csrf_token=csrf_for(raw) if kind == COOKIE_KIND else None,
    )


def revoke_session(hub: Hub, session_id: str) -> None:
    with hub.db.transaction() as conn:
        conn.execute(
            update(auth_sessions)
            .where(auth_sessions.c.id == session_id, auth_sessions.c.revoked_at.is_(None))
            .values(revoked_at=hub.clock())
        )


def revoke_all(conn: Connection, user_id: str, now: int) -> int:
    result = conn.execute(
        update(auth_sessions)
        .where(and_(auth_sessions.c.user_id == user_id, auth_sessions.c.revoked_at.is_(None)))
        .values(revoked_at=now)
    )
    return int(result.rowcount or 0)


# -- audit ------------------------------------------------------------------


def audit(
    conn: Connection, now: int, actor: str, action: str, target: str, detail: dict[str, Any]
) -> None:
    """Append one audit event. ``detail`` must never hold a secret."""
    conn.execute(
        insert(audit_events).values(
            at=now,
            actor=actor[:100],
            action=action[:64],
            target=target[:200],
            detail=json.dumps(detail, sort_keys=True),
        )
    )
