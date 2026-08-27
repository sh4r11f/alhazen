"""What the *subject* does with their hands: key presses and a scroll wheel.

Distinct from ``core/commands.py``, which is the *experimenter's* keyboard —
different person, different keys, different consequences. A subject's press is
data (it becomes a response and a reaction time); an experimenter's is control
(pause, calibrate, quit). Keeping them apart means a task can bind "space"
without colliding with the session controls, and a scripted test can drive one
without the other.

Both live behind one protocol so the engine's per-frame input snapshot is
assembled the same way whatever the rig has attached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResponseSample:
    """One frame's worth of subject input.

    ``keys`` are the presses since the previous poll, oldest first — a list,
    not a single key, because a fast double-press inside one frame must not be
    silently dropped. ``wheel`` is that frame's scroll movement, positive up.
    """

    keys: tuple[str, ...] = ()
    wheel: float = 0.0


@runtime_checkable
class ResponseDevice(Protocol):
    def poll(self) -> ResponseSample:
        """Everything the subject did since the last call. Called once per
        frame by the engine's input provider; each press is reported once."""
        ...


class NullResponse:
    """A rig with nothing for the subject to press (an unattended simulated
    session). Reports nothing, forever."""

    def poll(self) -> ResponseSample:
        return ResponseSample()


class SubjectKeyboard:
    """The subject's keyboard and scroll wheel, read through psychopy.

    Both come from the same window's event queue, so they are polled together
    rather than as two devices the builder would have to merge. A rig with no
    wheel simply reports 0.0 every frame.
    """

    def __init__(
        self,
        keys: tuple[str, ...] | None = None,
        key_getter: Callable[[], list[str]] | None = None,
        wheel_getter: Callable[[], float] | None = None,
        window: Any = None,
    ) -> None:
        # Restricting to the task's own keys at the source (rather than
        # filtering in a phase) keeps an experimenter's session-control press
        # from ever being recorded as if the subject had answered.
        self._keys = tuple(keys) if keys is not None else None
        self._key_getter = key_getter
        self._wheel_getter = wheel_getter
        self._window = window
        self._mouse: Any = None

    def _get_keys(self) -> list[str]:
        if self._key_getter is not None:
            return self._key_getter()
        # Lazy: psychopy must not be imported to construct this class, only
        # to read from it.
        from psychopy import event

        return list(event.getKeys(keyList=self._keys))

    def _get_wheel(self) -> float:
        if self._wheel_getter is not None:
            return self._wheel_getter()
        if self._window is None:
            return 0.0
        from psychopy import event

        if self._mouse is None:
            self._mouse = event.Mouse(win=self._window)
        # getWheelRel returns movement since the last call on both axes; the
        # vertical one is the knob an adjustment task turns.
        return float(self._mouse.getWheelRel()[1])

    def poll(self) -> ResponseSample:
        return ResponseSample(keys=tuple(self._get_keys()), wheel=self._get_wheel())
