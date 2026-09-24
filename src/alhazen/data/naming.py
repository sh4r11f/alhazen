"""Filename and directory naming conventions.

BIDS-inspired, not BIDS-compliant: ``sub-<ID>/ses-<NNN>/run-<NN>_task-<name>/``
with per-file basenames ``sub-<ID>_ses-<NNN>_run-<NN>_task-<name>_<YYYYMMDD>``.
Zero-padding widths are fixed so directories sort correctly as plain strings.
Validation of the segments themselves lives in `SessionInfo` (config/models);
these helpers only format already-validated values — and read one back
(`parse_run_dirname`, for numbering the next run). Code that builds or reads
these names goes through here rather than spelling ``f"ses-{n:03d}"`` out
again, so the layout has one definition.
"""

from __future__ import annotations

import re

# A run directory's name as `run_dirname` writes it, read back: "run-", ASCII
# digits, then the end of the name or the "_" that starts the task segment.
# ASCII only, because `\d` would also accept digits from other scripts, which
# `run_dirname` never writes. No "_task-" is required after the number: a
# folder named just "run-07" is still somebody's run 7, and not counting it
# would hand the number 7 out again.
_RUN_DIRNAME = re.compile(r"run-([0-9]+)(?:_.*)?")


def subject_dirname(subject: str) -> str:
    return f"sub-{subject}"


def session_dirname(session: int) -> str:
    return f"ses-{session:03d}"


def run_dirname(run: int, task_name: str) -> str:
    return f"run-{run:02d}_task-{task_name}"


def parse_run_dirname(name: str) -> int | None:
    """The run number in a run directory's name; None when the name is not
    one (``run-notes``, ``run-``, a stray file).

    The inverse of :func:`run_dirname` for the number only: runs are numbered
    per session whatever their task, so the task segment is not read.
    """
    match = _RUN_DIRNAME.fullmatch(name)
    return int(match.group(1)) if match else None


def base_name(subject: str, session: int, run: int, task_name: str, date_yyyymmdd: str) -> str:
    return f"sub-{subject}_ses-{session:03d}_run-{run:02d}_task-{task_name}_{date_yyyymmdd}"
