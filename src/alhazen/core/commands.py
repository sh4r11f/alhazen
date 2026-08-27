"""Experimenter runtime commands.

The engine polls a `CommandSource` once per frame and acts on session-control
commands; everything else about *how* keys map to commands lives behind the
protocol, so a scripted source (tests), a null source (unattended simulated
runs), and the real keyboard are interchangeable. The default key map is the
one both source repos trained their experimenters on.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum, auto
from typing import Protocol

# A key getter is handed the filter it should apply — a list of key names, or
# None for "every key". See KeyboardCommands._get_keys for why the caller and
# not the getter decides that.
KeyGetter = Callable[[list[str] | None], list[tuple[str, dict[str, bool]]]]


class Command(Enum):
    SKIP_TRIAL = auto()  # abort this trial, keep the session going
    PAUSE = auto()  # end the trial as PAUSED; runner opens the pause menu
    CALIBRATE = auto()  # PAUSE + ask the runner to recalibrate before resuming
    QUIT = auto()  # end the whole session (data collected so far is saved)
    MANUAL_REWARD = auto()  # deliver a reward now; does not end the trial
    # Training controls. None of these ends the trial: the runner applies
    # them between trials, because a stage change mid-trial would record one
    # row at a difficulty that was only true for part of it.
    PROMOTE_STAGE = auto()
    DEMOTE_STAGE = auto()
    HOLD_STAGE = auto()  # toggle: suspend automatic transitions


DEFAULT_KEYMAP: dict[str, Command] = {
    "escape": Command.SKIP_TRIAL,
    "p": Command.PAUSE,
    "c": Command.CALIBRATE,
    "ctrl+c": Command.QUIT,
    "r": Command.MANUAL_REWARD,
    # Bracket keys for the stage the subject is on: right for forward, left
    # for back, and 'h' to hold where it is. PsychoPy/pyglet reports these two
    # by name, never as the literal character, so the names are what a real
    # rig binds; the literals stay for scripted callers that spell them out.
    "bracketright": Command.PROMOTE_STAGE,
    "bracketleft": Command.DEMOTE_STAGE,
    "]": Command.PROMOTE_STAGE,
    "[": Command.DEMOTE_STAGE,
    "h": Command.HOLD_STAGE,
}


class CommandSource(Protocol):
    def poll(self) -> list[Command]:
        """Commands issued since the last poll, oldest first."""
        ...

    def poll_raw_keys(self) -> list[str]:
        """Raw key names since the last poll — for modal prompts (the pause
        menu) that read keys outside the command map."""
        ...


class NullCommands:
    """No experimenter present (unattended simulated sessions, CI)."""

    def poll(self) -> list[Command]:
        return []

    def poll_raw_keys(self) -> list[str]:
        return []


class KeyboardCommands:
    """Real keyboard via a pluggable key getter. The production default reads
    PsychoPy's event queue (lazy import); tests inject a scripted getter.
    Keys arrive as (name, modifiers) pairs; 'c' with ctrl held maps to QUIT
    per the default map's 'ctrl+c'.

    The getter takes the key filter as its argument rather than deciding it,
    because the two polls want different filters and only the caller knows
    which is which — see ``_get_keys``.
    """

    def __init__(
        self,
        keymap: dict[str, Command] | None = None,
        key_getter: KeyGetter | None = None,
    ) -> None:
        self._keymap = dict(DEFAULT_KEYMAP if keymap is None else keymap)
        self._key_getter = key_getter

    def _base_keys(self) -> list[str]:
        """The map's keys, with modifier combinations reduced to the key that
        actually arrives from the keyboard ("ctrl+c" -> "c"); poll()
        re-applies the modifier."""
        return sorted({name.split("+")[-1] for name in self._keymap})

    def _get_keys(self, names: list[str] | None) -> list[tuple[str, dict[str, bool]]]:
        """Read the queue, restricted to ``names`` (None means every key).

        The restriction is not cosmetic: reading a key DRAINS it. During a
        trial, poll() must ask only for the keys this map binds, or the
        subject's response keys — polled from the same queue moments later in
        the same frame (devices/response.py) — would simply never arrive.
        poll_raw_keys() is the opposite case and passes None: it runs only
        while the session is paused, nothing else is polling, and the keys it
        waits for (space, q) are deliberately outside the command map.
        """
        if self._key_getter is not None:
            return self._key_getter(names)
        from psychopy import event

        if names is None:
            return event.getKeys(modifiers=True)
        return event.getKeys(keyList=names, modifiers=True)

    def poll(self) -> list[Command]:
        commands = []
        for key, modifiers in self._get_keys(self._base_keys()):
            name = f"ctrl+{key}" if modifiers.get("ctrl") else key
            cmd = self._keymap.get(name)
            if cmd is not None:
                commands.append(cmd)
        return commands

    def poll_raw_keys(self) -> list[str]:
        return [key for key, _ in self._get_keys(None)]
