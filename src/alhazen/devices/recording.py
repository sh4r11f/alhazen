"""Recording systems: the neural or physiological recorder running alongside.

alhazen does not record neural data and does not try to. What it does is make
a session *findable* and *alignable* afterwards:

- **findable**: every run directory gets a ``recording_pointer.yaml`` naming
  the external recording this session belongs to. Most acquisition software
  cannot be annotated programmatically, and a session whose neural files
  nobody can identify six months later is a session that was not recorded;
- **alignable**: the TTL sync lines put the same events in both
  records, and ``alhazen.analysis.sync`` fits the clocks afterwards.

``check()`` is what ``check-rig`` calls: it answers "is the recorder where it
says it is", before the subject is in the chair rather than after.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from alhazen.config.models import RecordingConfig, SessionInfo

log = logging.getLogger(__name__)

POINTER_FILENAME = "recording_pointer.yaml"
POINTER_SCHEMA_VERSION = 1


@runtime_checkable
class RecordingSystem(Protocol):
    def check(self) -> str | None:
        """None when healthy, else a sentence saying what is wrong."""
        ...

    def annotate_session(self, info: SessionInfo, run_dir: Path) -> None:
        """Record which external recording this run belongs to.

        Always writes the pointer file, whatever else the system supports —
        that file is what ties behavior to neural data in the manifest.
        """
        ...


def write_pointer(
    run_dir: Path,
    info: SessionInfo,
    system: str,
    expected: str,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the run's ``recording_pointer.yaml``.

    Deliberately not clever: subject, session, run, task, the recording system
    and where its files are expected to be. It is written *before* the session
    runs, so a session that crashes still says what it was recording against —
    and the manifest hashes it along with everything else.
    """
    path = run_dir / POINTER_FILENAME
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": POINTER_SCHEMA_VERSION,
                "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "system": system,
                "expected": expected,
                "subject": info.subject,
                "session": info.session,
                "run": info.run,
                "task": info.task_name,
                **(extra or {}),
            },
            sort_keys=False,
        )
    )
    log.info("recording pointer written: %s -> %s", path.name, expected)
    return path


class SimulatedRecording:
    """No recorder attached. Writes the pointer and remembers the calls.

    Used on a rig with no recording system and in tests. It still writes a
    pointer, because a session run without a recorder should say so rather
    than leave the question open.
    """

    def __init__(self, cfg: RecordingConfig | None = None) -> None:
        self._cfg = cfg
        self.annotations: list[tuple[SessionInfo, Path]] = []

    def check(self) -> str | None:
        return None

    def annotate_session(self, info: SessionInfo, run_dir: Path) -> None:
        self.annotations.append((info, run_dir))
        write_pointer(
            run_dir,
            info,
            system="simulated",
            expected="(no recording system attached)",
        )


class SpikeGLXRecording:
    """A SpikeGLX acquisition host, identified by where its files land.

    SpikeGLX has no supported API for starting a run from another program, so
    this does not try: the experimenter starts SpikeGLX, and alhazen records
    *which* run this session expects to be paired with. ``check()`` verifies
    that the data directory is reachable, which is the failure that actually
    happens — a network share that did not mount, discovered after a session
    rather than before.
    """

    def __init__(self, cfg: RecordingConfig) -> None:
        self._cfg = cfg

    def check(self) -> str | None:
        data_dir = Path(self._cfg.data_dir)
        if not data_dir.exists():
            return (
                f"SpikeGLX data directory {data_dir} is not reachable — check that the "
                f"acquisition host's share is mounted before starting a session"
            )
        if not data_dir.is_dir():
            return f"SpikeGLX data_dir {data_dir} exists but is not a directory"
        return None

    def annotate_session(self, info: SessionInfo, run_dir: Path) -> None:
        # The glob rather than a concrete path: the run's own name is chosen
        # in SpikeGLX by the experimenter, and guessing it would produce a
        # pointer that is confidently wrong. What alhazen knows is the
        # directory and the naming convention this rig uses.
        expected = str(Path(self._cfg.data_dir) / self._cfg.run_glob)
        write_pointer(
            run_dir,
            info,
            system="spikeglx",
            expected=expected,
            extra={"data_dir": str(self._cfg.data_dir), "run_glob": self._cfg.run_glob},
        )


def make_recording(cfg: RecordingConfig) -> RecordingSystem:
    """Construct the recorder a rig config names. Shared by session build and
    ``check-rig``, so a clean check exercises the real constructor."""
    if cfg.backend == "spikeglx":
        return SpikeGLXRecording(cfg)
    return SimulatedRecording(cfg)
