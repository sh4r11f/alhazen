"""OperatorCommands: the live monitor's buttons, as the session takes them.

The page posts a command; the server checks it against the status it holds
(live_monitor/controls.py) and queues it, stamped with that status
(live_monitor/runtime.py). From there this module decides everything:

- **where a command goes.** One accepted while a trial ran goes to the engine,
  through the same ``CommandSource`` the keyboard feeds (core/commands.py
  ``MergedCommands``), one per frame. One accepted while paused goes to the
  pause menu (session/pause_control.py). One accepted while a blocking screen
  is up — the instructions or the calibration request before trial 1
  (``waiting``), an eye-tracker procedure's walk (``calibrating``) — goes to
  that screen, which says beforehand what it answers to (``offer``) and reads
  its commands between its own key reads (``take_offered``). A command is acted on only in the
  status it was accepted in: a Resume accepted a moment before the keyboard
  resumed would otherwise fire in the next trial, and a Reward accepted a
  moment before a pause in the pause after it.
- **what the session will act on.** ``controls(status)`` is the block every
  state publish carries: the buttons the page shows, the ones it enables, the
  key each stands for at the rig, and why each other one is off. The server
  holds a POST to the same list, and ``take_for_trial`` re-checks it, so the
  three cannot disagree.
- **the acknowledgement.** Every command taken off the queue gets one —
  applied, refused, failed or discarded, with a note — as an OPERATOR_COMMAND
  session event (events.csv) and in the record the page reads (the newest
  ``RECENT`` of them, in every state publish as ``commands`` and at once on
  the live monitor's acknowledgement channel).

Interface: ``bind``, ``take_for_trial``, ``take_for_pause``, ``offer``,
``take_offered``, ``withdraw``, ``record_keyboard``, ``discard_pending``,
``acknowledge``, ``controls``, ``recent``.

Callers must not rely on the wording of a note, nor on when within a frame
the queue is read; only on each command being acted on at most once, in the
status it was accepted in, and acknowledged exactly once.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from alhazen.core.clock import Clock
from alhazen.core.commands import DEFAULT_KEYMAP, Command
from alhazen.live_monitor.controls import (
    CONTEXT_COMMANDS,
    KNOWN_COMMANDS,
    LABELS,
    STATUS_COMMANDS,
    status_refusal,
)
from alhazen.live_monitor.runtime import LiveMonitorCommand
from alhazen.session.pause import QUIT_ROW_KEY, RESUME_ROW_KEY, build_pause_menu, key_label

log = logging.getLogger(__name__)

Result = Literal["applied", "refused", "failed", "discarded"]

# How many acknowledgements the record keeps: enough for the page to show the
# last few and to find any command it is still waiting on.
RECENT = 20

# The order the page lists the buttons in: the two that change what the
# session is doing first, then the eye tracker's, the reward, the trial, the
# curriculum, and Quit last, apart.
ORDER = (
    "pause",
    "resume",
    "back",
    "calibrate",
    "reuse_calibration",
    "accept_calibration",
    "validate",
    "drift_correct",
    "manual_reward",
    "skip_trial",
    "promote_stage",
    "demote_stage",
    "hold_stage",
    "quit",
)

# A command accepted while a trial ran, as the engine's command.
TRIAL_COMMANDS = {
    "pause": Command.PAUSE,
    "skip_trial": Command.SKIP_TRIAL,
    "manual_reward": Command.MANUAL_REWARD,
    "quit": Command.QUIT,
    "promote_stage": Command.PROMOTE_STAGE,
    "demote_stage": Command.DEMOTE_STAGE,
    "hold_stage": Command.HOLD_STAGE,
}

# Every engine command as the controls table names it (live_monitor/
# controls.py): the rig's keys are judged by the same table as the page's
# buttons. CALIBRATE is a key the engine refuses during a trial, because the
# table allows "calibrate" only while paused.
KEYBOARD_COMMAND_NAMES: dict[Command, str] = {
    **{command: name for name, command in TRIAL_COMMANDS.items()},
    Command.CALIBRATE: "calibrate",
}

# What the engine does with each, said in the acknowledgement. Said before it
# does it: the engine acts on a command that is alone in its poll on that
# same frame (MergedCommands), so the note is a fact by the time anyone reads
# it.
_TRIAL_NOTES = {
    "pause": "the trial ends here and the session pauses",
    "skip_trial": "the trial is abandoned; its condition is served again",
    "manual_reward": "delivering one reward now",
    "quit": "the session ends; the data so far is saved",
    "promote_stage": "stage change requested; applied between trials",
    "demote_stage": "stage change requested; applied between trials",
    "hold_stage": "stage hold toggled",
}

_TRACKER_COMMANDS = frozenset({"calibrate", "validate", "drift_correct"})

# The statuses whose commands go to the blocking screen that offered them.
OFFERED_STATUSES = frozenset({"waiting", "calibrating"})


@dataclass(frozen=True)
class Offer:
    """What the blocking screen on now answers to: the status it publishes
    under, each command it takes with the note its acknowledgement carries,
    the key each stands for at the rig, what the screen is called, and why
    it offers nothing when it offers nothing."""

    status: str
    commands: Mapping[str, str]
    keys: Mapping[str, str]
    what: str
    none_because: str | None = None


_STAGE_COMMANDS = frozenset({"promote_stage", "demote_stage", "hold_stage"})


class _CommandQueue(Protocol):
    """What this needs of the live monitor: its queue of commands and its
    acknowledgement channel (LiveMonitorController)."""

    def poll_commands(self) -> list[LiveMonitorCommand]: ...

    def publish_commands(self, records: list[dict[str, Any]]) -> None: ...


@dataclass(frozen=True)
class Capabilities:
    """What this session has to act with. A command whose device is missing
    is refused with the reason rather than passed on to do nothing."""

    tracker: bool = False
    reward: bool = False
    training: bool = False
    # Somebody can answer a pause: a keyboard and a rendering display. An
    # unattended run resumes every pause at once (PauseController.handle), so
    # Pause and Calibrate would do nothing a person could act on.
    attended: bool = True


def capability_refusal(name: str, caps: Capabilities) -> str | None:
    """Why this session cannot act on ``name`` whatever its status, or None."""
    if name in _TRACKER_COMMANDS and not caps.tracker:
        return "this session has no eye tracker"
    if name == "manual_reward" and not caps.reward:
        return "this session has no reward line"
    if name in _STAGE_COMMANDS and not caps.training:
        return "this session runs no curriculum"
    if name in ("pause", "calibrate") and not caps.attended:
        return "nobody can answer a pause in this unattended run; it would resume at once"
    return None


def _running_keys() -> dict[str, str]:
    """The key each in-trial command is bound to, as the rig's screen spells
    it, read off the keymap so a rebound key shows its own binding."""
    by_command: dict[Command, list[str]] = {}
    for key, command in DEFAULT_KEYMAP.items():
        label = key_label(key)
        if label not in by_command.setdefault(command, []):
            by_command[command].append(label)
    return {
        name: " or ".join(by_command[command])
        for name, command in TRIAL_COMMANDS.items()
        if command in by_command
    }


def _paused_keys() -> dict[str, str]:
    """The key each pause-menu row answers to, read off the menu itself."""
    menu = build_pause_menu(has_tracker=True, has_reward=True, has_training=True)
    keys = {item.action: item.key for item in menu.now if item.action}
    keys["resume"] = RESUME_ROW_KEY
    keys["quit"] = QUIT_ROW_KEY.split(" or ")[0]
    return keys


class OperatorCommands:
    """The session's end of the live monitor's command queue. See the module
    docstring for what it decides and what callers may rely on."""

    def __init__(self, live_monitor: _CommandQueue, clock: Clock | None = None) -> None:
        self._live_monitor = live_monitor
        self._clock = clock
        self._pending: deque[LiveMonitorCommand] = deque()
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self._emit: Callable[[str, dict[str, Any]], None] | None = None
        self._caps = Capabilities()
        self._keys = {"running": _running_keys(), "paused": _paused_keys()}
        # The blocking screen's offer while one is up (``offer``), else None.
        self._offer: Offer | None = None

    def bind(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], None],
        capabilities: Capabilities,
        clock: Clock,
    ) -> None:
        """Wire the runner's session-event emitter, what the session has and
        its clock. The runner does this as it is built; until then nothing
        can be acknowledged, and taking a command raises."""
        self._emit = emit
        self._caps = capabilities
        self._clock = clock

    # ------------------------------------------------------------------
    # What the session will act on
    # ------------------------------------------------------------------

    def refusal(self, name: str, status: str) -> str | None:
        """Why the session will not act on ``name`` in ``status``, or None."""
        refused = status_refusal(name, status) or capability_refusal(name, self._caps)
        if refused is None and status in OFFERED_STATUSES:
            refused = self._offer_refusal(name, status)
        return refused

    def _offer_refusal(self, name: str, status: str) -> str | None:
        """Why the blocking screen up in ``status`` does not take ``name``."""
        offer = self._offer
        if offer is None or offer.status != status:
            return f"{LABELS[name]} is not taken here right now"
        if name in offer.commands:
            return None
        if not offer.commands and offer.none_because:
            return offer.none_because
        return f"{LABELS[name]} does nothing at {offer.what}"

    def controls(self, status: str) -> dict[str, Any]:
        """The ``controls`` block a state publish carries for ``status``.

        ``shown``: the buttons the page lists, in order — every one this
        session could ever act on, plus the standard ones it cannot, which are
        listed off with their reason (a rig with no tracker still shows
        Calibrate, saying why it is off), the stage keys only when a
        curriculum runs, and a screen's own commands (Back, Reuse
        calibration, Accept calibration) only while that screen offers them.
        ``available``: the ones it acts on now. ``refused``: why each other
        shown one is off. ``keys``: the key at the rig, in the status where it
        works."""
        offered = (
            self._offer.commands if self._offer is not None and self._offer.status == status else {}
        )
        shown = [
            name
            for name in ORDER
            if (name not in _STAGE_COMMANDS or self._caps.training)
            and (name not in CONTEXT_COMMANDS or name in offered)
        ]
        available = [name for name in shown if self.refusal(name, status) is None]
        refused = {
            name: reason for name in shown if (reason := self.refusal(name, status)) is not None
        }
        keys = {}
        for name in shown:
            if name in offered and self._offer is not None and name in self._offer.keys:
                keys[name] = self._offer.keys[name]
                continue
            where = (
                status
                if name in STATUS_COMMANDS.get(status, ()) and status in self._keys
                else ("paused" if name in STATUS_COMMANDS["paused"] else "running")
            )
            key = self._keys[where].get(name)
            if key is not None:
                keys[name] = key
        return {
            "shown": shown,
            "available": available,
            "refused": refused,
            "keys": keys,
            "labels": {name: LABELS[name] for name in shown},
        }

    # ------------------------------------------------------------------
    # A blocking screen's commands
    # ------------------------------------------------------------------

    def offer(self, offer: Offer) -> None:
        """Say what the blocking screen going up now answers to. Set before
        the screen's first publish, so that publish already enables its
        buttons; replaced by the next offer (a procedure run from the
        calibration request offers its own, and the request offers again
        when it is back)."""
        self._offer = offer

    def withdraw(self, note: str) -> None:
        """The blocking screen is down: nothing more is taken for it, and
        whatever was sent to it and is still waiting is discarded, each
        acknowledged with ``note``."""
        offer, self._offer = self._offer, None
        if offer is None:
            return
        self._refill()
        kept: deque[LiveMonitorCommand] = deque()
        while self._pending:
            command = self._pending.popleft()
            if command.status == offer.status:
                self.acknowledge(command, "discarded", note)
            else:
                kept.append(command)
        self._pending = kept

    def take_offered(self, accepted: Collection[str] | None = None) -> list[str]:
        """The commands the blocking screen up now takes, oldest first, each
        acknowledged as applied with its offer's note.

        ``accepted`` narrows it to what the screen can act on at this moment
        (a walk's guide starts on Resume but has no target to step back
        from): a command it offers but cannot take yet stays queued, in
        order, for its next read. A command accepted in another status — sent
        a moment before the screen went up, or after it went down — is
        discarded, and one it does not offer (an offer that changed after the
        click: a reuse no longer offered) refused, each acknowledged. With no
        screen offered (the page was never told of one) nothing is taken and
        the queue is left as it is."""
        offer = self._offer
        if offer is None:
            return []
        self._refill()
        taken: list[str] = []
        kept: deque[LiveMonitorCommand] = deque()
        while self._pending:
            command = self._pending.popleft()
            if command.status != offer.status:
                self.acknowledge(
                    command,
                    "discarded",
                    f"sent while the session was {command.status}; {offer.what} was up "
                    "by the time it arrived",
                )
                continue
            if command.name not in offer.commands:
                reason = (
                    self._offer_refusal(command.name, offer.status)
                    if command.name in KNOWN_COMMANDS
                    else f"unknown command {command.name!r}"
                )
                self.acknowledge(command, "refused", reason or "not offered")
                continue
            if accepted is not None and command.name not in accepted:
                kept.append(command)
                continue
            self.acknowledge(command, "applied", offer.commands[command.name])
            taken.append(command.name)
        self._pending = kept
        return taken

    # ------------------------------------------------------------------
    # Taking commands off the queue
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        self._pending.extend(self._live_monitor.poll_commands())

    def take_for_trial(self) -> Command | None:
        """The next command accepted while a trial ran, as the engine's
        command, acknowledged as applied; None when there is none. Every
        command before it that was accepted in another status is discarded,
        and one this session cannot act on is refused, each acknowledged.

        One per call: MergedCommands asks once per frame, and a second command
        waits for the next frame (see there for why)."""
        self._refill()
        while self._pending:
            command = self._pending.popleft()
            if command.status != "running":
                self.acknowledge(
                    command,
                    "discarded",
                    f"sent while the session was {command.status}; a trial was running "
                    "by the time it arrived",
                )
                continue
            # The status rule first, then what this session has: the server
            # checked the same table, so a command it passed that a trial
            # cannot take (an eye-tracker procedure, which runs only while
            # paused) reaches here only from a server older than the table,
            # and is refused with the same reason, never acted on.
            refusal = (
                status_refusal(command.name, "running")
                if command.name in KNOWN_COMMANDS
                else f"unknown command {command.name!r}"
            ) or capability_refusal(command.name, self._caps)
            if refusal is not None:
                self.acknowledge(command, "refused", refusal)
                continue
            self.acknowledge(command, "applied", _TRIAL_NOTES[command.name])
            return TRIAL_COMMANDS[command.name]
        return None

    def take_for_pause(self) -> list[LiveMonitorCommand]:
        """The commands accepted while paused, oldest first, for the pause
        menu to act on and acknowledge (``acknowledge``). Any accepted while a
        trial ran — clicked as the trial ended — is discarded: the pause is
        not what it was sent to."""
        self._refill()
        taken: list[LiveMonitorCommand] = []
        while self._pending:
            command = self._pending.popleft()
            if command.status == "paused":
                refusal = (
                    status_refusal(command.name, "paused")
                    if command.name in KNOWN_COMMANDS
                    else None  # the menu logs and refuses an unknown action itself
                )
                if refusal is not None:
                    # Passed by a server older than the table (see
                    # take_for_trial): refused, never handed to the menu.
                    self.acknowledge(command, "refused", refusal)
                    continue
                taken.append(command)
            else:
                self.acknowledge(
                    command,
                    "discarded",
                    f"sent while the session was {command.status}; the session had paused "
                    "by the time it arrived",
                )
        return taken

    def discard_pending(self, note: str) -> list[LiveMonitorCommand]:
        """Discard everything waiting, each acknowledged with ``note``, and
        return what was discarded (for the log)."""
        self._refill()
        dropped = list(self._pending)
        self._pending.clear()
        for command in dropped:
            self.acknowledge(command, "discarded", note)
        return dropped

    def discard(self, commands: list[LiveMonitorCommand], note: str) -> None:
        """Acknowledge commands already taken (``take_for_pause``) as
        discarded without acting on them."""
        for command in commands:
            self.acknowledge(command, "discarded", note)

    # ------------------------------------------------------------------
    # Acknowledgement
    # ------------------------------------------------------------------

    def acknowledge(self, command: LiveMonitorCommand, result: Result, note: str) -> None:
        """Record what became of one command: an OPERATOR_COMMAND event, an
        entry in the record the page reads, and that record sent at once."""
        self._record(command.name, command.request_id, "live_monitor", result, note)

    def record_keyboard(self, name: str, request_id: str, result: Result, note: str) -> None:
        """Record a rig-keyboard command the session refused (today: the
        calibrate key during a trial), exactly as a page command's
        acknowledgement: the OPERATOR_COMMAND event with ``source:
        keyboard``, and an entry in the record the page lists under its
        buttons, so the operator watching the page sees why the key did
        nothing. ``request_id`` is the session's own (``keyboard-<n>``): no
        page sent it."""
        self._record(name, request_id, "keyboard", result, note)

    def _record(self, name: str, request_id: str, source: str, result: Result, note: str) -> None:
        if self._emit is None or self._clock is None:
            # The runner binds this as it is built, before any command can be
            # taken; reaching here unbound is a wiring bug, and an
            # acknowledgement that silently went nowhere would leave the page
            # waiting on a command the session took.
            raise RuntimeError("OperatorCommands.acknowledge before bind()")
        self._emit(
            "OPERATOR_COMMAND",
            {
                "name": name,
                "request_id": request_id,
                "source": source,
                "result": result,
                "note": note,
            },
        )
        self._recent.append(
            {
                "request_id": request_id,
                "name": name,
                "source": source,
                "result": result,
                "note": note,
                "t": self._clock.now(),
            }
        )
        log.info("%s command %s: %s (%s)", source.replace("_", " "), name, result, note)
        self._live_monitor.publish_commands(self.recent())

    def recent(self) -> list[dict[str, Any]]:
        """The newest acknowledgements, oldest first (a copy)."""
        return [dict(record) for record in self._recent]
