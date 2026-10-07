"""The experiment workspace's people registry: subjects and experimenters.

What this module owns, and what it does not
-------------------------------------------
The dashboard's General page manages who takes part in an experiment and who
runs it. Those *management records* live here, in one SQLite database per
workspace, ``<workspace>/people/people.sqlite3`` — the system of record for
them. Everything else about a person is either a scientific record written by
sessions, or derived from this database:

==========================  =========================================  ===========
data                        where                                       role
==========================  =========================================  ===========
subject / experimenter       ``people/people.sqlite3``                  canonical
records, assignments                                                    (this file)
CSV copies                   ``people/csv/experimenters.csv``,          derived,
                             ``people/csv/<experiment id>/*.csv``       rebuildable
backups                      ``people/backups/*.sqlite3``               copies
participants.tsv             ``<data_root>/participants.tsv``           scientific
                                                                        record
experiment.sqlite3           ``<data_root>/experiment.sqlite3``         rebuildable
                                                                        mirror
launch snapshot              ``<workspace>/runs/<id>/launch.json``      immutable
==========================  =========================================  ===========

participants.tsv is never written here. A session registers its subject
there when it starts (`alhazen.data.participants.ensure_participant`), in the
order subjects first ran — the order some experiments counterbalance by — so
registering a subject on the General page must not add a row: it would move
that subject's counterbalancing slot to the day of registration. The
registry *imports* participants.tsv (`plan_participants_import`), keeping
ids, initials, every other column and the row order, and links each record
to the rows it came from.

experiment.sqlite3 is a mirror the session rebuilds from run folders and
moves aside on a schema change (`session.database`). The only copy of what a
person typed into the General page must not live in a file with that
lifecycle, so this database is separate and is never moved aside: a schema
it does not know is refused, not replaced.

Identity
--------
Every record has a stable id (``s_…`` for a subject, ``e_…`` for an
experimenter) that never changes and is never reused. A subject belongs to
one experiment: its id (``code``) is unique within that experiment only, so
``01`` in two experiments is two records, and two people with the same
initials are two records. Records are archived, never deleted, so nothing a
launch snapshot names can disappear. A subject's code is fixed once a launch
has used it (the caller says which records are used): renaming it would file
later sessions under another id than earlier ones.

Writes and the CSV copies
-------------------------
Every write is one ``BEGIN IMMEDIATE`` transaction that also bumps the
database's ``revision``; a record update names the record ``revision`` it was
made against, and a stale one is refused (`Conflict`) rather than silently
overwriting another tab's edit. After the commit the CSV copies are
rewritten whole (each file replaced atomically) from one read snapshot, and
``exported_revision`` records which revision they show. A failed export is
not a failed write: the record is saved, the status says the copies are
behind and why (`export_status`), and `export_csv` retries — the server also
retries at start.

CSV cells are the database's text. Two encodings keep that lossless:

- a cell beginning with ``= + - @``, a tab, a carriage return, or a quote
  ``'`` is written with one extra leading ``'``, so a spreadsheet does not
  run it as a formula; reading strips exactly one leading ``'``;
- a value that is *missing* (NULL), not empty, is named in the row's
  ``missing_fields`` column (a JSON list), since a CSV cell cannot tell the
  two apart.

An edited CSV comes back only through `plan_csv_import` / `apply_csv_import`:
a preview, then an apply bound to the exact file and database revision the
preview saw. A row whose ``revision`` is older than the record's is a
conflict, a row absent from the file is left alone (never deleted), and a row
with no ``record_id`` is a new record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alhazen.config.models import normalize_initials
from alhazen.data.atomic import replace_atomically

SCHEMA_VERSION = 1
DATABASE_NAME = "people.sqlite3"
# How long a writer waits for another connection's transaction (another
# request thread of the same server) before giving up with an error.
BUSY_TIMEOUT_MS = 5000

MAX_NAME = 120
MAX_CODE = 32
MAX_NOTES = 4000
MAX_EXTRA_COLUMNS = 64
MAX_COLUMN_NAME = 64
MAX_EXTRA_VALUE = 1000
STATUSES = ("active", "archived")
# participants.tsv's own columns (alhazen.data.participants): the id and the
# initials. Every other column of a row is carried as the record's extra
# columns, in order.
TSV_ID = "participant_id"
TSV_INITIALS = "initials"
# Column names an extra column may not take: they are the record's own.
RESERVED_COLUMNS = frozenset(
    {
        "record_id",
        "experiment_id",
        "subject_id",
        "initials",
        "status",
        "notes",
        "revision",
        "position",
        "created",
        "updated",
        "missing_fields",
        TSV_ID,
    }
)
# What spreadsheet programs treat as the start of a formula, plus the quote
# this module uses to defuse one (so a value that itself begins with a quote
# survives the round trip).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "'")

SUBJECT_COLUMNS = (
    "record_id",
    "experiment_id",
    "subject_id",
    "initials",
    "status",
    "notes",
    "revision",
    "position",
    "created",
    "updated",
)
EXPERIMENTER_COLUMNS = (
    "record_id",
    "name",
    "initials",
    "status",
    "notes",
    "revision",
    "created",
    "updated",
)
ASSIGNMENT_COLUMNS = (
    "experimenter_id",
    "name",
    "initials",
    "experimenter_status",
    "assignment_status",
    "assigned",
)

_SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE experimenters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  initials TEXT,
  notes TEXT,
  status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
  revision INTEGER NOT NULL,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE TABLE subjects (
  id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  code TEXT NOT NULL,
  initials TEXT,
  notes TEXT,
  extra_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
  position INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  created TEXT NOT NULL,
  updated TEXT NOT NULL,
  UNIQUE (experiment_id, code)
);
CREATE TABLE subject_sources (
  subject_id TEXT NOT NULL REFERENCES subjects(id),
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  line INTEGER NOT NULL,
  imported TEXT NOT NULL,
  PRIMARY KEY (subject_id, source)
);
CREATE TABLE assignments (
  experiment_id TEXT NOT NULL,
  experimenter_id TEXT NOT NULL REFERENCES experimenters(id),
  status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
  assigned TEXT NOT NULL,
  PRIMARY KEY (experiment_id, experimenter_id)
);
CREATE TABLE changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  revision INTEGER NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  action TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
"""
_TABLES = ("assignments", "changes", "experimenters", "meta", "subject_sources", "subjects")


class PeopleError(ValueError):
    """A request the registry refuses, in words the person can act on."""


class Conflict(PeopleError):
    """A write made against a record revision that is no longer current."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# -- validation: every value is checked here, once, at the boundary ---------


def _single_line(value: Any, what: str, limit: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise PeopleError(f"{what} is required")
        return None
    if not isinstance(value, str):
        raise PeopleError(f"{what} must be text")
    text = value.strip()
    if not text:
        if required:
            raise PeopleError(f"{what} is required")
        return None
    if len(text) > limit:
        raise PeopleError(f"{what} must be at most {limit} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise PeopleError(f"{what} must be one line of text, without control characters")
    return text


def _notes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PeopleError("Notes must be text")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > MAX_NOTES:
        raise PeopleError(f"Notes must be at most {MAX_NOTES} characters")
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in text):
        raise PeopleError("Notes may not contain control characters")
    return text


def _initials(value: Any, *, required: bool = False) -> str | None:
    text = _single_line(value, "Initials", 16, required=required)
    if text is None:
        return None
    try:
        return normalize_initials(text)
    except ValueError as exc:
        raise PeopleError(str(exc)) from exc


def check_subject_code(value: Any) -> str:
    """A subject id as sessions accept it (``SessionInfo``: alphanumeric,
    since it becomes the ``sub-<id>`` folder), kept as text so ``007`` stays
    ``007``. A leading ``sub-`` is the folder's, not the id's, and refused
    so nobody files ``sub-sub-01``."""
    text = _single_line(value, "Subject ID", MAX_CODE, required=True)
    assert text is not None
    if text.lower().startswith("sub-"):
        raise PeopleError(f"Give the subject ID without 'sub-' (got {text!r})")
    if not text.isalnum():
        raise PeopleError(
            f"Subject ID must be letters and digits only (it becomes a folder name); got {text!r}"
        )
    return text


def _extra(value: Any) -> list[list[Any]]:
    """Extra columns as an ordered list of ``[name, value]``, value text or
    None (missing). Accepts that list, or a mapping (insertion order)."""
    if value is None:
        return []
    pairs: Iterable[Any]
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, list):
        pairs = value
    else:
        raise PeopleError("Extra columns must be a list of [name, value] pairs")
    out: list[list[Any]] = []
    seen: set[str] = set()
    for pair in pairs:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise PeopleError("Extra columns must be a list of [name, value] pairs")
        name = _single_line(pair[0], "A column name", MAX_COLUMN_NAME, required=True)
        assert name is not None
        if name in RESERVED_COLUMNS:
            raise PeopleError(f"{name!r} is one of the record's own fields, not an extra column")
        if name in seen:
            raise PeopleError(f"The column {name!r} is given twice")
        seen.add(name)
        cell = pair[1]
        if cell is not None:
            if not isinstance(cell, str):
                raise PeopleError(f"The value of {name!r} must be text")
            if len(cell) > MAX_EXTRA_VALUE:
                raise PeopleError(f"The value of {name!r} is longer than {MAX_EXTRA_VALUE}")
            if any(ord(ch) < 32 and ch not in "\n\t" for ch in cell):
                raise PeopleError(f"The value of {name!r} may not contain control characters")
        out.append([name, cell])
    if len(out) > MAX_EXTRA_COLUMNS:
        raise PeopleError(f"At most {MAX_EXTRA_COLUMNS} extra columns")
    return out


def _status(value: Any) -> str:
    if value not in STATUSES:
        raise PeopleError(f"Status must be one of {', '.join(STATUSES)}")
    return str(value)


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PeopleError("The record's revision is required (reload the page)")
    return value


# -- CSV cells ---------------------------------------------------------------


def csv_cell(value: str | None) -> str:
    """A database value as its CSV cell: missing is empty (and named in
    ``missing_fields``), and a value a spreadsheet would run as a formula
    gets one leading quote."""
    if value is None:
        return ""
    return "'" + value if value.startswith(FORMULA_START) else value


def from_csv_cell(cell: str) -> str:
    """The inverse of `csv_cell` for a present value."""
    return cell[1:] if cell.startswith("'") else cell


@dataclass(frozen=True)
class ExportStatus:
    revision: int
    exported_revision: int
    error: str | None
    attempted: str | None
    directory: str

    def as_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "exported_revision": self.exported_revision,
            "pending": self.exported_revision < self.revision,
            "error": self.error,
            "attempted": self.attempted,
            "directory": self.directory,
        }


class PeopleRegistry:
    """The registry over ``<workspace>/people``. Thread-safe: each call opens
    its own connection; SQLite serialises the writers."""

    def __init__(self, workspace_dir: Path):
        self.directory = Path(workspace_dir) / "people"
        self.path = self.directory / DATABASE_NAME
        self.csv_dir = self.directory / "csv"
        self.backup_dir = self.directory / "backups"
        self._export_lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._open()

    # -- the database ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return db

    def _open(self) -> None:
        """Create the schema in a new file; refuse a file this version did
        not write. Never moves or replaces an existing database."""
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version == 0 and not tables:
                db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO meta(key, value) VALUES ('revision', '0')")
                db.execute("INSERT INTO meta(key, value) VALUES ('exported_revision', '0')")
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.execute("COMMIT")
                return
            if version > SCHEMA_VERSION:
                raise PeopleError(
                    f"{self.path} was written by a newer alhazen (people schema {version}; this "
                    f"one reads {SCHEMA_VERSION}). Nothing was changed; use that alhazen, or "
                    "another --state-dir."
                )
            if version != SCHEMA_VERSION or set(_TABLES) - tables:
                raise PeopleError(
                    f"{self.path} is not a people registry this alhazen can read (schema "
                    f"{version}, tables {', '.join(sorted(tables)) or 'none'}). Nothing was "
                    "changed; move the file aside to start a new registry."
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[tuple[sqlite3.Connection, int]]:
        """One write transaction, with the database revision it creates."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                revision = int(_meta(db, "revision")) + 1
                db.execute("UPDATE meta SET value = ? WHERE key = 'revision'", (str(revision),))
                yield db, revision
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def _changed(self) -> None:
        """After a committed write: bring the CSV copies up to date. A
        failure is recorded (`export_status`), never raised: the write itself
        has already succeeded and must not be reported as failed."""
        self.export_csv(raise_errors=False)

    @staticmethod
    def _log(
        db: sqlite3.Connection, revision: int, entity: str, entity_id: str, action: str, detail: Any
    ) -> None:
        db.execute(
            "INSERT INTO changes(at, revision, entity, entity_id, action, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), revision, entity, entity_id, action, json.dumps(detail, ensure_ascii=False)),
        )

    # -- reads ------------------------------------------------------------------

    def experimenters(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM experimenters ORDER BY name COLLATE NOCASE, id")
            return [_experimenter(row) for row in rows]

    def experimenter(self, experimenter_id: str) -> dict[str, Any]:
        with self._connection() as db:
            return _experimenter(_one(db, "experimenters", experimenter_id, "experimenter"))

    def assignments(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT a.status AS assignment_status, a.assigned, e.* FROM assignments a "
                "JOIN experimenters e ON e.id = a.experimenter_id WHERE a.experiment_id = ? "
                "ORDER BY e.name COLLATE NOCASE, e.id",
                (experiment_id,),
            )
            return [
                {
                    **_experimenter(row),
                    "assignment_status": row["assignment_status"],
                    "assigned": row["assigned"],
                }
                for row in rows
            ]

    def subjects(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM subjects WHERE experiment_id = ? ORDER BY position, id",
                (experiment_id,),
            ).fetchall()
            sources = _sources(db, [row["id"] for row in rows])
            return [_subject(row, sources.get(row["id"], [])) for row in rows]

    def subject(self, subject_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = _one(db, "subjects", subject_id, "subject")
            return _subject(row, _sources(db, [subject_id]).get(subject_id, []))

    def experiment_ids(self) -> list[str]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT experiment_id FROM subjects UNION SELECT experiment_id FROM assignments"
            )
            return sorted(row[0] for row in rows)

    # -- experimenters -------------------------------------------------------------

    def add_experimenter(self, fields: Any) -> dict[str, Any]:
        _only(fields, {"name", "initials", "notes"})
        name = _single_line(fields.get("name"), "Name", MAX_NAME, required=True)
        initials = _initials(fields.get("initials"))
        notes = _notes(fields.get("notes"))
        record_id = _new_id("e")
        stamp = _now()
        with self._write() as (db, revision):
            db.execute(
                "INSERT INTO experimenters(id, name, initials, notes, status, revision, created, "
                "updated) VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
                (record_id, name, initials, notes, stamp, stamp),
            )
            self._log(db, revision, "experimenter", record_id, "add", {"name": name})
        self._changed()
        return self.experimenter(record_id)

    def update_experimenter(
        self, experimenter_id: str, revision: Any, fields: Any
    ) -> dict[str, Any]:
        _only(fields, {"name", "initials", "notes"})
        expected = _revision(revision)
        changes: dict[str, Any] = {}
        if "name" in fields:
            changes["name"] = _single_line(fields["name"], "Name", MAX_NAME, required=True)
        if "initials" in fields:
            changes["initials"] = _initials(fields["initials"])
        if "notes" in fields:
            changes["notes"] = _notes(fields["notes"])
        return self._update("experimenters", "experimenter", experimenter_id, expected, changes)

    def set_experimenter_status(
        self, experimenter_id: str, revision: Any, status: Any
    ) -> dict[str, Any]:
        changes = {"status": _status(status)}
        return self._update(
            "experimenters", "experimenter", experimenter_id, _revision(revision), changes
        )

    def assign(self, experiment_id: str, experimenter_id: str) -> list[dict[str, Any]]:
        """Make an experimenter selectable in an experiment (or again, after
        `unassign`). The experimenter must exist and be active."""
        _experiment_key(experiment_id)
        with self._write() as (db, revision):
            row = _one(db, "experimenters", experimenter_id, "experimenter")
            if row["status"] != "active":
                raise PeopleError(f"{row['name']} is archived; restore them first")
            db.execute(
                "INSERT INTO assignments(experiment_id, experimenter_id, status, assigned) "
                "VALUES (?, ?, 'active', ?) ON CONFLICT(experiment_id, experimenter_id) "
                "DO UPDATE SET status = 'active'",
                (experiment_id, experimenter_id, _now()),
            )
            self._log(db, revision, "assignment", experimenter_id, "assign", experiment_id)
        self._changed()
        return self.assignments(experiment_id)

    def unassign(self, experiment_id: str, experimenter_id: str) -> list[dict[str, Any]]:
        """Stop offering an experimenter in one experiment. The assignment is
        archived, not deleted; sessions they ran keep their snapshot."""
        with self._write() as (db, revision):
            cursor = db.execute(
                "UPDATE assignments SET status = 'archived' WHERE experiment_id = ? "
                "AND experimenter_id = ?",
                (experiment_id, experimenter_id),
            )
            if cursor.rowcount == 0:
                raise PeopleError("That experimenter is not assigned to this experiment")
            self._log(db, revision, "assignment", experimenter_id, "unassign", experiment_id)
        self._changed()
        return self.assignments(experiment_id)

    # -- subjects ---------------------------------------------------------------------

    def add_subject(self, experiment_id: str, fields: Any) -> dict[str, Any]:
        _experiment_key(experiment_id)
        _only(fields, {"code", "initials", "notes", "extra"})
        code = check_subject_code(fields.get("code"))
        initials = _initials(fields.get("initials"))
        notes = _notes(fields.get("notes"))
        extra = _extra(fields.get("extra"))
        record_id = _new_id("s")
        stamp = _now()
        with self._write() as (db, revision):
            _refuse_taken_code(db, experiment_id, code, None)
            position = _next_position(db, experiment_id)
            db.execute(
                "INSERT INTO subjects(id, experiment_id, code, initials, notes, extra_json, "
                "status, position, revision, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 1, ?, ?)",
                (
                    record_id,
                    experiment_id,
                    code,
                    initials,
                    notes,
                    _dump(extra),
                    position,
                    stamp,
                    stamp,
                ),
            )
            self._log(db, revision, "subject", record_id, "add", {"code": code})
        self._changed()
        return self.subject(record_id)

    def update_subject(
        self,
        experiment_id: str,
        subject_id: str,
        revision: Any,
        fields: Any,
        *,
        used: bool = False,
    ) -> dict[str, Any]:
        """Edit a subject. ``used``: a launch has named this record, so its
        code is fixed, and so are initials already recorded (a session wrote
        them into participants.tsv; a correction there is a human's)."""
        _only(fields, {"code", "initials", "notes", "extra"})
        expected = _revision(revision)
        current = self.subject(subject_id)
        if current["experiment_id"] != experiment_id:
            raise PeopleError("That subject belongs to another experiment")
        changes: dict[str, Any] = {}
        if "code" in fields:
            code = check_subject_code(fields["code"])
            if code != current["code"]:
                if used:
                    raise PeopleError(
                        f"sub-{current['code']} has sessions; its ID cannot change. Archive it "
                        "and add a new subject instead."
                    )
                changes["code"] = code
        if "initials" in fields:
            initials = _initials(fields["initials"])
            if initials != current["initials"]:
                if used and current["initials"]:
                    raise PeopleError(
                        f"sub-{current['code']} has run as {current['initials']}; those initials "
                        "are part of its sessions' records and cannot change here."
                    )
                changes["initials"] = initials
        if "notes" in fields:
            changes["notes"] = _notes(fields["notes"])
        if "extra" in fields:
            changes["extra_json"] = _dump(_extra(fields["extra"]))
        if "code" in changes:
            with self._connection() as db:
                _refuse_taken_code(db, experiment_id, changes["code"], subject_id)
        return self._update("subjects", "subject", subject_id, expected, changes)

    def set_subject_status(
        self, experiment_id: str, subject_id: str, revision: Any, status: Any
    ) -> dict[str, Any]:
        current = self.subject(subject_id)
        if current["experiment_id"] != experiment_id:
            raise PeopleError("That subject belongs to another experiment")
        return self._update(
            "subjects", "subject", subject_id, _revision(revision), {"status": _status(status)}
        )

    def _update(
        self, table: str, entity: str, record_id: str, expected: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        if table not in {"subjects", "experimenters"}:
            raise AssertionError(table)
        columns = set(changes)
        allowed = {"name", "initials", "notes", "status", "code", "extra_json"}
        if columns - allowed:
            raise AssertionError(columns - allowed)
        with self._write() as (db, revision):
            row = _one(db, table, record_id, entity)
            if row["revision"] != expected:
                raise Conflict(
                    f"This {entity} was changed elsewhere (revision {row['revision']}, your copy "
                    f"{expected}). Nothing was saved; reload to see the current record."
                )
            really = {k: v for k, v in changes.items() if row[k] != v}
            if really:
                assignments = ", ".join(f"{name} = ?" for name in sorted(really))
                db.execute(
                    f"UPDATE {table} SET {assignments}, revision = revision + 1, updated = ? "
                    "WHERE id = ?",
                    [really[name] for name in sorted(really)] + [_now(), record_id],
                )
                before = {k: row[k] for k in really}
                self._log(
                    db, revision, entity, record_id, "update", {"before": before, "after": really}
                )
        if really:
            self._changed()
        return self.subject(record_id) if table == "subjects" else self.experimenter(record_id)

    # -- launches ------------------------------------------------------------------------

    def launch_identity(
        self,
        experiment_id: str,
        subject_id: str | None,
        experimenter_id: str | None,
        *,
        need_initials: bool,
    ) -> dict[str, Any]:
        """The immutable identity a launch records: copies of the records as
        they are now, checked to be usable for this experiment. Later edits
        change the records, never these copies."""
        snapshot: dict[str, Any] = {"subject": None, "experimenter": None, "taken": _now()}
        with self._connection() as db:
            if subject_id is not None:
                row = _one(db, "subjects", subject_id, "subject")
                if row["experiment_id"] != experiment_id:
                    raise PeopleError("That subject is registered for another experiment")
                if row["status"] != "active":
                    raise PeopleError(f"sub-{row['code']} is archived; restore it to run it")
                if need_initials and not row["initials"]:
                    raise PeopleError(
                        f"sub-{row['code']} has no initials recorded; add them on the General "
                        "page before a run or test session"
                    )
                snapshot["subject"] = {
                    "record_id": row["id"],
                    "id": row["code"],
                    "initials": row["initials"],
                    "revision": row["revision"],
                }
            if experimenter_id is not None:
                row = _one(db, "experimenters", experimenter_id, "experimenter")
                if row["status"] != "active":
                    raise PeopleError(f"{row['name']} is archived; restore them first")
                assigned = db.execute(
                    "SELECT status FROM assignments WHERE experiment_id = ? AND "
                    "experimenter_id = ?",
                    (experiment_id, experimenter_id),
                ).fetchone()
                if assigned is None or assigned["status"] != "active":
                    raise PeopleError(
                        f"{row['name']} is not an experimenter of this experiment; add them on "
                        "the General page"
                    )
                snapshot["experimenter"] = {
                    "record_id": row["id"],
                    "name": row["name"],
                    "initials": row["initials"],
                    "revision": row["revision"],
                }
        return snapshot

    # -- CSV copies --------------------------------------------------------------------------

    def export_status(self) -> ExportStatus:
        with self._connection() as db:
            return ExportStatus(
                revision=int(_meta(db, "revision")),
                exported_revision=int(_meta(db, "exported_revision")),
                error=_meta(db, "export_error", None),
                attempted=_meta(db, "export_attempted", None),
                directory=str(self.csv_dir),
            )

    def export_csv(self, *, raise_errors: bool = True) -> ExportStatus:
        """Rewrite every CSV copy from one consistent read of the database,
        and record which revision they now show — or why they could not be
        written. Serialised, so an older snapshot never lands on a newer one."""
        with self._export_lock:
            try:
                with self._connection() as db:
                    db.execute("BEGIN")
                    revision = int(_meta(db, "revision"))
                    files = _csv_files(db, self.csv_dir)
                    db.execute("COMMIT")
                for path, text in files.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # A byte-order mark, so a spreadsheet reads the file as UTF-8.
                    replace_atomically(path, "\ufeff" + text, newline="")
            except (OSError, sqlite3.Error) as exc:
                message = f"{type(exc).__name__}: {exc}"
                with self._connection() as db:
                    _set_meta(db, {"export_error": message, "export_attempted": _now()})
                if raise_errors:
                    raise PeopleError(
                        f"The records are saved, but their CSV copies could not be written: "
                        f"{message}"
                    ) from exc
                return self.export_status()
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                done = int(_meta(db, "exported_revision"))
                if revision > done:
                    db.execute(
                        "UPDATE meta SET value = ? WHERE key = 'exported_revision'",
                        (str(revision),),
                    )
                db.execute("DELETE FROM meta WHERE key = 'export_error'")
                db.execute(
                    "INSERT INTO meta(key, value) VALUES ('export_attempted', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_now(),),
                )
                db.execute("COMMIT")
        return self.export_status()

    # -- backups --------------------------------------------------------------------------------

    def backup(self, reason: str) -> Path:
        """A consistent copy of the database (SQLite's online backup), named
        by the time and the reason. Returned so the caller can report it."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") or "backup"
        target = self.backup_dir / f"people-{stamp}-{slug}.sqlite3"
        with self._connection() as source:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
        return target

    # -- participants.tsv import ----------------------------------------------------------------

    def plan_participants_import(
        self, experiment_id: str, sources: list[tuple[Path, str]]
    ) -> dict[str, Any]:
        """What importing these participants.tsv files would do, without
        doing it. ``sources``: (data folder, "real" or "rehearsal"), in the
        order they are read. The plan's ``digest`` binds an apply to the
        files and the database exactly as previewed."""
        _experiment_key(experiment_id)
        existing = {s["code"]: s for s in self.subjects(experiment_id)}
        planned: dict[str, dict[str, Any]] = {}
        rows: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        digest.update(str(self.export_status().revision).encode())
        for root, kind in sources:
            path = Path(root) / "participants.tsv"
            entry: dict[str, Any] = {"path": str(path), "kind": kind, "rows": 0, "error": None}
            files.append(entry)
            if not path.is_file():
                entry["error"] = "no participants.tsv"
                continue
            try:
                data = path.read_bytes()
                digest.update(str(path).encode() + b"\0" + data)
                header, table = _read_tsv(data, path)
            except PeopleError as exc:
                entry["error"] = str(exc)
                continue
            entry["rows"] = len(table)
            entry["columns"] = header
            for line, cells in table:
                rows.append(_plan_row(path, kind, line, cells, existing, planned))
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        return {
            "experiment_id": experiment_id,
            "files": files,
            "rows": rows,
            "counts": counts,
            "changes": sum(1 for row in rows if row["action"] in {"new", "link", "fill"}),
            "digest": digest.hexdigest(),
        }

    def apply_participants_import(
        self, experiment_id: str, sources: list[tuple[Path, str]], digest: str
    ) -> dict[str, Any]:
        """Apply a previewed import. Refused if the files or the database
        changed since the preview. A backup is taken first when anything will
        change; applying the same files again changes nothing."""
        plan = self.plan_participants_import(experiment_id, sources)
        if plan["digest"] != digest:
            raise Conflict(
                "The participants files or the registry changed since the preview; nothing was "
                "imported. Preview again."
            )
        if not plan["changes"]:
            return {**plan, "applied": 0, "backup": None}
        backup = self.backup("before-participants-import")
        stamp = _now()
        applied = 0
        with self._write() as (db, revision):
            for row in plan["rows"]:
                action = row["action"]
                if action == "new":
                    record_id = _new_id("s")
                    _refuse_taken_code(db, experiment_id, row["code"], None)
                    db.execute(
                        "INSERT INTO subjects(id, experiment_id, code, initials, notes, "
                        "extra_json, status, position, revision, created, updated) "
                        "VALUES (?, ?, ?, ?, NULL, ?, 'active', ?, 1, ?, ?)",
                        (
                            record_id,
                            experiment_id,
                            row["code"],
                            row["initials"],
                            _dump(row["extra"]),
                            _next_position(db, experiment_id),
                            stamp,
                            stamp,
                        ),
                    )
                    row["record_id"] = record_id
                elif action == "fill":
                    db.execute(
                        "UPDATE subjects SET initials = ?, revision = revision + 1, updated = ? "
                        "WHERE id = ? AND initials IS NULL",
                        (row["initials"], stamp, row["record_id"]),
                    )
                if action in {"link", "fill"} and row["record_id"] is None:
                    found = db.execute(
                        "SELECT id FROM subjects WHERE experiment_id = ? AND code = ?",
                        (experiment_id, row["code"]),
                    ).fetchone()
                    row["record_id"] = found["id"]
                if action in {"new", "link", "fill"}:
                    db.execute(
                        "INSERT OR IGNORE INTO subject_sources(subject_id, source, kind, line, "
                        "imported) VALUES (?, ?, ?, ?, ?)",
                        (row["record_id"], row["source"], row["kind"], row["line"], stamp),
                    )
                    self._log(
                        db,
                        revision,
                        "subject",
                        row["record_id"],
                        f"import-{action}",
                        {"source": row["source"], "line": row["line"]},
                    )
                    applied += 1
        self._changed()
        return {**plan, "applied": applied, "backup": str(backup)}

    # -- CSV round trip --------------------------------------------------------

    def csv_path(self, kind: str, experiment_id: str | None) -> Path:
        if kind == "experimenters":
            return self.csv_dir / "experimenters.csv"
        if kind == "subjects" and experiment_id:
            return self.csv_dir / _experiment_key(experiment_id) / "subjects.csv"
        raise PeopleError("Choose subjects (of an experiment) or experimenters")

    def plan_csv_import(self, kind: str, experiment_id: str | None) -> dict[str, Any]:
        """What reading back an edited CSV copy would change. Only the
        registry's own copy is read (`csv_path`), never a path a request
        names."""
        path = self.csv_path(kind, experiment_id)
        if not path.is_file():
            raise PeopleError(f"{path} does not exist; nothing to import")
        data = path.read_bytes()
        status = self.export_status()
        digest = hashlib.sha256(str(status.revision).encode() + b"\0" + data).hexdigest()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PeopleError(f"{path} is not UTF-8 text: {exc}") from exc
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
        if not header:
            raise PeopleError(f"{path} is empty")
        own = SUBJECT_COLUMNS if kind == "subjects" else EXPERIMENTER_COLUMNS
        missing = [c for c in ("record_id", "revision", "missing_fields") if c not in header]
        if missing:
            raise PeopleError(f"{path} has no {', '.join(missing)} column; it is not a copy")
        current = (
            {s["id"]: s for s in self.subjects(experiment_id or "")}
            if kind == "subjects"
            else {e["id"]: e for e in self.experimenters()}
        )
        changes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for cells in reader:
            line = reader.line_num
            if not any(cells):
                continue
            if len(cells) != len(header):
                changes.append(
                    {
                        "line": line,
                        "action": "error",
                        "reason": f"{len(cells)} cells, the header has {len(header)}",
                    }
                )
                continue
            row = dict(zip(header, cells, strict=True))
            try:
                gaps = json.loads(row["missing_fields"] or "[]")
                if not isinstance(gaps, list):
                    raise ValueError("not a list")
            except ValueError:
                changes.append(
                    {"line": line, "action": "error", "reason": "missing_fields is not a JSON list"}
                )
                continue
            values = {k: (None if k in gaps else from_csv_cell(v)) for k, v in row.items()}
            changes.append(_csv_change(kind, line, values, header, own, current, seen))
        counts: dict[str, int] = {}
        for change in changes:
            counts[change["action"]] = counts.get(change["action"], 0) + 1
        untouched = sorted(set(current) - seen)
        return {
            "kind": kind,
            "path": str(path),
            "changes": changes,
            "counts": counts,
            "absent": untouched,
            "digest": digest,
        }

    def apply_csv_import(
        self, kind: str, experiment_id: str | None, digest: str, used: set[str]
    ) -> dict[str, Any]:
        plan = self.plan_csv_import(kind, experiment_id)
        if plan["digest"] != digest:
            raise Conflict("The CSV file or the registry changed since the preview; preview again")
        blocking = [c for c in plan["changes"] if c["action"] in {"error", "conflict"}]
        if blocking:
            raise PeopleError(
                f"{len(blocking)} row(s) cannot be imported (see the preview); nothing was changed"
            )
        todo = [c for c in plan["changes"] if c["action"] in {"add", "update"}]
        if not todo:
            return {**plan, "applied": 0, "backup": None}
        backup = self.backup(f"before-{kind}-csv-import")
        applied = 0
        for change in todo:
            if kind == "subjects":
                assert experiment_id is not None
                if change["action"] == "add":
                    self.add_subject(experiment_id, change["fields"])
                elif change["fields"]:
                    self.update_subject(
                        experiment_id,
                        change["record_id"],
                        change["revision"],
                        change["fields"],
                        used=change["record_id"] in used,
                    )
            elif change["action"] == "add":
                self.add_experimenter(change["fields"])
            elif change["fields"]:
                self.update_experimenter(change["record_id"], change["revision"], change["fields"])
            if change["action"] == "update" and change.get("status"):
                if kind == "subjects":
                    assert experiment_id is not None
                    latest = self.subject(change["record_id"])
                    self.set_subject_status(
                        experiment_id, change["record_id"], latest["revision"], change["status"]
                    )
                else:
                    latest = self.experimenter(change["record_id"])
                    self.set_experimenter_status(
                        change["record_id"], latest["revision"], change["status"]
                    )
            applied += 1
        return {**plan, "applied": applied, "backup": str(backup)}


# -- helpers over rows ---------------------------------------------------------


def _meta(db: sqlite3.Connection, key: str, default: Any = ...) -> Any:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        if default is ...:
            raise PeopleError(f"The people registry has no {key!r}; it is damaged")
        return default
    return row[0]


def _set_meta(db: sqlite3.Connection, values: dict[str, str]) -> None:
    db.execute("BEGIN IMMEDIATE")
    for key, value in values.items():
        db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
            "excluded.value",
            (key, value),
        )
    db.execute("COMMIT")


def _only(fields: Any, allowed: set[str]) -> None:
    if not isinstance(fields, dict):
        raise PeopleError("Expected the record's fields as an object")
    unknown = set(fields) - allowed
    if unknown:
        raise PeopleError(f"Unknown field(s): {', '.join(sorted(map(str, unknown)))}")


def _experiment_key(experiment_id: Any) -> str:
    if not isinstance(experiment_id, str) or not re.fullmatch(r"[0-9a-f]{16}", experiment_id):
        raise PeopleError("Unknown experiment")
    return experiment_id


def _one(db: sqlite3.Connection, table: str, record_id: Any, entity: str) -> sqlite3.Row:
    if table not in {"subjects", "experimenters"}:
        raise AssertionError(table)
    if not isinstance(record_id, str):
        raise PeopleError(f"Unknown {entity}")
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise PeopleError(f"Unknown {entity} {record_id!r}")
    return row


def _refuse_taken_code(
    db: sqlite3.Connection, experiment_id: str, code: str, other_than: str | None
) -> None:
    row = db.execute(
        "SELECT id, status FROM subjects WHERE experiment_id = ? AND code = ?",
        (experiment_id, code),
    ).fetchone()
    if row is not None and row["id"] != other_than:
        state = " (archived)" if row["status"] == "archived" else ""
        raise PeopleError(f"sub-{code} is already registered in this experiment{state}")


def _next_position(db: sqlite3.Connection, experiment_id: str) -> int:
    row = db.execute(
        "SELECT COALESCE(MAX(position), 0) FROM subjects WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    return int(row[0]) + 1


def _dump(extra: list[list[Any]]) -> str:
    return json.dumps(extra, ensure_ascii=False)


def _sources(db: sqlite3.Connection, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    for row in db.execute(
        f"SELECT * FROM subject_sources WHERE subject_id IN ({marks}) ORDER BY source", ids
    ):
        out.setdefault(row["subject_id"], []).append(
            {
                "source": row["source"],
                "kind": row["kind"],
                "line": row["line"],
                "imported": row["imported"],
            }
        )
    return out


def _experimenter(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "initials": row["initials"],
        "notes": row["notes"],
        "status": row["status"],
        "revision": row["revision"],
        "created": row["created"],
        "updated": row["updated"],
    }


def _subject(row: sqlite3.Row, sources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "code": row["code"],
        "initials": row["initials"],
        "notes": row["notes"],
        "extra": json.loads(row["extra_json"]),
        "status": row["status"],
        "position": row["position"],
        "revision": row["revision"],
        "created": row["created"],
        "updated": row["updated"],
        "sources": sources,
    }


def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _row_cells(values: dict[str, Any], header: list[str]) -> list[str]:
    missing = [name for name in header if name != "missing_fields" and values.get(name) is None]
    cells = []
    for name in header:
        if name == "missing_fields":
            cells.append(json.dumps(missing, ensure_ascii=False) if missing else "[]")
        else:
            value = values.get(name)
            cells.append(csv_cell(None if value is None else str(value)))
    return cells


def _csv_files(db: sqlite3.Connection, directory: Path) -> dict[Path, str]:
    """Every CSV copy's path and text, from the open read transaction."""
    files: dict[Path, str] = {}
    experimenters = [
        _experimenter(r)
        for r in db.execute("SELECT * FROM experimenters ORDER BY name COLLATE NOCASE, id")
    ]
    header = [*EXPERIMENTER_COLUMNS, "missing_fields"]
    files[directory / "experimenters.csv"] = _csv_text(
        header, [_row_cells({**e, "record_id": e["id"]}, header) for e in experimenters]
    )
    by_id = {e["id"]: e for e in experimenters}
    experiments = sorted(
        row[0]
        for row in db.execute(
            "SELECT experiment_id FROM subjects UNION SELECT experiment_id FROM assignments"
        )
    )
    for experiment_id in experiments:
        subjects = [
            _subject(r, [])
            for r in db.execute(
                "SELECT * FROM subjects WHERE experiment_id = ? ORDER BY position, id",
                (experiment_id,),
            )
        ]
        extra_names: list[str] = []
        for s in subjects:
            for name, _ in s["extra"]:
                if name not in extra_names:
                    extra_names.append(name)
        header = [*SUBJECT_COLUMNS, *extra_names, "missing_fields"]
        rows = []
        for s in subjects:
            values: dict[str, Any] = {
                **s,
                "record_id": s["id"],
                "subject_id": s["code"],
                "position": s["position"],
            }
            extra = dict(s["extra"])
            for name in extra_names:
                values[name] = extra.get(name)
            rows.append(_row_cells(values, header))
        files[directory / experiment_id / "subjects.csv"] = _csv_text(header, rows)
        header = [*ASSIGNMENT_COLUMNS, "missing_fields"]
        rows = []
        for a in db.execute(
            "SELECT * FROM assignments WHERE experiment_id = ? ORDER BY experimenter_id",
            (experiment_id,),
        ):
            person = by_id.get(a["experimenter_id"], {})
            rows.append(
                _row_cells(
                    {
                        "experimenter_id": a["experimenter_id"],
                        "name": person.get("name"),
                        "initials": person.get("initials"),
                        "experimenter_status": person.get("status"),
                        "assignment_status": a["status"],
                        "assigned": a["assigned"],
                    },
                    header,
                )
            )
        files[directory / experiment_id / "experimenters.csv"] = _csv_text(header, rows)
    return files


def _read_tsv(data: bytes, path: Path) -> tuple[list[str], list[tuple[int, dict[str, str | None]]]]:
    """participants.tsv as its header and rows (``(line, {column: cell})``),
    a cell None where a short row has none — missing, not empty. A row longer
    than the header is refused, as `alhazen.data.participants` refuses it."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PeopleError(f"{path} is not UTF-8 text: {exc}") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    header = next(reader, None)
    if not header or TSV_ID not in header:
        raise PeopleError(f"{path} has no {TSV_ID} column")
    if len(set(header)) != len(header):
        raise PeopleError(f"{path} names a column twice in its header")
    rows: list[tuple[int, dict[str, str | None]]] = []
    for cells in reader:
        if not cells:
            continue
        if len(cells) > len(header):
            raise PeopleError(
                f"{path}, line {reader.line_num}: more cells than the header has columns; fix "
                "that row by hand before importing"
            )
        padded: list[str | None] = [*cells, *([None] * (len(header) - len(cells)))]
        rows.append((reader.line_num, dict(zip(header, padded, strict=True))))
    return header, rows


def _tsv_extra(cells: dict[str, str | None]) -> tuple[list[list[Any]], list[str]]:
    """A participants.tsv row's other columns, in order, as extra columns;
    a column whose name is one of the record's own fields is kept under
    ``<name> (participants.tsv)`` and said so."""
    extra: list[list[Any]] = []
    renamed: list[str] = []
    for name, value in cells.items():
        if name in (TSV_ID, TSV_INITIALS):
            continue
        key = name
        if name in RESERVED_COLUMNS or not name.strip():
            key = f"{name or 'unnamed'} (participants.tsv)"
            renamed.append(name)
        extra.append([key, value])
    return extra, renamed


def _plan_row(
    path: Path,
    kind: str,
    line: int,
    cells: dict[str, str | None],
    existing: dict[str, dict[str, Any]],
    planned: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": str(path),
        "kind": kind,
        "line": line,
        "code": None,
        "initials": None,
        "extra": [],
        "action": "error",
        "reason": None,
        "record_id": None,
        "differences": [],
    }
    participant = (cells.get(TSV_ID) or "").strip()
    try:
        row["code"] = check_subject_code(participant.removeprefix("sub-"))
        raw = cells.get(TSV_INITIALS)
        row["initials"] = _initials(raw) if raw is not None and raw.strip() else None
        row["extra"], renamed = _tsv_extra(cells)
        _extra(row["extra"])
    except PeopleError as exc:
        row["reason"] = f"{participant or '(no id)'}: {exc}"
        return row
    if renamed:
        row["reason"] = "column(s) kept under a new name: " + ", ".join(renamed)
    code, initials = row["code"], row["initials"]
    record = existing.get(code)
    if record is not None:
        row["record_id"] = record["id"]
        recorded = dict(record["extra"])
        row["differences"] = [
            name for name, value in row["extra"] if name in recorded and recorded[name] != value
        ]
        if record["initials"] and initials and record["initials"] != initials:
            row["action"] = "conflict"
            row["reason"] = (
                f"sub-{code} is registered as {record['initials']}; this file says {initials}. "
                "Not merged: check which is right and correct it by hand."
            )
        elif any(s["source"] == str(path) for s in record["sources"]):
            row["action"] = "same"
        elif not record["initials"] and initials:
            row["action"] = "fill"
        else:
            row["action"] = "link"
        return row
    first = planned.get(code)
    if first is not None:
        if first["initials"] and initials and first["initials"] != initials:
            row["action"] = "conflict"
            row["reason"] = (
                f"sub-{code} is {first['initials']} in {first['source']} but {initials} here. "
                "Not merged: they may be two people."
            )
        else:
            row["action"] = "link"
        return row
    row["action"] = "new"
    planned[code] = row
    return row


def _csv_change(
    kind: str,
    line: int,
    values: dict[str, str | None],
    header: list[str],
    own: tuple[str, ...],
    current: dict[str, dict[str, Any]],
    seen: set[str],
) -> dict[str, Any]:
    change: dict[str, Any] = {
        "line": line,
        "action": "unchanged",
        "reason": None,
        "record_id": values.get("record_id") or None,
        "fields": {},
    }
    try:
        if kind == "subjects":
            fields: dict[str, Any] = {
                "code": check_subject_code(values.get("subject_id")),
                "initials": _initials(values.get("initials")),
                "notes": _notes(values.get("notes")),
                "extra": _extra(
                    [
                        [name, values.get(name)]
                        for name in header
                        if name not in own and name != "missing_fields"
                    ]
                ),
            }
        else:
            fields = {
                "name": _single_line(values.get("name"), "Name", MAX_NAME, required=True),
                "initials": _initials(values.get("initials")),
                "notes": _notes(values.get("notes")),
            }
        status = values.get("status") or "active"
        _status(status)
    except PeopleError as exc:
        change.update(action="error", reason=str(exc))
        return change
    record_id = change["record_id"]
    if record_id is None:
        if status != "active":
            change.update(action="error", reason="a new record must be active")
            return change
        change.update(action="add", fields=fields)
        return change
    if record_id in seen:
        change.update(action="error", reason=f"{record_id} appears twice")
        return change
    seen.add(record_id)
    record = current.get(record_id)
    if record is None:
        change.update(action="error", reason=f"{record_id} is not a record of this list")
        return change
    try:
        revision = int(values.get("revision") or "")
    except ValueError:
        change.update(action="error", reason="revision is not a number")
        return change
    mine = dict(fields)
    if kind == "subjects":
        mine = {**fields}
        existing = {
            "code": record["code"],
            "initials": record["initials"],
            "notes": record["notes"],
            "extra": record["extra"],
        }
    else:
        existing = {
            "name": record["name"],
            "initials": record["initials"],
            "notes": record["notes"],
        }
    differing = {k: v for k, v in mine.items() if existing[k] != v}
    status_change = status if status != record["status"] else None
    if not differing and status_change is None:
        return change
    if revision != record["revision"]:
        change.update(
            action="conflict",
            reason=f"edited from revision {revision}, but the record is now at "
            f"{record['revision']}; export again and redo this edit",
        )
        return change
    change.update(action="update", fields=differing, revision=revision, status=status_change)
    return change
