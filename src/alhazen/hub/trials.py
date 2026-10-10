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
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import OperationalError

from alhazen.hub.context import Hub
from alhazen.hub.errors import HubError
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


class _Superseded(Exception):
    """This index job's token was replaced (rebuild requested or claim taken over)."""


def _due() -> Any:
    return lambda now: (
        (data_sessions.c.index_status == "pending")
        | (
            (data_sessions.c.index_status == "indexing")
            & (data_sessions.c.index_claimed_until <= now)
        )
    )


def claim_next(hub: Hub, session_id: str | None = None) -> tuple[str, str] | None:
    """Claim one session whose index is due (or the given one).

    Returns (session_id, token) or None. The token fences the job: every
    write it makes later is conditional on still holding it.
    """
    now = hub.clock()
    due = _due()(now)
    with hub.db.transaction() as conn:
        query = select(data_sessions.c.id).where(data_sessions.c.status == "committed", due)
        if session_id is not None:
            query = query.where(data_sessions.c.id == session_id)
        row = conn.execute(query.order_by(data_sessions.c.completed_at).limit(1)).first()
        if row is None:
            return None
        token = secrets.token_hex(16)
        taken = conn.execute(
            update(data_sessions)
            .where(data_sessions.c.id == row.id, due)
            .values(index_status="indexing", index_claimed_until=now + CLAIM_MS, index_token=token)
        ).rowcount
        return (str(row.id), token) if taken else None


def request_reindex(hub: Hub, owner_id: str | None, session_id: str) -> str | None:
    """Ask for a committed session's index to be rebuilt; return the new
    status ("pending"), or None if there is no such committed session.

    Idempotent, and effective even while a job runs: the running job's token
    is cleared, so its result is discarded and the rebuild runs after it.
    """
    with hub.db.transaction() as conn:
        query = update(data_sessions).where(
            data_sessions.c.id == session_id, data_sessions.c.status == "committed"
        )
        if owner_id is not None:
            query = query.where(data_sessions.c.owner_id == owner_id)
        changed = conn.execute(
            query.values(index_status="pending", index_claimed_until=None, index_token=None)
        ).rowcount
    return "pending" if changed else None


_TRANSIENT = (OperationalError,)


def index_session(hub: Hub, session_id: str, token: str) -> str:
    """Build the derived rows of one claimed session; return its new status.

    Budget breaches and unreadable tables fail the index visibly and keep no
    rows. A transient database problem returns the job to "pending" for a
    later attempt instead of failing it. A job whose token was replaced
    meanwhile changes nothing ("superseded").
    """
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
    columns: list[str] = []
    count = 0
    try:
        with hub.db.transaction() as conn:
            conn.execute(delete(trial_rows).where(trial_rows.c.session_id == session_id))
            if tables:
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
            status = "indexed" if tables else "none"
            if not _finish(conn, session_id, token, status, None, count, columns):
                raise _Superseded(session_id)
        return status
    except _Superseded:
        return "superseded"
    except IndexBudget as exc:
        error = str(exc)[:500]
    except FileNotFoundError:
        error = "a stored trial table is missing; an operator must restore the session's files"
    except (HubError, *_TRANSIENT) as exc:
        if isinstance(exc, HubError) and exc.code != "database_unavailable":
            raise
        deferred = _defer(hub, session_id, token)
        log.warning(
            "indexing session %s deferred: the database is unavailable%s",
            session_id,
            "" if deferred else "; its claim will lapse",
        )
        return "pending"
    with hub.db.transaction() as conn:
        if not _finish(conn, session_id, token, "failed", error, 0, []):
            return "superseded"
        conn.execute(delete(trial_rows).where(trial_rows.c.session_id == session_id))
    return "failed"


def _defer(hub: Hub, session_id: str, token: str) -> bool:
    """Return a claimed job to "pending"; False if the database is still
    unreachable, in which case the claim lapses after CLAIM_MS and is retaken
    (the designed fallback)."""
    try:
        with hub.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session_id, data_sessions.c.index_token == token)
                .values(index_status="pending", index_claimed_until=None, index_token=None)
            )
    except HubError:
        return False
    return True


def _finish(
    conn: Any,
    session_id: str,
    token: str,
    status: str,
    error: str | None,
    count: int,
    columns: list[str],
) -> bool:
    """Record the job's result, only if it still holds its token."""
    changed = conn.execute(
        update(data_sessions)
        .where(
            data_sessions.c.id == session_id,
            data_sessions.c.index_token == token,
            data_sessions.c.index_status == "indexing",
        )
        .values(
            index_status=status,
            index_error=error,
            index_rows=count,
            index_columns=json.dumps(columns) if columns else None,
            index_claimed_until=None,
            index_token=None,
        )
    ).rowcount
    return bool(changed)


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

# A trial page carries at most this many bytes of row values, so a page of
# wide rows stays far below any client's JSON cap (the rig's is 16 MiB);
# next_offset continues where the page stopped.
PAGE_BYTES = 4 * 1024 * 1024


def page(
    hub: Hub, session_id: str, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int | None]:
    """Up to ``limit`` rows from ``offset`` within PAGE_BYTES (always at least
    one row); returns (items, next_offset or None)."""
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(trial_rows)
            .where(trial_rows.c.session_id == session_id, trial_rows.c.ordinal >= offset)
            .order_by(trial_rows.c.ordinal)
            .limit(limit + 1)
        ).all()
    items: list[dict[str, Any]] = []
    used = 0
    for r in rows[:limit]:
        if items and used + len(r.row_values) > PAGE_BYTES:
            break
        used += len(r.row_values)
        items.append(
            {
                "ordinal": int(r.ordinal),
                "source_path": r.source_path,
                "values": json.loads(r.row_values),
            }
        )
    more = len(items) < len(rows)
    return items, (int(items[-1]["ordinal"]) + 1 if more and items else None)


class IndexChanged(RuntimeError):
    """The trial index was not in the indexed state in the export's snapshot;
    the stream stops (and the client sees an incomplete download) rather than
    end as if complete."""


@contextmanager
def _snapshot(hub: Hub, session_id: str) -> Iterator[tuple[list[str], bool, Iterator[Any]]]:
    """One consistent view of a session's index: its columns, whether rows
    come from several files, and the rows, all read in one snapshot so a
    rebuild committing meanwhile cannot mix two index generations. The
    connection is released when the ``with`` block ends, however it ends."""
    with hub.db.snapshot() as conn:
        state = conn.execute(
            select(data_sessions.c.index_status, data_sessions.c.index_columns).where(
                data_sessions.c.id == session_id
            )
        ).one()
        if state.index_status not in ("indexed", "pending", "indexing") or not state.index_columns:
            raise IndexChanged(session_id)
        columns = json.loads(state.index_columns)
        sources = conn.execute(
            select(func.count(func.distinct(trial_rows.c.source_path))).where(
                trial_rows.c.session_id == session_id
            )
        ).scalar_one()
        result = conn.execution_options(stream_results=True, yield_per=_BATCH).execute(
            select(trial_rows)
            .where(trial_rows.c.session_id == session_id)
            .order_by(trial_rows.c.ordinal)
        )
        try:
            yield columns, int(sources) > 1, iter(result)
        finally:
            result.close()


def export_csv(hub: Hub, session_id: str) -> Iterator[bytes]:
    """The index as CSV, streamed from one snapshot."""
    with _snapshot(hub, session_id) as (columns, multiple, rows):
        header = (["source_file"] if multiple else []) + columns
        yield _csv_line([safe_cell(h) for h in header])
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        pending = 0
        for r in rows:
            values = json.loads(r.row_values)
            cells = ([r.source_path] if multiple else []) + [values.get(c, "") for c in columns]
            writer.writerow([safe_cell(c) for c in cells])
            pending += 1
            if pending >= _BATCH:
                yield buffer.getvalue().encode("utf-8")
                buffer.seek(0)
                buffer.truncate(0)
                pending = 0
        if pending:
            yield buffer.getvalue().encode("utf-8")


def _csv_line(cells: list[str]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(cells)
    return buffer.getvalue().encode("utf-8")


def export_json(hub: Hub, session_id: str) -> Iterator[bytes]:
    """The index as one JSON document, streamed from one snapshot."""
    with _snapshot(hub, session_id) as (columns, _multiple, rows):
        yield (
            '{"session_id":'
            + json.dumps(session_id)
            + ',"columns":'
            + json.dumps(columns, ensure_ascii=False)
            + ',"rows":['
        ).encode("utf-8")
        first = True
        parts: list[str] = []
        for r in rows:
            item = {
                "ordinal": int(r.ordinal),
                "source_path": r.source_path,
                "values": json.loads(r.row_values),
            }
            parts.append(json.dumps(item, ensure_ascii=False))
            if len(parts) >= _BATCH:
                chunk = ",".join(parts)
                yield (chunk if first else "," + chunk).encode("utf-8")
                first, parts = False, []
        if parts:
            chunk = ",".join(parts)
            yield (chunk if first else "," + chunk).encode("utf-8")
        yield b"]}"


class ExportStream:
    """An export whose first part is already read (so refusals happen before
    a response starts), holding one database snapshot until it is exhausted
    or closed.

    Iterate it, and call ``close()`` when the response ends for any reason
    (finished, client gone, cancelled, never started): that releases the
    snapshot's connection at once, without waiting for garbage collection.
    ``close()`` is idempotent; it is also a context manager.
    """

    def __init__(self, first: bytes, rest: Iterator[bytes]) -> None:
        self._first: bytes | None = first
        self._rest = rest
        self._closed = False

    def __iter__(self) -> ExportStream:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        if self._first is not None:
            part, self._first = self._first, None
            return part
        try:
            return next(self._rest)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._first = None
            close = getattr(self._rest, "close", None)
            if close is not None:
                close()

    def __enter__(self) -> ExportStream:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_export(hub: Hub, session_id: str, fmt: str) -> ExportStream:
    """Start an export and return its stream, with the first part already
    read, so a session whose index is not ready is refused (409) before any
    response starts instead of ending a 200 download early."""
    stream = export_csv(hub, session_id) if fmt == "csv" else export_json(hub, session_id)
    try:
        first = next(stream)
    except StopIteration:  # pragma: no cover - both formats always yield a first part
        raise HubError(500, "internal", "The export produced nothing") from None
    except IndexChanged:
        raise HubError(
            409,
            "index_not_ready",
            "This session's trial index is being rebuilt or is not available; its original "
            "files remain downloadable",
        ) from None
    return ExportStream(first, stream)
