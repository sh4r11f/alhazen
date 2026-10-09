"""The derived trial index and the trial-table exports.

Hides: how a committed session's ``trials.csv`` / ``*_trials.csv`` files
become bounded query rows, and how rows are written back out safely.

The index is DERIVED: the committed raw files are the record, and
`index_session` can rebuild the rows from them at any time (it replaces a
session's rows in one transaction). A session stays visible and its files
downloadable whatever its index state. Budgets (review gate M4) are checked
while streaming the file; exceeding one fails the index with a clear error
and keeps no rows, never a silently truncated table.

Exports stream from the index in key order. CSV cells that a spreadsheet
would run as a formula (leading ``= + - @``, tab or carriage return) are
prefixed with an apostrophe, except values that are plain numbers, so
negative measurements stay numbers.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Iterator
from typing import Any

from sqlalchemy import delete, insert, select, update

from alhazen.hub.context import Hub
from alhazen.hub.schema import data_sessions, session_files, trial_rows

log = logging.getLogger(__name__)

_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_BATCH = 500
CLAIM_MS = 10 * 60 * 1000


class IndexBudget(ValueError):
    """A trials table broke an index budget or is not a readable CSV."""


def safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_START) and not _NUMBER.match(text):
        return "'" + text
    return text


# -- indexing -----------------------------------------------------------------


def claim_next(hub: Hub, session_id: str | None = None) -> str | None:
    """Claim one session whose index is due (or the given one); None if none."""
    now = hub.clock()
    with hub.db.transaction() as conn:
        query = select(data_sessions.c.id).where(
            data_sessions.c.status == "committed",
            (data_sessions.c.index_status == "pending")
            | (
                (data_sessions.c.index_status == "indexing")
                & (data_sessions.c.index_claimed_until <= now)
            ),
        )
        if session_id is not None:
            query = query.where(data_sessions.c.id == session_id)
        row = conn.execute(query.order_by(data_sessions.c.completed_at).limit(1)).first()
        if row is None:
            return None
        taken = conn.execute(
            update(data_sessions)
            .where(
                data_sessions.c.id == row.id,
                (data_sessions.c.index_status == "pending")
                | (
                    (data_sessions.c.index_status == "indexing")
                    & (data_sessions.c.index_claimed_until <= now)
                ),
            )
            .values(index_status="indexing", index_claimed_until=now + CLAIM_MS)
        ).rowcount
        return str(row.id) if taken else None


def request_reindex(hub: Hub, owner_id: str | None, session_id: str) -> bool:
    """Mark a committed session's index for rebuilding. Idempotent."""
    with hub.db.transaction() as conn:
        query = update(data_sessions).where(
            data_sessions.c.id == session_id,
            data_sessions.c.status == "committed",
            data_sessions.c.index_status.in_(("indexed", "failed", "none", "pending")),
        )
        if owner_id is not None:
            query = query.where(data_sessions.c.owner_id == owner_id)
        changed = conn.execute(
            query.values(index_status="pending", index_claimed_until=None)
        ).rowcount
    return bool(changed)


def index_session(hub: Hub, session_id: str) -> str:
    """Build the derived rows of one claimed session; return the new status."""
    limits = hub.settings.limits
    with hub.db.transaction() as conn:
        row = conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()
        tables = [
            f.path
            for f in conn.execute(
                select(session_files.c.path).where(session_files.c.session_id == session_id)
            ).all()
            if is_trials_table(f.path)
        ]
    tables.sort()
    status, error, count = "indexed", None, 0
    columns: list[str] = []
    try:
        if not tables:
            status = "none"
        else:
            with hub.db.transaction() as conn:
                conn.execute(delete(trial_rows).where(trial_rows.c.session_id == session_id))
                batch: list[dict[str, Any]] = []
                for source, values in _rows(hub, row.experiment_id, session_id, tables, columns):
                    if count >= limits.max_indexed_rows:
                        raise IndexBudget(
                            f"more than {limits.max_indexed_rows} trial rows (the index budget)"
                        )
                    batch.append(
                        {
                            "session_id": session_id,
                            "ordinal": count,
                            "source_path": source,
                            "row_values": json.dumps(values, ensure_ascii=False),
                        }
                    )
                    count += 1
                    if len(batch) >= _BATCH:
                        conn.execute(insert(trial_rows), batch)
                        batch = []
                if batch:
                    conn.execute(insert(trial_rows), batch)
                _finish(conn, session_id, "indexed", None, count, columns)
            return "indexed"
    except IndexBudget as exc:
        status, error, count, columns = "failed", str(exc)[:500], 0, []
    except Exception:
        log.exception("indexing session %s failed", session_id)
        status, error, count, columns = "failed", "the trial table could not be indexed", 0, []
    with hub.db.transaction() as conn:
        conn.execute(delete(trial_rows).where(trial_rows.c.session_id == session_id))
        _finish(conn, session_id, status, error, count, columns)
    return status


def _finish(
    conn: Any, session_id: str, status: str, error: str | None, count: int, columns: list[str]
) -> None:
    conn.execute(
        update(data_sessions)
        .where(data_sessions.c.id == session_id)
        .values(
            index_status=status,
            index_error=error,
            index_rows=count,
            index_columns=json.dumps(columns) if columns else None,
            index_claimed_until=None,
        )
    )


def is_trials_table(path: str) -> bool:
    """Whether a session file is a trial table the index reads."""
    name = path.rsplit("/", 1)[-1]
    return name == "trials.csv" or name.endswith("_trials.csv")


def _rows(
    hub: Hub, experiment_id: str, session_id: str, tables: list[str], columns: list[str]
) -> Iterator[tuple[str, dict[str, str]]]:
    """(source path, {column: value}) for every row; extends ``columns`` in
    first-seen order as headers are read."""
    limits = hub.settings.limits
    known = set(columns)
    for source in tables:
        path = hub.store.final_file(experiment_id, session_id, source)
        if path.stat().st_size > limits.max_indexed_file_bytes:
            raise IndexBudget(f"{source} is larger than {limits.max_indexed_file_bytes} bytes")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(_bounded_lines(handle, limits.max_csv_row_bytes, source))
                header = next(reader, None)
                if not header:
                    continue
                if len(header) > limits.max_indexed_columns:
                    raise IndexBudget(
                        f"{source} has more than {limits.max_indexed_columns} columns"
                    )
                if len(set(header)) != len(header) or any(not h for h in header):
                    raise IndexBudget(f"{source} has empty or repeated column names")
                for name in header:
                    if name not in known:
                        known.add(name)
                        columns.append(name)
                if len(columns) > limits.max_indexed_columns:
                    raise IndexBudget(
                        f"the trial tables together exceed {limits.max_indexed_columns} columns"
                    )
                for number, record in enumerate(reader, start=2):
                    if len(record) != len(header):
                        raise IndexBudget(
                            f"{source} record {number} has {len(record)} fields, "
                            f"the header {len(header)}"
                        )
                    if sum(len(cell) for cell in record) > limits.max_csv_row_bytes:
                        raise IndexBudget(f"{source} record {number} is longer than the row budget")
                    if any(len(cell) > limits.max_csv_cell_bytes for cell in record):
                        raise IndexBudget(
                            f"{source} record {number} has a cell over the cell budget"
                        )
                    yield source, dict(zip(header, record, strict=True))
        except UnicodeDecodeError:
            raise IndexBudget(f"{source} is not UTF-8 text") from None
        except csv.Error as exc:
            raise IndexBudget(f"{source} is not a readable CSV: {exc}") from None


def _bounded_lines(handle: io.TextIOBase, limit: int, source: str) -> Iterator[str]:
    while True:
        line = handle.readline(limit + 1)
        if not line:
            return
        if len(line) > limit:
            raise IndexBudget(f"{source} has a line longer than the row budget")
        yield line


# -- reading and export -------------------------------------------------------


def page(hub: Hub, session_id: str, limit: int, offset: int) -> tuple[list[dict[str, Any]], bool]:
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(trial_rows)
            .where(trial_rows.c.session_id == session_id, trial_rows.c.ordinal >= offset)
            .order_by(trial_rows.c.ordinal)
            .limit(limit + 1)
        ).all()
    items = [
        {
            "ordinal": int(r.ordinal),
            "source_path": r.source_path,
            "values": json.loads(r.row_values),
        }
        for r in rows[:limit]
    ]
    return items, len(rows) > limit


def _batches(hub: Hub, session_id: str) -> Iterator[list[Any]]:
    after = -1
    while True:
        with hub.db.transaction() as conn:
            rows = conn.execute(
                select(trial_rows)
                .where(trial_rows.c.session_id == session_id, trial_rows.c.ordinal > after)
                .order_by(trial_rows.c.ordinal)
                .limit(_BATCH)
            ).all()
        if not rows:
            return
        yield list(rows)
        after = int(rows[-1].ordinal)


def export_csv(
    hub: Hub, session_id: str, columns: list[str], multiple_sources: bool
) -> Iterator[bytes]:
    header = (["source_file"] if multiple_sources else []) + columns
    yield _csv_line([safe_cell(h) for h in header])
    for rows in _batches(hub, session_id):
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        for r in rows:
            values = json.loads(r.row_values)
            cells = ([r.source_path] if multiple_sources else []) + [
                values.get(c, "") for c in columns
            ]
            writer.writerow([safe_cell(c) for c in cells])
        yield buffer.getvalue().encode("utf-8")


def _csv_line(cells: list[str]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(cells)
    return buffer.getvalue().encode("utf-8")


def export_json(hub: Hub, session_id: str, columns: list[str]) -> Iterator[bytes]:
    yield (
        '{"session_id":'
        + json.dumps(session_id)
        + ',"columns":'
        + json.dumps(columns, ensure_ascii=False)
        + ',"rows":['
    ).encode("utf-8")
    first = True
    for rows in _batches(hub, session_id):
        parts = []
        for r in rows:
            item = {
                "ordinal": int(r.ordinal),
                "source_path": r.source_path,
                "values": json.loads(r.row_values),
            }
            parts.append(json.dumps(item, ensure_ascii=False))
        chunk = ",".join(parts)
        yield (chunk if first else "," + chunk).encode("utf-8")
        first = False
    yield b"]}"
