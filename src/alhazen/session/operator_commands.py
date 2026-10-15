"""OperatorCommands: the live monitor's buttons, as the session takes them.

The page posts a command; the server checks it against the status it holds
(live_monitor/controls.py) and queues it, stamped with that status
(live_monitor/runtime.py). From there this module decides everything:

- **where a command goes.** One accepted while a trial ran goes to the engine,
  through the same ``CommandSource`` the keyboard feeds (core/commands.py
  ``MergedCommands``), one per frame. One accepted while paused goes to the
  pause menu (session/pause_control.py). A command is acted on only in the
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

Interface: ``bind``, ``take_for_trial``, ``take_for_pause``,
``discard_pending``, ``acknowledge``, ``controls``, ``recent``.

Callers must not rely on the wording of a note, nor on when within a frame
the queue is read; only on each command being acted on at most once, in the
status it was accepted in, and acknowledged exactly once.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from alhazen.core.clock import Clock
from alhazen.core.commands import DEFAULT_KEYMAP, Command
from alhazen.live_monitor.controls import LABELS, STATUS_COMMANDS, status_refusal
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
    "calibrate",
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
    "calibrate": Command.CALIBRATE,
    "skip_trial": Command.SKIP_TRIAL,
    "manual_reward": Command.MANUAL_REWARD,
    "quit": Command.QUIT,
    "promote_stage": Command.PROMOTE_STAGE,
    "demote_stage": Command.DEMOTE_STAGE,
    "hold_stage": Command.HOLD_STAGE,
}

# What the engine does with each, said in the acknowledgement. Said before it
# does it: the engine acts on a command that is alone in its poll on that
# same frame (MergedCommands), so the note is a fact by the time anyone reads
# it.
_TRIAL_NOTES = {
    "pause": "the trial ends here and the session pauses",
    "calibrate": "the trial ends here; the session pauses and calibrates",
    "skip_trial": "the trial is abandoned; its condition is served again",
    "manual_reward": "one reward delivered now",
    "quit": "the session ends; the data so far is saved",
    "promote_stage": "stage change requested; applied between trials",
    "demote_stage": "stage change requested; applied between trials",
    "hold_stage": "stage hold toggled",
}

_TRACKER_COMMANDS = frozenset({"calibrate", "validate", "drift_correct"})
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
        return status_refusal(name, status) or capability_refusal(name, self._caps)

    def controls(self, status: str) -> dict[str, Any]:
        """The ``controls`` block a state publish carries for ``status``.

        ``shown``: the buttons the page lists, in order — every one this
        session could ever act on, plus the standard ones it cannot, which are
        listed off with their reason (a rig with no tracker still shows
        Calibrate, saying why it is off), and the stage keys only when a
        curriculum runs. ``available``: the ones it acts on now. ``refused``:
        why each other shown one is off. ``keys``: the key at the rig, in the
        status where it works."""
        shown = [name for name in ORDER if name not in _STAGE_COMMANDS or self._caps.training]
        available = [name for name in shown if self.refusal(name, status) is None]
        refused = {
            name: reason for name in shown if (reason := self.refusal(name, status)) is not None
        }
        keys = {}
        for name in shown:
            where = (
                status
                if name in STATUS_COMMANDS.get(status, ())
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
            refusal = capability_refusal(command.name, self._caps)
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
        if self._emit is None or self._clock is None:
            # The runner binds this as it is built, before any command can be
            # taken; reaching here unbound is a wiring bug, and an
            # acknowledgement that silently went nowhere would leave the page
            # waiting on a command the session took.
            raise RuntimeError("OperatorCommands.acknowledge before bind()")
        self._emit(
            "OPERATOR_COMMAND",
            {
                "name": command.name,
                "request_id": command.request_id,
                "source": "live_monitor",
                "result": result,
                "note": note,
            },
        )
        self._recent.append(
            {
                "request_id": command.request_id,
                "name": command.name,
                "result": result,
                "note": note,
                "t": self._clock.now(),
            }
        )
        log.info("live monitor command %s: %s (%s)", command.name, result, note)
        self._live_monitor.publish_commands(self.recent())

    def recent(self) -> list[dict[str, Any]]:
        """The newest acknowledgements, oldest first (a copy)."""
        return [dict(record) for record in self._recent]
