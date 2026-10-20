"""Machine tokens: how a rig's dashboard reaches its owner's library on a platform host.

A browser proves who it is with the local host's per-launch token or the server's session
cookie; neither suits a dashboard on a rig that runs for weeks. A **machine token** does: the
person makes one per machine on the platform's Machines page (shown once, stored only as its
SHA-256), the dashboard keeps it in ``library.json`` (``alhazen library connect``) and sends it
as ``Authorization: Bearer <token>``. It is scoped to its owner's own library: it lists their
copies, downloads their revisions and reports what happened on the machine
(``POST /api/machines/events``), and nothing else (webapp.MACHINE_ROUTES). Revoking it, or
disabling the account, cuts the machine off at its next request.

Events are the reports a dashboard sends when it closes (``dashboard-closed``: the machine,
its alhazen, when it ran and the sessions run meanwhile); the Machines page lists the recent
ones. They are bounded in size and count per person.

One SQLite file in the platform store (``machines.sqlite3``), shared by the local and the
server host. The decision this module hides is how a machine is proven and remembered; the
rest of the platform sees a ``Principal`` and a machine id.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from alhazen.experiment_platform.errors import InvalidRequest, NotFound

FILE = "machines.sqlite3"
TOKEN_PREFIX = "alm_"
MAX_MACHINES = 50          # active tokens per person
MAX_EVENTS = 200           # kept per person, newest first
MAX_SESSIONS = 200         # sessions listed in one report
EVENT_KINDS = ("dashboard-closed",)
# A last-used time written at most this often (every request would be a write).
TOUCH_S = 60
_JOB = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
  id TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created INTEGER NOT NULL,
  last_used INTEGER,
  revoked INTEGER
);
CREATE INDEX IF NOT EXISTS machines_uid ON machines(uid);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_uid ON events(uid, seq);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


class Machines:
    """The machine tokens and their reports, in the SQLite file at ``path``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        with self._db() as db:
            db.executescript(_SCHEMA)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------ tokens
    def create(self, uid: str, name: str) -> tuple[dict[str, Any], str]:
        """A new token for one of ``uid``'s machines: (its record, the token). The token is
        returned this once; only its hash is kept."""
        name = " ".join(str(name or "").split())
        if not 0 < len(name) <= 60:
            raise InvalidRequest("Name the machine (1 to 60 characters)")
        db = self._db()
        (count,) = db.execute(
            "SELECT COUNT(*) FROM machines WHERE uid = ? AND revoked IS NULL", (uid,)
        ).fetchone()
        if count >= MAX_MACHINES:
            raise InvalidRequest(
                f"You have {MAX_MACHINES} machine tokens; revoke one you no longer use first"
            )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = {"id": "m" + secrets.token_hex(6), "name": name, "created": _now_ms()}
        db.execute(
            "INSERT INTO machines (id, uid, name, token_hash, created) VALUES (?, ?, ?, ?, ?)",
            (record["id"], uid, name, _hash(token), record["created"]),
        )
        return {**record, "lastUsed": None}, token

    def list(self, uid: str) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT id, name, created, last_used FROM machines "
            "WHERE uid = ? AND revoked IS NULL ORDER BY created DESC",
            (uid,),
        )
        return [{"id": r[0], "name": r[1], "created": r[2], "lastUsed": r[3]} for r in rows]

    def revoke(self, uid: str, machine_id: str) -> None:
        changed = (
            self._db()
            .execute(
                "UPDATE machines SET revoked = ? WHERE id = ? AND uid = ? AND revoked IS NULL",
                (_now_ms(), str(machine_id), uid),
            )
            .rowcount
        )
        if not changed:
            raise NotFound("No such machine token")

    def authenticate(self, token: str) -> tuple[str, str, str] | None:
        """(uid, machine id, machine name) for a live token, else None."""
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        digest = _hash(token)
        row = (
            self._db()
            .execute(
                "SELECT id, uid, name, token_hash, last_used FROM machines "
                "WHERE token_hash = ? AND revoked IS NULL",
                (digest,),
            )
            .fetchone()
        )
        if row is None or not hmac.compare_digest(row[3], digest):
            return None
        now = _now_ms()
        if row[4] is None or now - row[4] > TOUCH_S * 1000:
            with contextlib.suppress(sqlite3.Error):  # a busy database never refuses a machine
                self._db().execute("UPDATE machines SET last_used = ? WHERE id = ?", (now, row[0]))
        return row[1], row[0], row[2]

    # ------------------------------------------------------------------ events
    def record(self, uid: str, machine_id: str, machine_name: str, event: Any) -> dict[str, Any]:
        """Keep a report from one of ``uid``'s machines; returns what was kept."""
        kept = clean_event(event, machine_name)
        db = self._db()
        db.execute(
            "INSERT INTO events (uid, machine_id, at, kind, body) VALUES (?, ?, ?, ?, ?)",
            (uid, machine_id, kept["at"], kept["event"], json.dumps(kept)),
        )
        db.execute(
            "DELETE FROM events WHERE uid = ? AND seq NOT IN "
            "(SELECT seq FROM events WHERE uid = ? ORDER BY seq DESC LIMIT ?)",
            (uid, uid, MAX_EVENTS),
        )
        return kept

    def events(self, uid: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT seq, body FROM events WHERE uid = ? ORDER BY seq DESC LIMIT ?",
            (uid, max(1, min(int(limit), MAX_EVENTS))),
        )
        out = []
        for seq, body in rows:
            with contextlib.suppress(ValueError):
                out.append({"id": seq, **json.loads(body)})
        return out


def clean_event(event: Any, machine_name: str) -> dict[str, Any]:
    """The report as it is kept: known fields only, every string bounded. The machine's name
    on the platform comes from its token, the name it reports is kept beside it."""
    if not isinstance(event, dict):
        raise InvalidRequest("A report is a JSON object")
    kind = event.get("event")
    if kind not in EVENT_KINDS:
        raise InvalidRequest(f"Unknown report {kind!r}")
    sessions = event.get("sessions") or []
    if not isinstance(sessions, list):
        raise InvalidRequest("sessions must be a list")
    jobs = event.get("jobs") or []
    if not isinstance(jobs, list):
        raise InvalidRequest("jobs must be a list")
    kept_sessions = []
    for s in sessions[:MAX_SESSIONS]:
        if not isinstance(s, dict):
            continue
        outcomes = s.get("outcomes") if isinstance(s.get("outcomes"), dict) else {}
        kept_sessions.append(
            {
                "experiment": _text(s.get("experiment"), 120),
                "copy": _text(s.get("copy"), 120),
                "revision": _text(s.get("revision"), 80),
                "task": _text(s.get("task"), 120),
                "mode": _text(s.get("mode"), 40),
                "startedAt": _text(s.get("startedAt"), 40),
                "endedAt": _text(s.get("endedAt"), 40),
                "status": _text(s.get("status"), 40),
                "session": _text(s.get("session"), 40),
                "outcomes": {
                    str(k)[:60]: int(v)
                    for k, v in list(outcomes.items())[:40]
                    if isinstance(v, int) and not isinstance(v, bool)
                },
            }
        )
    return {
        "event": kind,
        "at": _now_ms(),
        "machine": machine_name,
        "reportedName": _text(event.get("machine"), 80),
        "alhazen": _text(event.get("alhazen"), 40),
        "startedAt": _text(event.get("startedAt"), 40),
        "closedAt": _text(event.get("closedAt"), 40),
        "reason": _text(event.get("reason"), 80),
        "jobs": [j for j in jobs[:50] if isinstance(j, str) and _JOB.match(j)],
        "sessions": kept_sessions,
    }
