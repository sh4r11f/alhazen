"""Public test doubles.

These are part of alhazen's API, not private test plumbing: experiment
packages get the same deterministic, hardware-free test power the framework's
own suite uses. The pairing that makes trials deterministic is FakeClock +
FakeDisplay — every flip advances the clock by exactly one frame period, so
time-based phase logic runs instantly and exactly.
"""

from __future__ import annotations

from typing import Any

from alhazen.core.commands import Command
from alhazen.core.events import Event
from alhazen.core.trial import InputFrame


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


__all__ = [
    "EventCollector",
    "FakeClock",
    "FakeDisplay",
    "FakeStimulus",
    "ScriptedCommands",
    "ScriptedInputs",
]
