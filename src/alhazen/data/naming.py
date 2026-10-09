"""Filename and directory naming conventions.

BIDS-inspired, not BIDS-compliant:
``v<version>/sub-<ID>/ses-<NNN>/run-<NN>_task-<name>/`` with per-file
basenames ``sub-<ID>_ses-<NNN>_run-<NN>_<YYYYMMDD>``. The task is in the run
folder's name and not in the file names (it was in both before 3.0): saying it
twice made a run's paths long enough to pass Windows' 260-character limit, and
a file over that limit cannot be written at all. The first
level is the experiment's version (alhazen 2.0): data recorded by two
versions of an experiment's protocol never share a folder. Before 2.0 the
layout started at ``sub-<ID>/``; `parse_version_dirname` is what lets a reader
tell the two apart.

Zero-padding widths are fixed so directories sort correctly as plain strings.
Validation of the segments themselves lives upstream — the subject and task
in `SessionInfo` (config/models), the version in `find_experiment`
(config/experiment) — and these helpers only format already-validated values,
and read some back (`parse_run_dirname` for numbering the next run,
`parse_version_dirname` for finding runs in both layouts). Code that builds or
reads these names goes through here rather than spelling ``f"ses-{n:03d}"``
out again, so the layout has one definition.
"""

from __future__ import annotations

import re

# A run directory's name as `run_dirname` writes it, read back: "run-", ASCII
# digits, then the end of the name or the "_" that starts the task segment.
# ASCII only, because `\d` would also accept digits from other scripts, which
# `run_dirname` never writes. No "_task-" is required after the number: a
# folder named just "run-07" is still somebody's run 7, and not counting it
# would hand the number 7 out again.
_RUN_DIRNAME = re.compile(r"run-([0-9]+)(?:_task-(.+)|_.*)?")

# A version folder's name as `version_dirname` writes it: "v", then the
# characters config/experiment.py's VERSION_PATTERN lets a version have. The
# pattern is repeated rather than imported because config and data are
# independent layers; a version this cannot read back is one find_experiment
# already refused, so the two cannot disagree about a folder alhazen wrote.
_VERSION_DIRNAME = re.compile(r"v([0-9A-Za-z][0-9A-Za-z.+_-]*)")


def version_dirname(version: str) -> str:
    """The folder a version's data lives in: ``v0.4.0`` for version 0.4.0.

    The ``v`` keeps the folder from looking like a number to a person or a
    sorting tool, and is what tells a version folder from a pre-2.0 subject
    folder (``sub-...``) at the top of a data root.
    """
    return f"v{version}"


def parse_version_dirname(name: str) -> str | None:
    """The version a folder name holds (``v0.4.0`` -> ``0.4.0``); None when
    the name is not a version folder (``sub-01``, ``participants.tsv``)."""
    match = _VERSION_DIRNAME.fullmatch(name)
    return match.group(1) if match else None


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


def parse_run_task(name: str) -> str | None:
    """The task a run directory's name holds (``run-02_task-mib-quest`` ->
    ``mib-quest``); None for a name with no task segment (``run-07``) or one
    that is not a run directory at all."""
    match = _RUN_DIRNAME.fullmatch(name)
    return match.group(2) if match else None


def base_name(subject: str, session: int, run: int, date_yyyymmdd: str) -> str:
    """What every data file of one run starts with: ``sub-M1_ses-003_run-02_20260826``.

    The task is left out on purpose. The run folder these files sit in
    already names it (`run_dirname`), and repeating it here cost its whole
    length a second time in every path. Before 3.0 the name was
    ``sub-<ID>_ses-<NNN>_run-<NN>_task-<name>_<YYYYMMDD>``; readers find a
    run's files by their ending (``*_trials.csv``) and its date by the last
    ``_`` segment, so they read both forms.
    """
    return f"sub-{subject}_ses-{session:03d}_run-{run:02d}_{date_yyyymmdd}"
