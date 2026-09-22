"""Public test doubles.

These are part of alhazen's API, not private test plumbing: experiment
packages get the same deterministic, hardware-free test power the framework's
own suite uses. The pairing that makes trials deterministic is FakeClock +
FakeDisplay — every flip advances the clock by exactly one frame period, so
time-based phase logic runs instantly and exactly.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

from alhazen.config.models import RewardPulses
from alhazen.core.commands import Command
from alhazen.core.events import Event
from alhazen.core.trial import InputFrame
from alhazen.errors import RewardError


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _RecordingWindow:
    def __init__(self) -> None:
        self.draw_log: list[str] = []


class FakeDisplay:
    """A display whose flips advance a FakeClock by one frame period each —
    real time never passes, simulated time is exact. Set ``next_flip_extra``
    to inject one long frame (a dropped frame) for QA tests.

    ``kind`` is "simulated" so backend-branching factories (e.g.
    ``make_fixation``) treat it exactly like the SimulatedDisplay backend.
    """

    kind = "simulated"

    def __init__(self, clock: FakeClock, frame_period_s: float = 1 / 60) -> None:
        self._clock = clock
        self.frame_period_s = frame_period_s
        self.next_flip_extra = 0.0
        self.window: Any = _RecordingWindow()
        self.flip_count = 0
        self.messages: list[str] = []
        self.menus: list[tuple[str, str, tuple[float, float, float]]] = []
        self.closed = False
        self.gamma: float | None = None

    def open(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def flip(self, clear: bool = True) -> None:
        self._clock.advance(self.frame_period_s + self.next_flip_extra)
        self.next_flip_extra = 0.0
        self.flip_count += 1

    def measure_refresh_rate(self, n_flips: int) -> float:
        return 1.0 / self.frame_period_s

    def show_message(self, text: str) -> None:
        self.messages.append(text)

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        # Recorded whole, including the colour: the colour is the part of the
        # pause screen that carries its meaning, so a test that pins the
        # menu's behaviour must be able to see it.
        self.menus.append((title, body, color))
        self.messages.append(title)

    def set_gamma(self, gamma: float) -> None:
        # Recorded, not ignored: this fake must satisfy the whole
        # DisplayBackend protocol, or a session that applies a stored gamma
        # would run everywhere except in the tests that are meant to pin it.
        self.gamma = gamma


class FakeStimulus:
    def __init__(self, name: str = "stimulus") -> None:
        self.name = name
        self.draw_count = 0
        self.updates: list[float] = []

    def update(self, dt: float) -> None:
        self.updates.append(dt)

    def draw(self) -> None:
        self.draw_count += 1


class ScriptedCommands:
    """Serves scripted command batches, one batch per poll, then [] forever.
    ``raw_keys`` batches feed poll_raw_keys the same way (for pause-menu
    tests)."""

    def __init__(
        self,
        batches: list[list[Command]] | None = None,
        raw_keys: list[list[str]] | None = None,
    ) -> None:
        self._batches = list(batches or [])
        self._raw = list(raw_keys or [])

    def poll(self) -> list[Command]:
        return self._batches.pop(0) if self._batches else []

    def poll_raw_keys(self) -> list[str]:
        return self._raw.pop(0) if self._raw else []


class ScriptedInputs:
    """Serves scripted InputFrames, one per call; the last one repeats once
    the script runs out (a subject who keeps looking where they looked)."""

    def __init__(self, frames: list[InputFrame]) -> None:
        self._frames = list(frames)
        self._last = frames[-1] if frames else InputFrame()

    def __call__(self) -> InputFrame:
        if self._frames:
            self._last = self._frames.pop(0)
        return self._last


class EventCollector:
    """Bus subscriber that keeps every event, for assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [e.name for e in self.events]


class ScriptedReward:
    """A reward dispenser a test drives one delivery at a time.

    Mid-trial reward runs its deliveries on a worker thread
    (``devices.reward.QueuedReward``), and the behaviours worth pinning —
    a drop requested while another is still on the valve, a failure
    reported frames after the request — only exist while a delivery is
    *in progress*. A real pump makes that a matter of timing; this makes it
    a matter of the test's say-so, with no sleeps:

    - ``hold=True`` keeps every delivery on the valve until the test calls
      ``release()`` (one delivery per call, oldest first).
    - ``fail`` names the attempts (1-based) that raise ``RewardError``.
    - ``wait_for_attempts(n)`` blocks until n deliveries have *started*, so a
      test knows the worker is inside ``deliver`` before it acts.
    - ``max_on_valve`` is the most deliveries that were ever inside
      ``deliver`` at once. Anything above 1 is two pulse trains overlapping
      on one valve — the thing a serialised reward path exists to prevent.

    Every wait gives up after ``timeout_s`` with an error naming what never
    happened, so a broken hand-off fails the test rather than hanging it.
    """

    def __init__(self, *, hold: bool = False, fail: Iterable[int] = (), timeout_s: float = 5.0):
        self.hold = hold
        self._fail = frozenset(fail)
        self._timeout_s = timeout_s
        # Every deliver() call in order, and the ones that succeeded. Written
        # under the condition, because the worker thread appends to them.
        self.attempts: list[RewardPulses] = []
        self.deliveries: list[RewardPulses] = []
        self.closed = False
        self.on_valve = 0
        self.max_on_valve = 0
        self._released = 0
        self._cond = threading.Condition()

    def deliver(self, pulses: RewardPulses) -> None:
        with self._cond:
            self.attempts.append(pulses)
            attempt = len(self.attempts)
            self.on_valve += 1
            self.max_on_valve = max(self.max_on_valve, self.on_valve)
            self._cond.notify_all()  # wakes wait_for_attempts
            try:
                # wait_for releases the lock while it waits, so a second,
                # overlapping deliver() could get in here — and would show up
                # in max_on_valve.
                if self.hold and not self._cond.wait_for(
                    lambda: self._released >= attempt, timeout=self._timeout_s
                ):
                    raise TimeoutError(
                        f"ScriptedReward: delivery {attempt} was never released — the test "
                        f"held the valve and did not call release()"
                    )
                if attempt in self._fail:
                    raise RewardError(f"scripted failure on delivery {attempt}")
                self.deliveries.append(pulses)
            finally:
                self.on_valve -= 1

    def release(self, n: int = 1) -> None:
        """Let the next ``n`` held deliveries finish."""
        with self._cond:
            self._released += n
            self._cond.notify_all()

    def wait_for_attempts(self, n: int) -> None:
        """Block until at least ``n`` deliveries have started."""
        with self._cond:
            if not self._cond.wait_for(lambda: len(self.attempts) >= n, timeout=self._timeout_s):
                raise TimeoutError(
                    f"ScriptedReward: expected {n} delivery(ies) to start, saw {len(self.attempts)}"
                )

    def close(self) -> None:
        self.closed = True


__all__ = [
    "EventCollector",
    "FakeClock",
    "FakeDisplay",
    "FakeStimulus",
    "ScriptedCommands",
    "ScriptedInputs",
    "ScriptedReward",
]
