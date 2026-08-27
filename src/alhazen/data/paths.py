"""Where a run's files live, created once and treated as immutable.

The overwrite refusal is the load-bearing rule: a run directory that already
holds a trials file belongs to an already-recorded run — a subject's (or
animal's) unrepeatable work — and is never silently reused. Re-running the
same subject/session/task means the next run number, not clobbering.
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
        if paths.trials_path.exists():
            raise DataError(
                f"refusing to overwrite existing run data at {paths.trials_path} — "
                f"use the next run number"
            )
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
