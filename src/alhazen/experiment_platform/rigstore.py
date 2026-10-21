"""The standalone hosts' machine rig configs: one machine record per person and machine, in
SQLite (``rigs.sqlite3`` beside the machine tokens), changed only through ``rigsync``'s rules.

Every change reads the record and writes it back inside one ``BEGIN IMMEDIATE`` transaction,
so a machine's sync and a person's edit arriving together are applied one after the other,
each against the other's result (rigsync's compare-and-set does the rest).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from alhazen.experiment_platform import rigsync
from alhazen.experiment_platform.errors import Conflict, InvalidRequest, NotFound

FILE = "rigs.sqlite3"
MAX_MACHINES = 50  # machine records per person

_SCHEMA = """
CREATE TABLE IF NOT EXISTS machine_rigs (
  uid TEXT NOT NULL,
  mid TEXT NOT NULL,
  record TEXT NOT NULL,
  updated INTEGER NOT NULL,
  PRIMARY KEY (uid, mid)
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def platform_error(error: rigsync.RigSyncError) -> Exception:
    """rigsync's refusal as the platform's error class (and HTTP status)."""
    kind = {"conflict": Conflict, "missing": NotFound}.get(error.kind, InvalidRequest)
    return kind(str(error))


class RigStore:
    """The machine records in the SQLite file at ``path``."""

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

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def _read(self, db: sqlite3.Connection, uid: str, mid: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT record FROM machine_rigs WHERE uid = ? AND mid = ?", (uid, mid)
        ).fetchone()
        return rigsync.normalize(json.loads(row[0]), mid) if row else None

    def _change(
        self,
        uid: str,
        mid: str,
        change: Callable[[dict[str, Any] | None, int], tuple[dict[str, Any], Any]],
        *,
        create: bool = False,
    ) -> tuple[dict[str, Any], Any]:
        try:
            rigsync.check_machine_id(mid)
            with self._transaction() as db:
                current = self._read(db, uid, mid)
                if current is None and create:
                    (count,) = db.execute(
                        "SELECT COUNT(*) FROM machine_rigs WHERE uid = ?", (uid,)
                    ).fetchone()
                    if count >= MAX_MACHINES:
                        raise InvalidRequest(
                            f"You have {MAX_MACHINES} machines with rig configs; forget one first"
                        )
                now = _now_ms()
                record, extra = change(current, now)
                db.execute(
                    "INSERT INTO machine_rigs (uid, mid, record, updated) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (uid, mid) DO UPDATE SET record = excluded.record, "
                    "updated = excluded.updated",
                    (uid, mid, rigsync.to_json(record), now),
                )
                return record, extra
        except rigsync.RigSyncError as error:
            raise platform_error(error) from None

    # ------------------------------------------------------------------ reading
    def list(self, uid: str) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT mid, record FROM machine_rigs WHERE uid = ? ORDER BY updated DESC", (uid,)
        )
        return [rigsync.summary(rigsync.normalize(json.loads(r[1]), r[0])) for r in rows]

    def get(self, uid: str, mid: str) -> dict[str, Any] | None:
        try:
            rigsync.check_machine_id(mid)
        except rigsync.RigSyncError as error:
            raise platform_error(error) from None
        return self._read(self._db(), uid, mid)

    def require(self, uid: str, mid: str) -> dict[str, Any]:
        record = self.get(uid, mid)
        if record is None:
            raise NotFound("No such machine (its dashboard has not synced with this platform)")
        return record

    # ------------------------------------------------------------------ the machine
    def push(
        self, uid: str, mid: str, push: Any, token: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._change(
            uid,
            mid,
            lambda rec, now: rigsync.apply_push(rec, mid, push, now, token=token),
            create=True,
        )

    # ------------------------------------------------------------------ the person
    def put(self, uid: str, mid: str, cid: str, body: dict[str, Any]) -> dict[str, Any]:
        record, _ = self._change(
            uid,
            mid,
            lambda rec, now: (
                rigsync.owner_put(
                    rec,
                    mid,
                    cid,
                    name=body.get("name"),
                    text=body.get("text"),
                    base=body.get("base"),
                    based_on=body.get("basedOn"),
                    now=now,
                ),
                None,
            ),
        )
        return record

    def delete(self, uid: str, mid: str, cid: str, base: Any) -> dict[str, Any]:
        record, _ = self._change(
            uid,
            mid,
            lambda rec, now: (rigsync.owner_delete(rec, mid, cid, base=base, now=now), None),
        )
        return record

    def set_default(self, uid: str, mid: str, cid: Any, base: Any) -> dict[str, Any]:
        record, _ = self._change(
            uid,
            mid,
            lambda rec, now: (rigsync.owner_default(rec, mid, str(cid), base=base, now=now), None),
        )
        return record

    def resolve(self, uid: str, mid: str, cid: str, keep: Any) -> dict[str, Any]:
        record, _ = self._change(
            uid,
            mid,
            lambda rec, now: (rigsync.owner_resolve(rec, mid, cid, keep=keep, now=now), None),
        )
        return record

    def forget(self, uid: str, mid: str) -> None:
        try:
            rigsync.check_machine_id(mid)
        except rigsync.RigSyncError as error:
            raise platform_error(error) from None
        changed = (
            self._db()
            .execute("DELETE FROM machine_rigs WHERE uid = ? AND mid = ?", (uid, mid))
            .rowcount
        )
        if not changed:
            raise NotFound("No such machine")
