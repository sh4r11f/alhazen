"""Where a run's files live, created once and treated as immutable.

The overwrite refusal is the load-bearing rule: a run directory that already
holds any file belongs to an already-started run — a subject's (or animal's)
unrepeatable work — and is never silently reused. Re-running the same
subject/session/task means the next run number, not clobbering, on the same
day or any later one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from alhazen.data import naming
from alhazen.errors import DataError


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
    ) -> SessionPaths:
        stamp = date_yyyymmdd or date.today().strftime("%Y%m%d")
        run_dir = (
            data_root
            / naming.subject_dirname(subject)
            / naming.session_dirname(session)
            / naming.run_dirname(run, task_name)
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
    config_snapshot.yaml, manifest.yaml and dashboard, appending to its
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
