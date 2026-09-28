"""Where a run's files live, created once and treated as immutable.

The overwrite refusal is the load-bearing rule: a run directory that already
holds any file belongs to an already-started run — a subject's (or animal's)
unrepeatable work — and is never silently reused. Re-running the same
subject/session/task means the next run number, not clobbering, on the same
day or any later one.

Since alhazen 2.0 every run sits under its experiment's version::

    <data_root>/v<version>/sub-<ID>/ses-<NNN>/run-<NN>_task-<name>/

so data recorded by two versions of a protocol never share a folder, and run
numbers count within one version (`session_dir`). What spans versions stays at
the unversioned root: the subject registry (participants.tsv), the experiment
database, each subject's training state. `find_runs` reads both this layout
and the pre-2.0 one, which started at ``sub-<ID>/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from alhazen.data import naming
from alhazen.errors import DataError


def session_dir(data_root: Path | str, experiment_version: str, subject: str, session: int) -> Path:
    """The folder one subject's session keeps its runs in, under a version.

    The one place the layout above the run folder is spelled out: the run
    folder `SessionPaths.create` makes sits directly inside it, and
    `modes.session.next_run` counts the runs already there. Two copies of this
    path would let the counter look in one folder while runs are made in
    another — and then hand out a number that is already taken.
    """
    return (
        Path(data_root)
        / naming.version_dirname(experiment_version)
        / naming.subject_dirname(subject)
        / naming.session_dirname(session)
    )


@dataclass(frozen=True)
class SessionPaths:
    """All paths one run writes. Built by `create`, which is the only place
    directories are made."""

    run_dir: Path
    base: str  # sub-.._ses-.._run-.._task-.._YYYYMMDD

    @classmethod
    def create(
        cls,
        data_root: Path,
        subject: str,
        session: int,
        run: int,
        task_name: str,
        date_yyyymmdd: str | None = None,
        *,
        experiment_version: str,
    ) -> SessionPaths:
        """Make this run's folder under ``data_root``, refusing a used one.

        ``experiment_version`` is required, with no default, because it
        decides which folder the data goes in: a default would file a session
        under a version its experiment never declared. It is the version
        `config.experiment.find_experiment` read (or a caller gave
        `build_session`), already checked to be a valid folder name.
        """
        stamp = date_yyyymmdd or date.today().strftime("%Y%m%d")
        run_dir = session_dir(data_root, experiment_version, subject, session) / (
            naming.run_dirname(run, task_name)
        )
        base = naming.base_name(subject, session, run, task_name, stamp)
        paths = cls(run_dir=run_dir, base=base)
        _refuse_a_used_run_dir(run_dir)
        (run_dir / "figures").mkdir(parents=True, exist_ok=True)
        return paths

    @property
    def trials_path(self) -> Path:
        return self.run_dir / f"{self.base}_trials.csv"

    @property
    def events_path(self) -> Path:
        return self.run_dir / f"{self.base}_events.csv"

    @property
    def frames_path(self) -> Path:
        return self.run_dir / f"{self.base}_frames.csv"

    @property
    def paradigm_path(self) -> Path:
        """Where a scheduler's end-of-session summary lands (an adaptive fit,
        per-cell counts). Written only when the scheduler has one."""
        return self.run_dir / f"{self.base}_paradigm.csv"

    @property
    def snapshot_path(self) -> Path:
        return self.run_dir / "config_snapshot.yaml"

    @property
    def session_json_path(self) -> Path:
        """The run's identity card: which experiment and version, subject,
        rig and files, in one small JSON a person or a script reads first
        (session/identity.py)."""
        return self.run_dir / "session.json"

    @property
    def rig_copy_path(self) -> Path:
        """The rig file the session was started with, copied byte for byte."""
        return self.run_dir / "rig.yaml"

    @property
    def params_copy_path(self) -> Path:
        """The params file the session was started with, copied byte for
        byte; absent when the session ran on the params model's defaults."""
        return self.run_dir / "params.yaml"

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.yaml"

    @property
    def log_path(self) -> Path:
        return self.run_dir / "session.log"

    @property
    def figures_dir(self) -> Path:
        return self.run_dir / "figures"


def _refuse_a_used_run_dir(run_dir: Path) -> None:
    """Refuse a run directory that holds any file at all.

    Checking for this run's own trials file was not enough. Its name carries
    the date and the directory's does not, so the same run number on a later
    day passed the check and wrote into the earlier run's folder: over its
    config_snapshot.yaml, manifest.yaml and live monitor, appending to its
    session.log — and `load_run` then paired one day's trials with the other
    day's snapshot. A run that crashed before writing its trials file (it has
    a snapshot and a log) is protected the same way.

    An EMPTY directory is not a run: a build that failed before the session
    started (a tracker that would not connect) leaves one behind, with only
    the empty ``figures`` folder, and trying again with the same number is
    fine.
    """
    if not run_dir.exists():
        return
    found = sorted(path for path in run_dir.rglob("*") if path.is_file())
    if not found:
        return
    # A few names are enough to recognise the run; all of them would bury
    # the instruction at the end of the message.
    names = ", ".join(path.relative_to(run_dir).as_posix() for path in found[:3])
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    raise DataError(
        f"refusing to overwrite existing run data in {run_dir} ({names}{more}) — "
        f"use the next run number"
    )


@dataclass(frozen=True)
class RunFolder:
    """One run folder found under a data root, and what its path says.

    ``experiment_version`` is None for a run recorded before alhazen 2.0,
    which sits directly under the data root (``sub-<ID>/...``) with no
    version folder above it. ``task`` is None for a run folder whose name
    carries no task segment (``run-07``).
    """

    path: Path
    experiment_version: str | None
    subject: str
    session: int
    run: int
    task: str | None


def find_runs(data_root: Path | str) -> list[RunFolder]:
    """Every run folder under ``data_root``, in both layouts, sorted by path.

    What a script that globbed ``data/sub-*/ses-*/run-*`` before 2.0 calls
    instead: that glob finds none of the runs recorded since, which sit one
    level down, under ``v<version>/``. Both layouts are read, so a data root
    holding runs from before and after the upgrade is read whole, and each
    run says which layout it came from (``experiment_version`` None: pre-2.0).

    A folder counts only when every level of its path is one alhazen writes
    (`naming`): a ``v<version>`` folder (or none, for the old layout), then
    ``sub-<ID>``, ``ses-<NNN>``, ``run-<NN>...``. Anything else under the
    root — the database, participants.tsv, a subject's training state, a
    folder of notes — is not a run and is not returned. A rehearsal's data
    lives under its own root (``<data_root>-rehearsal``), so it is found by
    calling this on that root, never mixed into the real one.
    """
    root = Path(data_root)
    if not root.is_dir():
        return []
    found: list[RunFolder] = []
    for top in sorted(root.iterdir()):
        if not top.is_dir():
            continue
        version = naming.parse_version_dirname(top.name)
        if version is not None:
            for subject_folder in sorted(top.glob("sub-*")):
                found.extend(_runs_of_subject(subject_folder, version))
        elif top.name.startswith("sub-"):
            # The pre-2.0 layout: this IS a subject folder, one level up.
            found.extend(_runs_of_subject(top, None))
    return sorted(found, key=lambda run: run.path)


def _runs_of_subject(subject_folder: Path, version: str | None) -> Iterator[RunFolder]:
    """The run folders under one ``sub-<ID>`` folder, in either layout."""
    if not subject_folder.is_dir():
        return
    subject = subject_folder.name[len("sub-") :]
    for session_folder in sorted(subject_folder.glob("ses-*")):
        number = session_folder.name[len("ses-") :]
        # ASCII digits only, as `naming.session_dirname` writes them: `isdigit`
        # alone would accept digits from other scripts, which int() then reads.
        if not (session_folder.is_dir() and number.isascii() and number.isdigit()):
            continue
        for run_folder in sorted(session_folder.glob("run-*")):
            run = naming.parse_run_dirname(run_folder.name)
            if run is None or not run_folder.is_dir():
                continue
            yield RunFolder(
                path=run_folder,
                experiment_version=version,
                subject=subject,
                session=int(number),
                run=run,
                task=naming.parse_run_task(run_folder.name),
            )
