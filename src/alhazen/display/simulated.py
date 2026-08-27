"""SimulatedDisplay: a first-class headless backend, not a test-only stub.

This is what a laptop with no rig runs (rig config ``display.backend:
"simulated"``): sessions execute end to end, producing real data files, with
no renderer installed. ``flip()`` optionally paces to a frame period so a
simulated session feels like (and times like) the real one; pass
``frame_period_s=0`` to run as fast as the machine allows.

For deterministic unit tests, prefer ``alhazen.testing.FakeDisplay``, which
advances a FakeClock instead of sleeping.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


class _RecordingWindow:
    """Stands in for a renderer window: stimuli built for the simulated
    backend receive this and simply record against it."""

    def __init__(self) -> None:
        self.draw_log: list[str] = []


class SimulatedDisplay:
    kind = "simulated"

    def __init__(self, nominal_refresh_hz: float, frame_period_s: float | None = None) -> None:
        self._nominal_hz = nominal_refresh_hz
        self._period = 1.0 / nominal_refresh_hz if frame_period_s is None else frame_period_s
        self.window = _RecordingWindow()
        self.flip_count = 0
        self.messages: list[str] = []
        self._opened = False
        self._last_flip: float | None = None
        # None until a calibration is applied, so "no gamma" and "gamma 1.0"
        # stay distinguishable.
        self.gamma: float | None = None

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def flip(self, clear: bool = True) -> None:
        # Pace against the *previous* flip, not a fixed sleep, so per-frame
        # work doesn't accumulate drift — the same discipline a vsync'd
        # renderer gives for free.
        if self._period > 0:
            now = time.perf_counter()
            if self._last_flip is not None:
                remaining = self._period - (now - self._last_flip)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_flip = time.perf_counter()
        self.flip_count += 1

    def measure_refresh_rate(self, n_flips: int) -> float:
        """The rate this display paces at — not the rate a stopwatch saw.

        A real backend must measure, because the question is whether the panel
        is doing what the rig config claims. A simulated one has no panel: it
        paces with ``time.sleep``, so timing it measures how accurately the
        host can sleep, which on a loaded machine is 20 Hz for a 60 Hz request
        and fails refresh validation for reasons that have nothing to do with
        the experiment. Reporting the paced rate keeps the check meaningful —
        a deliberately mismatched ``frame_period_s`` still disagrees with the
        nominal rate and still fails — while making it deterministic.
        """
        if self._period <= 0:
            # Unpaced simulation runs as fast as it can; the nominal rate is
            # the only rate it can honestly claim.
            return self._nominal_hz
        return 1.0 / self._period

    def show_message(self, text: str) -> None:
        self.messages.append(text)
        log.info("display message: %s", text)

    def set_gamma(self, gamma: float) -> None:
        # Recorded rather than ignored: a simulated session should still be
        # able to say what correction a real one would have applied.
        self.gamma = gamma
        log.info("simulated display gamma set to %.3f", gamma)
