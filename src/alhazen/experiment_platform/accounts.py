"""Accounts and sessions for the self-hosted server, in one SQLite file.

Passwords are hashed with scrypt (stdlib ``hashlib.scrypt``, n=2**15, r=8, p=1, a 16-byte
random salt per password) and compared in constant time. A session is a random 32-byte
token given to the browser in an HttpOnly cookie; only its SHA-256 is stored, with a
per-session CSRF token, an absolute expiry and the user it belongs to. Sign-in failures
are throttled per user name and per client address.

The decision this module hides is how identity is proven and remembered. The rest of the
platform sees only a ``Principal(uid, name)``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

from alhazen.experiment_platform.errors import AuthError, Conflict, InvalidRequest, NotFound
from alhazen.experiment_platform.service import Principal

_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
MIN_PASSWORD = 12
MAX_PASSWORD = 1024
FAILURES_BEFORE_LOCK = 5
LOCK_SECONDS = 15 * 60
SESSION_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  uid TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  password TEXT NOT NULL,
  created INTEGER NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  uid TEXT NOT NULL REFERENCES users(uid),
  csrf TEXT NOT NULL,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, dklen=32, maxmem=128 * 1024 * 1024, **_SCRYPT
    )
    params = f"{_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}"
    return f"scrypt${params}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=128 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Accounts:
    """The accounts database at ``path`` (created with its tables if missing)."""

    def __init__(self, path: Path, *, session_days: float = SESSION_DAYS) -> None:
        self.path = Path(path)
        self.session_seconds = int(session_days * 86400)
        self._local = threading.local()
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._failure_lock = threading.Lock()
        # A dummy hash to verify against when the user does not exist, so a missing user
        # takes as long to refuse as a wrong password.
        self._dummy = hash_password(secrets.token_urlsafe(16))
        with self._db() as db:
            db.executescript(_SCHEMA)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ users
    def add_user(self, username: str, name: str, password: str) -> Principal:
        username = username.strip().lower()
        if not _USERNAME.match(username):
            raise InvalidRequest("User names are 2-40 characters: a-z, 0-9, '.', '_', '-'")
        name = name.strip() or username
        if len(name) > 80:
            raise InvalidRequest("The display name is longer than 80 characters")
        self._check_password(password)
        uid = "u" + secrets.token_hex(6)
        try:
            with self._db() as db:
                db.execute(
                    "INSERT INTO users (uid, username, name, password, created) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uid, username, name, hash_password(password), int(time.time())),
                )
        except sqlite3.IntegrityError as error:
            raise Conflict(f"The user name {username!r} is taken") from error
        return Principal(uid, name)

    @staticmethod
    def _check_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise InvalidRequest(f"Passwords have at least {MIN_PASSWORD} characters")
        if len(password) > MAX_PASSWORD:
            raise InvalidRequest("The password is too long")

    def set_password(self, username: str, password: str) -> None:
        self._check_password(password)
        with self._db() as db:
            changed = db.execute(
                "UPDATE users SET password = ? WHERE username = ?",
                (hash_password(password), username.strip().lower()),
            ).rowcount
            if not changed:
                raise NotFound(f"No user {username!r}")
            db.execute(
                "DELETE FROM sessions WHERE uid = (SELECT uid FROM users WHERE username = ?)",
                (username.strip().lower(),),
            )

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._db() as db:
            changed = db.execute(
                "UPDATE users SET disabled = ? WHERE username = ?",
                (1 if disabled else 0, username.strip().lower()),
            ).rowcount
            if not changed:
                raise NotFound(f"No user {username!r}")
            if disabled:
                db.execute(
                    "DELETE FROM sessions WHERE uid = (SELECT uid FROM users WHERE username = ?)",
                    (username.strip().lower(),),
                )

    def users(self) -> list[dict[str, object]]:
        rows = self._db().execute(
            "SELECT uid, username, name, created, disabled FROM users ORDER BY username"
        )
        return [
            {"uid": r[0], "username": r[1], "name": r[2], "created": r[3], "disabled": bool(r[4])}
            for r in rows
        ]

    # ------------------------------------------------------------------ sessions
    def _throttled(self, keys: list[str]) -> bool:
        now = time.monotonic()
        with self._failure_lock:
            for key in keys:
                recent = [t for t in self._failures[key] if now - t < LOCK_SECONDS]
                self._failures[key] = recent
                if len(recent) >= FAILURES_BEFORE_LOCK:
                    return True
        return False

    def _fail(self, keys: list[str]) -> None:
        now = time.monotonic()
        with self._failure_lock:
            for key in keys:
                self._failures[key].append(now)

    def login(self, username: str, password: str, client: str = "") -> tuple[str, str, Principal]:
        """(session token, csrf token, principal). Raises AuthError with one message for
        every failure, so the reply never says whether the user name exists."""
        username = str(username or "").strip().lower()
        keys = [f"user:{username}", f"client:{client}"]
        if self._throttled(keys):
            raise AuthError("Too many failed sign-ins; wait a few minutes and try again")
        row = (
            self._db()
            .execute(
                "SELECT uid, name, password, disabled FROM users WHERE username = ?", (username,)
            )
            .fetchone()
        )
        ok = verify_password(str(password or ""), row[2] if row else self._dummy)
        if not row or not ok or row[3]:
            self._fail(keys)
            raise AuthError("The user name or password is not right")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = int(time.time())
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            db.execute(
                "INSERT INTO sessions (token_hash, uid, csrf, created, expires) "
                "VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), row[0], csrf, now, now + self.session_seconds),
            )
        return token, csrf, Principal(row[0], row[1])

    def session(self, token: str | None) -> tuple[Principal, str] | None:
        """The principal and CSRF token of a live session, or None."""
        if not token or len(token) > 200:
            return None
        row = (
            self._db()
            .execute(
                "SELECT u.uid, u.name, s.csrf, s.expires, u.disabled "
                "FROM sessions s JOIN users u ON u.uid = s.uid "
                "WHERE s.token_hash = ?",
                (_token_hash(token),),
            )
            .fetchone()
        )
        if not row or row[3] < time.time() or row[4]:
            return None
        return Principal(row[0], row[1]), row[2]

    def logout(self, token: str | None) -> None:
        if token:
            with self._db() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def backup(self, dest: Path) -> Path:
        """A consistent copy of the database (SQLite's online backup)."""
        dest = Path(dest)
        target = sqlite3.connect(dest)
        try:
            self._db().backup(target)
        finally:
            target.close()
        return dest
