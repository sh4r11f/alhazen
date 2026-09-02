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
import sys
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# How much of each frame's wait is spun rather than slept. A sleep returns
# late by up to one scheduler tick — about 1 ms on Linux, macOS and a Windows
# process that has asked for fine ticks (`_request_fine_timer`) — so the last
# 2 ms are spent polling the clock instead. Spinning the whole frame would be
# exact but would pin a core for the length of a session; sleeping the whole
# frame is what turned every 60 Hz frame into a 31 ms one on Windows.
_SPIN_MARGIN_S = 0.002


class _RecordingWindow:
    """Stands in for a renderer window: stimuli built for the simulated
    backend receive this and simply record against it."""

    def __init__(self) -> None:
        self.draw_log: list[str] = []


class SimulatedDisplay:
    kind = "simulated"

    def __init__(
        self,
        nominal_refresh_hz: float,
        frame_period_s: float | None = None,
        *,
        now: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._nominal_hz = nominal_refresh_hz
        self._period = 1.0 / nominal_refresh_hz if frame_period_s is None else frame_period_s
        # Injectable so pacing can be tested against a fake clock rather than
        # by timing real sleeps, which the test suite forbids.
        self._now = now
        self._sleep = sleep
        self.window = _RecordingWindow()
        self.flip_count = 0
        self.messages: list[str] = []
        # (title, body) per menu shown. Kept apart from `messages` so a test
        # or a log reader can ask "did this session ever stop?" without
        # pattern-matching message text.
        self.menus: list[tuple[str, str]] = []
        self._opened = False
        self._last_flip: float | None = None
        self._release_timer: Callable[[], None] | None = None
        # None until a calibration is applied, so "no gamma" and "gamma 1.0"
        # stay distinguishable.
        self.gamma: float | None = None

    def open(self) -> None:
        self._opened = True
        if self._period > 0 and self._release_timer is None:
            self._release_timer = _request_fine_timer()

    def close(self) -> None:
        self._opened = False
        if self._release_timer is not None:
            self._release_timer()
            self._release_timer = None

    def flip(self, clear: bool = True) -> None:
        # Pace against the *previous* flip, not a fixed sleep, so per-frame
        # work doesn't accumulate drift — the same discipline a vsync'd
        # renderer gives for free.
        if self._period > 0:
            if self._last_flip is not None:
                _wait_until(self._last_flip + self._period, now=self._now, sleep=self._sleep)
            self._last_flip = self._now()
        self.flip_count += 1

    def measure_refresh_rate(self, n_flips: int) -> float:
        """The rate this display paces at — not the rate a stopwatch saw.

        A real backend must measure, because the question is whether the panel
        is doing what the rig config claims. A simulated one has no panel: it
        paces with the host's clock, so timing it measures how accurately the
        host can wait, which on a loaded machine is not the rate asked for and
        fails refresh validation for reasons that have nothing to do with the
        experiment. Reporting the paced rate keeps the check meaningful — a
        deliberately mismatched ``frame_period_s`` still disagrees with the
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

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        """Log the menu and keep it, headline first.

        Logged in full rather than summarised: an unattended simulated session
        that pauses does so for a reason (a reward failure is the usual one),
        and the session log is the only place that reason can be read
        afterwards.
        """
        self.menus.append((title, body))
        self.messages.append(title)
        log.info("display menu: %s\n%s", title, body)

    def set_gamma(self, gamma: float) -> None:
        # Recorded rather than ignored: a simulated session should still be
        # able to say what correction a real one would have applied.
        self.gamma = gamma
        log.info("simulated display gamma set to %.3f", gamma)


def _wait_until(
    deadline: float,
    *,
    now: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until the clock reads ``deadline``: sleep most of it, spin the rest.

    ``time.sleep`` promises only "at least this long", and how much longer is
    the scheduler's tick. Sleeping the full remainder therefore lands one tick
    late on every frame, and on a 60 Hz simulation that tick is the whole
    frame: the frame monitor then flags every flip as dropped. Handing the
    last ``_SPIN_MARGIN_S`` to a polling loop absorbs the tick, so the flip
    happens within a few microseconds of when it was due.
    """
    remaining = deadline - now()
    if remaining > _SPIN_MARGIN_S:
        sleep(remaining - _SPIN_MARGIN_S)
    while now() < deadline:
        pass


def _request_fine_timer() -> Callable[[], None]:
    """Ask the OS for 1 ms scheduler ticks; return what undoes it.

    Only Windows needs asking. Its default tick is 15.6 ms, and Python 3.10's
    ``time.sleep`` there rounds every wait up to it, so a 14 ms sleep takes
    15 to 31 ms and no margin short of a whole frame can be spun away. Python
    3.11 sleeps on a high-resolution timer and is unaffected; the request is
    still made because it is harmless, and the 3.10 environment is the one
    that runs at the rig.

    The period is a per-process setting on current Windows, so it lasts until
    ``close()`` or process exit. A failed request (``TIMERR_NOCANDO``) leaves
    pacing coarse rather than refusing to run: a slow simulation is still a
    simulation, and the frame log says what happened.
    """
    if sys.platform != "win32":
        return _no_release
    import ctypes

    winmm = ctypes.windll.winmm
    if winmm.timeBeginPeriod(1) != 0:
        log.warning("could not set a 1 ms timer resolution; simulated frames will pace coarsely")
        return _no_release

    def release() -> None:
        winmm.timeEndPeriod(1)

    return release


def _no_release() -> None:
    return None
