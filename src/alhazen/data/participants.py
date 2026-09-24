"""The subject registry: one ``participants.tsv`` at the data root.

Deliberately minimal — an append-only id registry with free-form metadata
columns, so every session's subject exists in exactly one place. Richer
schemas (demographics for humans, animal records) can layer on without
changing the file's location or key column.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from alhazen.data.atomic import replace_atomically
from alhazen.errors import DataError

_ID_COLUMN = "participant_id"


def participants_path(data_root: Path) -> Path:
    return data_root / "participants.tsv"


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """The registry's columns and rows, refusing a row with more cells than
    the header has columns.

    Such a row is a hand edit gone wrong (a stray tab, a pasted cell). csv
    files the surplus under a ``None`` key, which has no column to be written
    back under: dropping it would delete part of a record, and guessing a
    column for it would invent one. So a human fixes it, told which file and
    which line.
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = []
        for row in reader:
            # None is csv's key for the cells past the header's last column.
            surplus = row.get(None)
            if surplus is not None:
                raise DataError(
                    f"{path}, line {reader.line_num}: the row for {row.get(_ID_COLUMN)!r} has "
                    f"more cells than the header has columns (extra: "
                    f"{', '.join(repr(cell) for cell in surplus)}). The registry was not "
                    f"changed; fix that row by hand, or name a column for the extra cells, "
                    f"then start again."
                )
            rows.append(row)
        return list(reader.fieldnames or []), rows


def ensure_participant(
    data_root: Path, subject: str, metadata: dict[str, str] | None = None
) -> None:
    """Register the subject if unknown; never rewrite an existing row (the
    registry is a record, not a cache — corrections are made by a human)."""
    path = participants_path(data_root)
    row = {_ID_COLUMN: f"sub-{subject}", **(metadata or {})}
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row), delimiter="\t")
            writer.writeheader()
            writer.writerow(row)
        return

    columns, rows = _read(path)
    if any(r.get(_ID_COLUMN) == row[_ID_COLUMN] for r in rows):
        return
    # New metadata keys widen the file; existing rows keep blanks there.
    new_columns = columns + [c for c in row if c not in columns]
    # Built in memory and swapped in whole: adding one subject rewrites every
    # row, and rewritten in place, a crash or a full disk part-way through
    # left a truncated file — every earlier subject gone from the only copy.
    # `_read` has already refused a row with cells no column holds, so the
    # writer's default of raising on an unknown key cannot fire here.
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=new_columns, delimiter="\t")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    writer.writerow(row)
    # newline="": csv wrote its own CRLF line endings; they go to disk as they are.
    replace_atomically(path, buffer.getvalue(), newline="")
