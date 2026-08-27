"""Filename and directory naming conventions.

BIDS-inspired, not BIDS-compliant: ``sub-<ID>/ses-<NNN>/run-<NN>_task-<name>/``
with per-file basenames ``sub-<ID>_ses-<NNN>_run-<NN>_task-<name>_<YYYYMMDD>``.
Zero-padding widths are fixed so directories sort correctly as plain strings.
Validation of the segments themselves lives in `SessionInfo` (config/models);
these helpers only format already-validated values.
"""

from __future__ import annotations


def subject_dirname(subject: str) -> str:
    return f"sub-{subject}"


def session_dirname(session: int) -> str:
    return f"ses-{session:03d}"


def run_dirname(run: int, task_name: str) -> str:
    return f"run-{run:02d}_task-{task_name}"


def base_name(subject: str, session: int, run: int, task_name: str, date_yyyymmdd: str) -> str:
    return f"sub-{subject}_ses-{session:03d}_run-{run:02d}_task-{task_name}_{date_yyyymmdd}"
