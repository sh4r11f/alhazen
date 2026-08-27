"""The subject registry: one ``participants.tsv`` at the data root.

Deliberately minimal — an append-only id registry with free-form metadata
columns, so every session's subject exists in exactly one place. Richer
schemas (demographics for humans, animal records) can layer on without
changing the file's location or key column.
"""

from __future__ import annotations

import csv
from pathlib import Path

_ID_COLUMN = "participant_id"


def participants_path(data_root: Path) -> Path:
    return data_root / "participants.tsv"


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return list(reader.fieldnames or []), list(reader)


def ensure_participant(
    data_root: Path, subject: str, metadata: dict[str, str] | None = None
) -> None:
    """Register the subject if unknown; never rewrite an existing row (the
    registry is a record, not a cache — corrections are made by a human)."""
    path = participants_path(data_root)
    row = {_ID_COLUMN: f"sub-{subject}", **(metadata or {})}
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row), delimiter="\t")
            writer.writeheader()
            writer.writerow(row)
        return

    columns, rows = _read(path)
    if any(r.get(_ID_COLUMN) == row[_ID_COLUMN] for r in rows):
        return
    # New metadata keys widen the file; existing rows keep blanks there.
    new_columns = columns + [c for c in row if c not in columns]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=new_columns, delimiter="\t")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        writer.writerow(row)
