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


def paused_only_refusal(label: str) -> str:
    """Why an eye-tracker procedure (calibrate, validate, drift correct) is
    refused while a trial runs: the one sentence the live monitor's page and
    the session's log both show, for a button or the rig's key."""
    return f"{label} works only while the task is paused; pause it first"


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


class MergedCommands:
    """The keyboard and a second, remote source of commands, as one source.

    The live monitor's buttons reach a running trial through here: the engine
    polls this exactly as it polls the keyboard, so a Pause clicked in the
    browser and P pressed at the rig end the trial through the same branch of
    ``TrialEngine._handle_commands`` and are recorded the same way.

    ``remote`` hands over at most one command per call, or None. It is asked
    only on a frame on which ``local`` produced nothing. The engine acts on
    the first trial-ending command of a poll and drops the rest, so a remote
    command that shared a poll with a key could be dropped after the remote
    side had already said it was taken; asked alone, it never shares one, and
    a command left waiting is taken on the next frame. The keyboard comes
    first because it is the control surface at the rig.

    ``poll_raw_keys`` is the keyboard's alone: the pause menu reads the
    browser's choices through its own path (session/pause_control.py).
    """

    def __init__(self, local: CommandSource, remote: Callable[[], Command | None]) -> None:
        self._local = local
        self._remote = remote

    def poll(self) -> list[Command]:
        commands = self._local.poll()
        if commands:
            return commands
        remote = self._remote()
        return [remote] if remote is not None else []

    def poll_raw_keys(self) -> list[str]:
        return self._local.poll_raw_keys()
