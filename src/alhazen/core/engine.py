"""TrialEngine: the single per-frame loop that drives one trial end to end.

Once per displayed frame: poll experimenter commands, run health checks
(a device that stopped aborts the trial — unless it stopped during the
closing phase, after the measurement, when it is only flagged), snapshot
inputs into the context, let the current phase draw and decide, draw
the rig's overlay, flip, stamp the flip on the session clock, feed frame QA
(which, at the trial's end, may recycle a trial whose display dropped too many
frames), then emit whatever events that frame queued — stamped with the flip's own
time, because a visual event's timestamp must correspond to the frame that
actually showed it, not to the Python call that requested it. Those
timestamps are what let analysis line up behavior with device recordings
afterwards. A phase's mid-trial reward requests are handed to the dispenser
at that same moment, so their REWARD events carry the same flip.

Both system faults the engine can see — a failed health check and a frame-QA
recycle — are written onto the row as its ``fault`` (core/trial.py NO_FAULT).
What a fault then costs, pays and counts is the runner's business.

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
from typing import Any, Protocol

from alhazen.core.clock import Clock
from alhazen.core.commands import Command, CommandSource
from alhazen.core.events import Event, EventBus, EventSchema
from alhazen.core.trial import (
    ABORTED,
    DROPPED_FRAMES,
    FAULT_DROPPED_FRAMES,
    NO_FAULT,
    PAUSED,
    InputFrame,
    Outcome,
    PhaseAction,
    RewardCompletion,
    RewardRequest,
    TrialContext,
    lost_to_fault,
)
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
    # The outcome the subject's own response ended the trial as, when frame
    # QA then recycled it into DROPPED_FRAMES; None on every other trial. The
    # Outcome object rather than only its name (the record keeps the name as
    # `outcome_before_frame_qa`): the runner needs its `completed` flag to
    # decide NO_REWARD, and it has no outcome set to look a name up in.
    outcome_before_frame_qa: Outcome | None = None

    @property
    def response_outcome(self) -> Outcome:
        """What the subject's response earned, whatever the display did.

        Two questions share a trial and can get different answers. Whether
        the measurement is kept — and so whether the condition is served
        again — follows ``outcome``, which frame QA may have replaced with
        DROPPED_FRAMES. What the subject was told and what they are paid
        follow this: a display fault is not something the subject did, and
        must never cost them a reward they earned. Identical to ``outcome``
        on every trial frame QA left alone, including PAUSED and ABORTED.
        """
        if self.outcome_before_frame_qa is not None:
            return self.outcome_before_frame_qa
        return self.outcome

    @property
    def lost_to_fault(self) -> str | None:
        """The system fault this trial's measurement was lost to —
        ``"dropped_frames"`` or ``"tracker_stopped"`` — or None.

        Not always what the row's ``fault`` column says: a tracker that
        stopped during the closing phase is flagged there, but the trial kept
        its outcome, so nothing was lost and this is None. Derived from the
        outcome and the record by ``core.trial.lost_to_fault`` rather than
        stored, so the session and an analysis reading trials.csv apply one
        rule.
        """
        return lost_to_fault(self.outcome.name, self.record)


class RewardRequestSink(Protocol):
    """Where the engine hands mid-trial reward requests, and hears back how
    they ended.

    The engine sees only this, never a device — the same narrow-hook rule as
    ``on_manual_reward``. The session's implementation is
    ``devices.reward.QueuedReward``, which delivers on a worker thread, so
    nothing the engine calls on it inside a trial waits for the pump.
    """

    def submit(self, request: RewardRequest) -> int:
        """Queue a delivery and return at once, with how many deliveries were
        already ahead of it (running or queued)."""
        ...

    def completed(self) -> list[RewardCompletion]:
        """Every completion reported since the last call, oldest first.
        Never blocks."""
        ...

    def wait_idle(self) -> None:
        """Block until nothing is running or queued."""
        ...


def _null_inputs() -> InputFrame:
    return InputFrame()


def _is_interrupted(outcome: Outcome | None) -> bool:
    """Did the trial stop rather than end?

    PAUSED and ABORTED are not trial results — one is an experimenter
    stopping the session, the other the experimenter's skip or a device that
    stopped mid-trial — so nothing that closes a trial out runs on them.
    Showing a subject a red fixation point because somebody pressed P would
    be telling them they failed a trial they were still in the middle of.
    """
    return outcome is not None and outcome.name in ("PAUSED", "ABORTED")


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
        reward_requests: RewardRequestSink | None = None,
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
        # returns an abort reason string, or None when healthy. A check that
        # fails is a system fault — a device stopped, which is never the
        # subject's doing — so its reason is also the row's `fault`; and
        # during the closing phase it flags the row without ending the trial
        # (_run_phase).
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
        # Where ctx.request_reward's requests go. None unless the task
        # declared mid_trial_reward (the builder refuses such a task on a rig
        # with no dispenser); with None, a request is a loud error at the call.
        self._reward_requests = reward_requests
        self._frame_index = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_trial(self, ctx: TrialContext, phases: list[Any]) -> TrialResult:
        self._frame_index = 0
        # "none" from the start, overwritten only when a fault happens: every
        # row carries the column, and a clean trial says so with a value, not
        # an empty cell (core/trial.py NO_FAULT).
        ctx.record["fault"] = NO_FAULT
        if self._frame_monitor is not None:
            self._frame_monitor.start_trial(ctx.trial_index)
            if self._frame_monitor.marks_trials:
                # Zero from the start, not created on the first drop: a clean
                # trial must write 0, because an empty cell reads back as
                # NaN, and NaN is what made a column mean overstate drops by
                # a third and `astype(int)` raise on the rig's own data.
                ctx.record["n_dropped_frames"] = 0
        ctx.accepts_reward_requests = self._reward_requests is not None
        if self._reward_requests is not None:
            # Zero from the start, like n_dropped_frames and for the same
            # reason: a trial that asked for no drops must write 0, not leave
            # an empty cell that reads back as NaN.
            ctx.record["n_mid_trial_rewards"] = 0
            ctx.record["n_mid_trial_reward_failures"] = 0
            ctx.record["n_mid_trial_rewards_cancelled"] = 0

        # A phase that declares it must be last — trial feedback, which must
        # never be on screen while something is still being measured — is
        # refused anywhere else, before a frame is drawn. A programming error
        # in the task, met while writing it rather than with a subject in
        # the chair and a coloured fixation point over the measurement.
        for index, phase in enumerate(phases):
            if getattr(phase, "must_be_last", False) and index != len(phases) - 1:
                raise RuntimeError(
                    f"phase {getattr(phase, 'name', phase)!r} must be the trial's last phase, "
                    f"but {len(phases) - 1 - index} phase(s) follow it. Feedback is shown "
                    f"only after everything has been measured."
                )

        # TRIAL_START is emitted immediately — not on a flip — because it is
        # not a visual event: nothing has been drawn yet, and downstream
        # alignment needs a trial-start mark that precedes every other event
        # in the trial without exception.
        self._emit(ctx, "TRIAL_START", dict(ctx.params))

        # A phase that must be last is the trial's CLOSING phase, and a
        # closing phase runs whatever the trial ended as. Held out of the
        # loop below for that reason: the loop stops at the first phase that
        # returns an Outcome, so a trial that ended early — a fixation break,
        # a saccade that never came — would otherwise never reach it.
        #
        # That is exactly the trial a subject most needs to hear about, and
        # it was unreachable: feedback fired on every completed trial and on
        # none of the failures. There is no way for a task to work around it
        # either, because the only way to keep a procedural phase from ending
        # the trial is to have it ADVANCE, which lets a broken fixation fall
        # through into the phase that measures the response.
        closing = phases[-1] if phases and getattr(phases[-1], "must_be_last", False) else None
        body = phases[:-1] if closing is not None else phases

        outcome: Outcome | None = None
        for phase in body:
            # QuitRequested propagates straight out (its message/cleanup
            # happened at the raise site); any other exception propagates
            # too — a bug or hardware fault mid-trial must surface, not be
            # masked by cleanup that makes the trial look normal.
            outcome = self._run_phase(phase, ctx)
            if outcome is not None:
                break

        if closing is not None and not _is_interrupted(outcome):
            # What the trial ended as, readable by the closing phase; None
            # when the body ran to its end and the closing phase is the one
            # that decides.
            #
            # Always the subject's own outcome, never frame QA's: the closing
            # phase runs before the frame-QA verdict below, on purpose.
            # Feedback tells the subject what THEY did, and a display that
            # dropped frames is not something they did — a correct trial the
            # display then recycles is still shown as a success, and the
            # runner pays it the same way (TrialResult.response_outcome).
            # The verdict cannot come first anyway: the closing phase's own
            # frames are part of the trial frame QA judges.
            ctx.outcome = outcome
            # closing=True: a device that stops during it is flagged, not
            # allowed to end the trial — see _run_phase.
            closing_outcome = self._run_phase(closing, ctx, closing=True)
            # A closing phase decides the outcome only when nothing else
            # has. Feedback is shown for a fixation break; it does not turn
            # one into a completed trial.
            if outcome is None:
                outcome = closing_outcome
        if outcome is None:
            # ADVANCE-ing off the end of the phase list is a programming
            # error in the task, not a runtime trial outcome.
            raise RuntimeError("the last phase must end the trial with an Outcome, not ADVANCE")

        # Blank the display before finishing: without this flip, the last
        # drawn frame would stay on screen through the ITI and the next
        # trial's setup — while the record claims the trial ended.
        self._display.flip()

        before_frame_qa: Outcome | None = None
        if self._frame_monitor is not None:
            # The monitor is told whether the trial completed, because that
            # decides whether a recycle is even on the table — and the
            # monitor counts consecutive recycles, so it cannot be left to
            # guess. Asking it for a verdict the engine then ignores is how
            # a run of fixation breaks used to trip the "N trials in a row
            # recycled" abort on a display that was fine.
            frames = self._frame_monitor.end_trial(completed=outcome.completed)
            if frames.recycle:
                # The trial ran to its end, but the display did not show what
                # the config describes. Its measurement is discarded the way a
                # fixation break's is — a non-completed outcome, which the
                # scheduler re-serves — and what it would have been is kept
                # on the row. Only a COMPLETED outcome can get here: one that
                # was already non-completed is already being re-served, and
                # PAUSED in particular drives the runner's pause flow, so the
                # monitor returns no verdict for either.
                #
                # The verdict governs data quality only. The Outcome it
                # replaces travels on the result as well as on the row, so
                # the runner can still pay what the response earned.
                before_frame_qa = outcome
                ctx.record["outcome_before_frame_qa"] = outcome.name
                ctx.record["frame_qa_reason"] = frames.reason
                # A display fault, not the subject's, and the one the trial
                # is served again for — so it is what the row's single fault
                # column names. It replaces a tracker stop the closing phase
                # may have flagged on this trial: that one cost nothing, and
                # was logged at WARNING when it happened.
                ctx.record["fault"] = FAULT_DROPPED_FRAMES
                outcome = DROPPED_FRAMES

        self._finalize(ctx, outcome)
        return TrialResult(
            outcome=outcome, record=ctx.record, outcome_before_frame_qa=before_frame_qa
        )

    # ------------------------------------------------------------------
    # Per-phase frame loop
    # ------------------------------------------------------------------

    def _run_phase(self, phase: Any, ctx: TrialContext, *, closing: bool = False) -> Outcome | None:
        """Run one phase frame by frame until it ADVANCEs or ends the trial.

        ``closing`` marks the trial's closing phase (the one declaring
        ``must_be_last``), the only place a failed health check does not end
        the trial.
        """
        phase.on_enter(ctx)
        # dt reference resets per phase so a phase's first dt means "since
        # this phase started", not whatever the previous phase's last frame
        # happened to take.
        last_t = ctx.clock.now()
        # Set once a health check has failed during the closing phase. The
        # fault is on the row by then, and asking again every frame would
        # only find the same device stopped.
        fault_flagged = False
        while True:
            outcome = self._handle_commands(ctx)  # may raise QuitRequested
            if outcome is not None:
                return outcome

            reason = None if fault_flagged else self._failed_health_check()
            if reason is not None:
                if not closing:
                    # The measurement is still being made, and it cannot be
                    # made without the device, so the trial is aborted and
                    # its condition served again. `abort_reason` and `fault`
                    # carry the same reason: that pairing is how
                    # core.trial.lost_to_fault tells this abort from the
                    # experimenter's skip, which is ABORTED too.
                    ctx.record["abort_reason"] = reason
                    ctx.record["fault"] = reason
                    return ABORTED
                self._flag_closing_phase_fault(ctx, phase, reason)
                fault_flagged = True

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
            # This frame's reward requests go to the dispenser now, stamped
            # with the flip that just followed them; then whatever the
            # dispenser finished since the last frame is reported. Neither
            # waits for the pump.
            self._hand_over_reward_requests(ctx, frame=self._frame_index - 1)
            self._report_reward_completions(ctx)

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
    # Health checks
    # ------------------------------------------------------------------

    def _failed_health_check(self) -> str | None:
        """The reason of the first health check that fails this frame, or
        None when every device reports itself healthy."""
        for check in self._health_checks:
            reason = check()
            if reason is not None:
                return reason
        return None

    def _flag_closing_phase_fault(self, ctx: TrialContext, phase: Any, reason: str) -> None:
        """A device failed its health check during the closing phase: flag the
        row, say so, and let the phase run to its end.

        Everything was measured before the closing phase began — that is what
        ``must_be_last`` promises (feedback is never on screen while
        something is being measured) — so a tracker that stops now has cost
        the trial only the eye data of its feedback. Aborting, as every other
        phase does, did real damage here: it discarded a finished measurement
        (a closing phase that decides the outcome, as after a ``LandingCheck``
        that ADVANCEs, ended ABORTED and was served again), cut the subject's
        feedback off before it was drawn, and wrote an ``abort_reason`` on a
        trial that was not aborted. The trial keeps the outcome its own
        phases give it; the row's ``fault`` is the only trace, beside this
        line.
        """
        ctx.record["fault"] = reason
        log.warning(
            "trial %d: a device health check failed (%s) during the closing phase %r, after "
            "the measurement: the trial is not aborted — its outcome stands and the phase "
            "runs to its end — and the row is flagged fault=%s",
            ctx.trial_index,
            reason,
            getattr(phase, "name", phase),
            reason,
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
                # So this frame waits for the pump. For a task with mid-trial
                # reward the hook overrides the queue: it cancels every drop
                # still waiting and is delivered once, after the train
                # already on the valve — so the wait is at most that train
                # plus its own (devices/reward.py, QueuedReward.deliver_manual).
                if self._on_manual_reward is not None:
                    self._on_manual_reward()
                # Those cancellations, and a drop that finished on the valve
                # while the key waited, are reported now, before the manual
                # REWARD: events.csv then reads in the order things happened
                # at the valve — the REWARD_CANCELLED events, that drop's
                # REWARD_DELIVERED, then the manual REWARD that caused them.
                # A no-op without mid-trial reward.
                self._report_reward_completions(ctx)
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
    # Mid-trial reward
    # ------------------------------------------------------------------

    def _hand_over_reward_requests(self, ctx: TrialContext, frame: int) -> None:
        """Submit this frame's requests and emit a REWARD for each.

        Runs right after the flip, so each REWARD is stamped with the flip
        that followed the request — the frame the drop was commanded on,
        which is what events.csv and the tracker's messages must say, since
        an analysis masks vergence and pupil transients around it. Submitted
        before the event is emitted (the manual key's hardware-then-event
        order), but submit only queues: the pump runs on the dispenser's own
        thread, and its end is reported later as REWARD_DELIVERED or
        REWARD_FAILED — or as REWARD_CANCELLED, when a manual reward overrode
        the queue before it reached the valve.
        """
        if not ctx.pending_reward_requests:
            return
        # request_reward only queues when accepts_reward_requests is True,
        # and run_trial sets that only when a sink is wired.
        assert self._reward_requests is not None
        queued, ctx.pending_reward_requests = ctx.pending_reward_requests, []
        for request in queued:
            stamped = RewardRequest(pulses=request.pulses, reason=request.reason, frame=frame)
            ahead = self._reward_requests.submit(stamped)
            payload: dict[str, Any] = {"manual": False, **stamped.payload()}
            if ahead:
                # Only when something is ahead of it: a rig whose pulse train
                # is longer than the task's drop interval shows here that its
                # drops are delivered late, and behind how many deliveries.
                payload["queued_behind"] = ahead
            self._emit(ctx, "REWARD", payload)

    def _report_reward_completions(self, ctx: TrialContext) -> None:
        """Emit an event for every mid-trial drop that has ended — delivered,
        failed or cancelled — and count it on the trial's record.

        Called on the session thread only. The dispenser's worker thread never
        touches the bus or the record — it leaves completions in a queue that
        this drains. The events are stamped when drained, within a frame of
        the pump finishing: they are not visual events, and the REWARD they
        complete already carries the frame the drop was commanded on.

        A failure does not stop the trial. The measurement is still being
        made and a pump fault is no reason to discard it; the runner hands
        the failure to the pause flow once the trial is over, exactly as it
        does an end-of-trial failure. A cancellation is not a failure, and
        does not take that pause.
        """
        if self._reward_requests is None:
            return
        for done in self._reward_requests.completed():
            if done.cancelled_by is not None:
                # Commanded — its REWARD is already in the record — and never
                # delivered: a manual reward overrode the queue before it
                # reached the valve. Its own end event, never REWARD_FAILED:
                # the pump did not fail, and a REWARD_FAILED would send the
                # session to the pump-failure pause. `rewarded` is left alone,
                # since no delivery of it was attempted; the manual reward's
                # own REWARD says what the subject got instead.
                ctx.record["n_mid_trial_rewards_cancelled"] = (
                    ctx.record.get("n_mid_trial_rewards_cancelled", 0) + 1
                )
                self._emit(
                    ctx,
                    "REWARD_CANCELLED",
                    {**done.request.payload(), "cancelled_by": done.cancelled_by},
                )
            elif done.error is None:
                ctx.record["n_mid_trial_rewards"] = ctx.record.get("n_mid_trial_rewards", 0) + 1
                # Any pulse delivered this trial makes it a rewarded trial.
                ctx.record["rewarded"] = True
                self._emit(ctx, "REWARD_DELIVERED", done.request.payload())
            else:
                ctx.record["n_mid_trial_reward_failures"] = (
                    ctx.record.get("n_mid_trial_reward_failures", 0) + 1
                )
                # False only if nothing else paid: a drop that did arrive
                # earlier in the trial still makes it a rewarded trial.
                ctx.record.setdefault("rewarded", False)
                log.error(
                    "mid-trial reward %r failed on trial %d: %s",
                    done.request.reason,
                    ctx.trial_index,
                    done.error,
                )
                self._emit(ctx, "REWARD_FAILED", {**done.request.payload(), "error": done.error})

    def settle_rewards(self, ctx: TrialContext) -> None:
        """Wait for every mid-trial delivery to finish, and report them all.

        The runner calls this between trials — after ``run_trial`` returns and
        before it pays the outcome's reward — so the end-of-trial pulse train
        never overlaps a mid-trial one on the same valve, and every drop of
        the trial is counted on its record before the row is written. It is
        also called at teardown, for a trial that a quit or a fault cut
        short. A no-op for a session with no mid-trial reward.
        """
        if self._reward_requests is None:
            return
        if ctx.pending_reward_requests:
            # Queued in a phase's on_enter, and then the trial ended before
            # the next flip (a skip, a pause, a failed health check). With no
            # flip to stamp them they were never commanded — said in the log,
            # never dropped without a word.
            log.warning(
                "trial %d ended before the flip that would have commanded %d mid-trial "
                "reward request(s) (%s); they were not delivered",
                ctx.trial_index,
                len(ctx.pending_reward_requests),
                ", ".join(request.reason for request in ctx.pending_reward_requests),
            )
            ctx.pending_reward_requests = []
        self._reward_requests.wait_idle()
        self._report_reward_completions(ctx)

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
