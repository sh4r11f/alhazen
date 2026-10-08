"""The subject registry: one ``participants.tsv`` at the data root.

Deliberately minimal — an append-only id registry with free-form metadata
columns, so every session's subject exists in exactly one place. Richer
schemas (demographics for humans, animal records) can layer on without
changing the file's location or key column.

It sits at the UNversioned data root (alhazen 2.0 files runs under
``v<version>/``): a subject is the same person or animal whatever version of
the protocol they sit through.

**Initials** (alhazen 2.0) are the second thing a session says about its
subject, beside the id, and the registry is what makes them a check: the
first session that names a subject's initials records them, and a later one
that gives the same id with different initials is refused
(`check_participant`) — the likeliest cause is a mistyped subject number,
which would otherwise file one person's session under another's id. A row
registered before 2.0, with no initials, has them filled in by the first
session that gives them; that is completing a record, not correcting one.
Initials live only here and in the run's record, never in a file or folder
name.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from alhazen.data import naming
from alhazen.data.atomic import replace_atomically
from alhazen.errors import DataError

_ID_COLUMN = "participant_id"
_INITIALS_COLUMN = "initials"


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


def _recorded_initials(row: dict[str, str]) -> str:
    """The initials a registry row holds, as recorded ("" when none): a
    hand-edited cell may carry spaces or lowercase, which are not a
    different person."""
    return (row.get(_INITIALS_COLUMN) or "").strip().upper()


def _refuse_other_initials(path: Path, subject: str, recorded: str, initials: str) -> None:
    if recorded != initials:
        raise DataError(
            f"{naming.subject_dirname(subject)} is recorded as {recorded}; this session says "
            f"{initials} — check the subject number. Nothing was written. If the registry "
            f"is what is wrong, correct the {_INITIALS_COLUMN} column for "
            f"{naming.subject_dirname(subject)} in {path} by hand, then start again."
        )


def check_participant(data_root: Path, subject: str, initials: str | None) -> None:
    """Refuse a session whose subject's initials disagree with the registry.

    Reads only, so the session builder can ask before it writes anything —
    a run folder, a database row, the registry itself. Nothing to check when
    the session gives no initials, the registry does not exist yet, the
    subject is new, or its row has no initials recorded (a subject from
    before 2.0: `ensure_participant` fills them in). ``initials`` are
    expected already normalised (config.models.normalize_initials).
    """
    if initials is None:
        return
    path = participants_path(data_root)
    if not path.exists():
        return
    _columns, rows = _read(path)
    participant_id = naming.subject_dirname(subject)
    for row in rows:
        if row.get(_ID_COLUMN) == participant_id:
            recorded = _recorded_initials(row)
            if recorded:
                _refuse_other_initials(path, subject, recorded, initials)
            return


def ensure_participant(
    data_root: Path,
    subject: str,
    metadata: dict[str, str] | None = None,
    *,
    initials: str | None = None,
) -> None:
    """Register the subject if unknown; never rewrite what an existing row
    records (the registry is a record, not a cache — corrections are made by
    a human).

    ``initials`` are recorded with a new subject. For a subject already here
    they are checked (`check_participant`'s refusal, again, in case the file
    changed since the build asked) and, on a row that has none, filled in:
    the one change made to an existing row, because it adds to the record
    rather than altering it. None records and checks nothing.
    """
    path = participants_path(data_root)
    participant_id = naming.subject_dirname(subject)
    # The id, the initials beside it, then the rest (age, sex, …): the order
    # a new file's columns take.
    row = {_ID_COLUMN: participant_id}
    if initials is not None:
        row[_INITIALS_COLUMN] = initials
    row.update(metadata or {})
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row), delimiter="\t")
            writer.writeheader()
            writer.writerow(row)
        return

    columns, rows = _read(path)
    existing = next((r for r in rows if r.get(_ID_COLUMN) == participant_id), None)
    if existing is None:
        rows.append(row)
    elif initials is None:
        return
    else:
        recorded = _recorded_initials(existing)
        if recorded:
            _refuse_other_initials(path, subject, recorded, initials)
            return  # the same initials: nothing to add
        existing[_INITIALS_COLUMN] = initials
    # New keys — metadata, or the initials column on a registry from before
    # 2.0 — widen the file; rows without them keep blanks there.
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
    # newline="": csv wrote its own CRLF line endings; they go to disk as they are.
    replace_atomically(path, buffer.getvalue(), newline="")
