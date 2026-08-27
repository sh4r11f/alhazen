"""TrialEngine: the single per-frame loop that drives one trial end to end.

Once per displayed frame: poll experimenter commands, run health checks,
snapshot inputs into the context, let the current phase draw and decide, draw
the rig's overlay, flip, stamp the flip on the session clock, feed frame QA,
then emit whatever events that frame queued — stamped with the flip's own
time, because a visual event's timestamp must correspond to the frame that
actually showed it, not to the Python call that requested it. Those
timestamps are what let analysis line up behavior with device recordings
afterwards.

The engine is the only code that touches the display, the command source,
and the bus. Phases stay dumb (core/trial.py). Device-specific side effects
(tracker messages, reward hardware) attach through the bus and the injected
hooks, so this module needs no devices to run — which is why every test and
every simulated session can drive it as-is.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.core.events import Event, EventBus, EventSchema
from alhazen.core.trial import ABORTED, PAUSED, InputFrame, Outcome, PhaseAction, TrialContext
from alhazen.display.backend import DisplayBackend
from alhazen.display.frames import FrameMonitor

log = logging.getLogger(__name__)


class QuitRequested(Exception):
    """Raised mid-trial on the QUIT command. The trial's cleanup has already
    run by the time this propagates; the session runner catches it to stop
    serving trials and fall through to teardown (data is saved)."""


@dataclass(frozen=True)
class TrialResult:
    outcome: Outcome
    record: dict[str, Any]


def _null_inputs() -> InputFrame:
    return InputFrame()


class TrialEngine:
    def __init__(
        self,
        display: DisplayBackend,
        clock: Clock,
        bus: EventBus,
        schema: EventSchema,
        commands: CommandSource,
        frame_monitor: FrameMonitor | None = None,
        input_provider: Callable[[], InputFrame] | None = None,
        health_checks: tuple[Callable[[], str | None], ...] = (),
        on_manual_reward: Callable[[], None] | None = None,
        manual_reward_payload: dict[str, Any] | None = None,
        overlay: Callable[[TrialContext], None] | None = None,
        on_session_command: Callable[[Command], None] | None = None,
        on_frame_input: Callable[[int, int, float, InputFrame], None] | None = None,
    ) -> None:
        self._display = display
        self._clock = clock
        self._bus = bus
        self._schema = schema
        self._commands = commands
        self._frame_monitor = frame_monitor
        self._input_provider = input_provider or _null_inputs
        # Health checks run every frame, not once at trial start: a trial
        # that believes a device is still recording when it is not would
        # silently produce data with holes and no record of why. A check
        # returns an abort reason string, or None when healthy.
        self._health_checks = tuple(health_checks)
        self._on_manual_reward = on_manual_reward
        self._manual_reward_payload = dict(manual_reward_payload or {})
        # A rig-owned drawable the task knows nothing about (today: the
        # photodiode patch). It draws after the phase and before the flip, so
        # it can see what this frame queued — which is what lets the patch
        # mark the exact flip an event's timestamp refers to.
        self._overlay = overlay
        # Commands the engine has no opinion about — today, the training
        # stage keys. Passed straight through so the engine stays ignorant
        # of what a curriculum is; the runner decides what they mean and
        # when they take effect.
        self._on_session_command = on_session_command
        self._on_frame_input = on_frame_input
        self._frame_index = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_trial(self, ctx: TrialContext, phases: list[Any]) -> TrialResult:
        self._frame_index = 0
        if self._frame_monitor is not None:
            self._frame_monitor.start_trial(ctx.trial_index)

        # TRIAL_START is emitted immediately — not on a flip — because it is
        # not a visual event: nothing has been drawn yet, and downstream
        # alignment needs a trial-start mark that precedes every other event
        # in the trial without exception.
        self._emit(ctx, "TRIAL_START", dict(ctx.params))

        outcome: Outcome | None = None
        for phase in phases:
            # QuitRequested propagates straight out (its message/cleanup
            # happened at the raise site); any other exception propagates
            # too — a bug or hardware fault mid-trial must surface, not be
            # masked by cleanup that makes the trial look normal.
            outcome = self._run_phase(phase, ctx)
            if outcome is not None:
                break
        if outcome is None:
            # ADVANCE-ing off the end of the phase list is a programming
            # error in the task, not a runtime trial outcome.
            raise RuntimeError("the last phase must end the trial with an Outcome, not ADVANCE")

        # Blank the display before finishing: without this flip, the last
        # drawn frame would stay on screen through the ITI and the next
        # trial's setup — while the record claims the trial ended.
        self._display.flip()

        self._finalize(ctx, outcome)
        return TrialResult(outcome=outcome, record=ctx.record)

    # ------------------------------------------------------------------
    # Per-phase frame loop
    # ------------------------------------------------------------------

    def _run_phase(self, phase: Any, ctx: TrialContext) -> Outcome | None:
        phase.on_enter(ctx)
        # dt reference resets per phase so a phase's first dt means "since
        # this phase started", not whatever the previous phase's last frame
        # happened to take.
        last_t = ctx.clock.now()
        while True:
            outcome = self._handle_commands(ctx)  # may raise QuitRequested
            if outcome is not None:
                return outcome

            for check in self._health_checks:
                reason = check()
                if reason is not None:
                    ctx.record["abort_reason"] = reason
                    return ABORTED

            ctx.inputs = self._input_provider()

            step = phase.on_frame(ctx)

            if self._overlay is not None:
                self._overlay(ctx)

            # The flip is the only moment the photons change. Nothing this
            # frame queued is real until this call returns.
            self._display.flip()
            now = ctx.clock.now()
            # dt = how long the just-shown frame actually took, available to
            # the NEXT on_frame call to advance motion by the right amount.
            # Floored as a divide-by-zero guard against a zero-duration flip.
            ctx.dt, last_t = max(now - last_t, 1e-4), now

            if self._frame_monitor is not None:
                dropped = self._frame_monitor.note_flip(now)
                if dropped and self._frame_monitor.marks_trials:
                    ctx.record["n_dropped_frames"] = ctx.record.get("n_dropped_frames", 0) + 1

            if self._on_frame_input is not None:
                self._on_frame_input(ctx.trial_index, self._frame_index, now, ctx.inputs)
            self._frame_index += 1

            # Only after the flip do queued events emit, stamped now — the
            # true photon-onset time, the one that must line up with sync
            # pulses in device recordings.
            self._flush_flip_events(ctx)

            if step == PhaseAction.CONTINUE:
                continue
            if step == PhaseAction.ADVANCE:
                return None
            if isinstance(step, Outcome):
                return step
            raise TypeError(
                f"phase {getattr(phase, 'name', phase)!r} returned {step!r}; expected "
                f"PhaseAction.CONTINUE, PhaseAction.ADVANCE, or an Outcome"
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _handle_commands(self, ctx: TrialContext) -> Outcome | None:
        """Act on every command this frame produced. The first trial-ending
        command wins; MANUAL_REWARD doesn't end anything and falls through."""
        for cmd in self._commands.poll():
            if cmd is Command.SKIP_TRIAL:
                ctx.record["abort_reason"] = "skipped_by_user"
                return ABORTED
            if cmd is Command.PAUSE:
                self._emit(ctx, "PAUSED", {})
                return PAUSED
            if cmd is Command.CALIBRATE:
                # A calibration request is a pause the runner resolves by
                # actually recalibrating before reopening the menu.
                ctx.record["pause_action"] = "calibrate"
                self._emit(ctx, "PAUSED", {"action": "calibrate"})
                return PAUSED
            if cmd is Command.QUIT:
                raise QuitRequested()
            if cmd is Command.MANUAL_REWARD:
                # Emitted immediately — an experimenter action has no flip to
                # time-lock to. The hook reaches the rig's reward dispenser
                # (session/builder.py); the event is the permanent record.
                # Hardware first, then the event: an event claiming a reward
                # the pump never delivered is the one ordering that lies.
                if self._on_manual_reward is not None:
                    self._on_manual_reward()
                self._emit(ctx, "REWARD", {"manual": True, **self._manual_reward_payload})
            elif self._on_session_command is not None:
                # Not this loop's business. Handed on rather than ignored: a
                # key an experimenter pressed and nothing acted on is
                # indistinguishable, from the rig, from a key that did not
                # register.
                self._on_session_command(cmd)
        return None

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit(self, ctx: TrialContext, name: str, payload: dict) -> None:
        """The only place an Event is constructed in the engine: validate the
        name against the schema, stamp the clock, mirror the time into the
        trial record (``t_<name>``), publish."""
        self._schema.validate(name)
        t = self._clock.now()
        ctx.record[f"t_{name.lower()}"] = t
        self._bus.emit(Event(name=name, t=t, trial_index=ctx.trial_index, payload=payload))

    def _flush_flip_events(self, ctx: TrialContext) -> None:
        # Snapshot-then-clear so a subscriber that re-entered trial code
        # could never observe a partially-drained queue.
        queued, ctx.pending_flip_events = ctx.pending_flip_events, []
        for name, payload in queued:
            self._emit(ctx, name, payload)

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def _finalize(self, ctx: TrialContext, outcome: Outcome) -> None:
        ctx.record["outcome"] = outcome.name
        # On the ROW, not only in the TRIAL_END payload. Incomplete outcomes
        # write rows too, and without this column every reader downstream has
        # to guess completion from the outcome *name* — which it cannot do,
        # because outcome names belong to the experiment.
        ctx.record["completed"] = outcome.completed
        if outcome.success is not None:
            ctx.record["success"] = outcome.success
        self._emit(
            ctx,
            "TRIAL_END",
            {"outcome": outcome.name, "completed": outcome.completed},
        )
