"""The eye-tracker seam: what a gaze sample is, and what every backend does.

Coordinate contract, stated once here because getting it wrong is invisible
until analysis: a ``GazeSample`` is in **screen px** (origin top-left, y grows
down) — the frame trackers natively speak — while phases see **centered px**
(origin at the screen centre, y up). The conversion happens in exactly one
place, the session builder's input-provider closure, and nowhere else.

``t`` is the *session* clock's time, not the tracker's own. Every other timed
thing (events, flips, commands) is stamped on that same clock, so gaze can be
compared against them with no epoch or unit conversion. The tracker's native
clock reappears only offline, in the EDF, where the messages we write into it
provide the alignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from alhazen.core.clock import Clock
from alhazen.display.screen import Screen


@dataclass(frozen=True)
class GazeSample:
    """One gaze reading in screen px, stamped on the session clock."""

    gx: float
    gy: float
    t: float


@dataclass(frozen=True)
class HostShape:
    """A shape for the EyeLink Host PC's operator display — the fixation and
    region windows drawn over the live eye image so the experimenter can see
    where the subject has to look. Screen px; ``kind="cross"`` uses only
    (x1, y1), ``kind="box"`` uses all four corners. Never drawn on the
    subject-facing window."""

    kind: Literal["cross", "box"]
    x1: int
    y1: int
    x2: int = 0
    y2: int = 0


@runtime_checkable
class EyeTracker(Protocol):
    """Every backend satisfies this structurally — none of them inherit it.

    ``get_gaze()`` returning None is the *normal* signal for "no verifiable
    position right now" (blink, track loss, no sample yet), not an error: the
    blink rule (core/trial.CircleRegion) turns that None into "outside every
    region", so fixation is never credited when it cannot be verified.
    """

    def connect(self) -> None: ...

    def configure(self, screen: Screen, clock: Clock) -> None:
        """Hand the backend the screen geometry and the SESSION clock.

        The clock is part of the contract, not a convenience: a ``GazeSample``
        carries ``t`` on the session clock, and a backend that reaches for
        ``time.monotonic()`` instead has quietly introduced a second clock
        into a session that is supposed to have exactly one (invariant 2).
        Every backend takes it, whether it stamps samples from it or reads
        them from the vendor's own stream.
        """
        ...

    def calibrate(self) -> None: ...

    def start_trial(self, trial_index: int, status: str) -> None: ...

    def stop_trial(self) -> None:
        """Idempotent: the runner guarantees this runs in a ``finally``, so it
        may be called on a trial that never started recording."""
        ...

    def is_recording(self) -> bool: ...

    def get_gaze(self) -> GazeSample | None: ...

    def send_message(self, text: str) -> None: ...

    def draw_host_overlay(self, shapes: list[HostShape]) -> None: ...

    def shutdown(self, recording_destination: Path | None, /) -> None:
        """Release the device and leave its native eye recording at this path.

        The path is the run directory's base name carrying the EyeLink's
        ``.edf`` suffix — historical, because the EDF was the first and for a
        while the only native recording alhazen retrieved. The *suffix* is the
        backend's to replace: a TRACKPixx3 writes CSV, so the viewpixx backend
        takes the stem and adds its own files. Only the directory and the
        stem are a promise the runner makes.

        Positional-only so that the parameter's name is not part of the
        contract: an experiment package that implements this protocol should
        be free to call it whatever its own recording is.
        """
        ...
