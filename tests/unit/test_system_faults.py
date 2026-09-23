"""System faults on the record: a trial the rig failed, flagged in its row.

Two failures are the rig's, never the subject's, and the engine sees both:

- **dropped frames** — frame QA's ``recycle_trial`` turned the trial into
  ``DROPPED_FRAMES``;
- **the eye tracker stopped recording** — the tracker health check fired.

Every row names the one that hit its trial in its ``fault`` column
(``"none"`` when nothing did), and ``lost_to_fault`` says, from the row
alone, whether the fault cost the trial its measurement. The experimenter's
skip is not a fault.

And one edge case, decided here: a tracker that stops during the closing
phase — after the measurement, while feedback is on screen — does not abort
the trial. The row is flagged, the trial keeps its outcome, and it is paid,
scheduled and counted by that outcome.

Everything runs on ``alhazen.testing`` fakes and simulated time; nothing
sleeps. A test phase stops the scripted tracker on the frame the test picks.
"""

from __future__ import annotations

import csv
import logging

from alhazen.config.models import FrameQAConfig, RewardPulses
from alhazen.core.commands import Command
from alhazen.core.trial import (
    FAULT_DROPPED_FRAMES,
    FAULT_TRACKER_STOPPED,
    NO_FAULT,
    TRIAL_RECORD_COLUMNS,
    Outcome,
    PhaseAction,
    lost_to_fault,
)
from alhazen.devices.eyetracker import ScriptedTracker
from alhazen.devices.reward import SimulatedReward
from alhazen.display.frames import FrameMonitor
from alhazen.session.builder import make_tracker_health_check
from alhazen.stimuli.base import NullStimulus
from alhazen.task.phases import TrialFeedback
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import FakeClock, ScriptedCommands
from support import FRAME_S, EngineHarness, RunForFrames, SessionHarness

CORRECT = Outcome("CORRECT", completed=True, success=True)
FIX_BREAK = Outcome("FIX_BREAK", completed=False)

# What a correct response earns.
PAID = RewardPulses(n_pulses=2, pulse_ms=100, inter_pulse_ms=50)
POLICY = RewardPolicy(by_outcome={"CORRECT": PAID})


# ---------------------------------------------------------------------------
# Test phases: the rig failing at a moment the test picks. Test-only — a real
# phase never touches a device.
# ---------------------------------------------------------------------------


class StopsTheTracker(RunForFrames):
    """A measuring phase during which the eye tracker stops recording.

    It stops the tracker on its ``on_frame``-th frame (0 = the first); the
    engine's health check sees that at the start of the next frame and aborts
    the trial — before this phase would have returned ``then``, which is how
    a real dropout ends a trial the subject had not finished.
    """

    name = "stops_the_tracker"

    def __init__(self, n_frames, then, tracker: ScriptedTracker, on_frame: int = 1) -> None:
        super().__init__(n_frames, then)
        self._tracker = tracker
        self._on_frame = on_frame
        self._frame = 0

    def on_frame(self, ctx):
        if self._frame == self._on_frame:
            self._tracker.stop_recording()
        self._frame += 1
        return super().on_frame(ctx)


class Overruns(RunForFrames):
    """A measuring phase whose every frame takes two frame periods: half its
    frames dropped, far past the 10% budget the sessions here give frame QA,
    so a completed trial is recycled into DROPPED_FRAMES."""

    name = "overruns"

    def __init__(self, n_frames, then, display) -> None:
        super().__init__(n_frames, then)
        self._display = display

    def on_frame(self, ctx):
        self._display.next_flip_extra = FRAME_S
        return super().on_frame(ctx)


class TrackerStopsDuringFeedback(TrialFeedback):
    """TrialFeedback during which the eye tracker stops recording: it stops as
    the phase begins, so the health check fails on the closing phase's very
    first frame — after everything was measured."""

    def __init__(self, tracker: ScriptedTracker, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tracker = tracker

    def on_enter(self, ctx):
        super().on_enter(ctx)
        self._tracker.stop_recording()


def feedback(then=CORRECT, verdict=None) -> TrialFeedback:
    """The closing phase the sessions here end on, when they have one."""
    return TrialFeedback(verdict=verdict or (lambda ctx: True), then=then, duration_s=3 * FRAME_S)


# ---------------------------------------------------------------------------
# Plan entries: one per attempt served, each building that trial's phases
# against the session's own tracker and display.
# ---------------------------------------------------------------------------


def clean(outcome=CORRECT):
    """A trial the rig lets finish: ``outcome`` after two frames."""
    return lambda harness: [RunForFrames(2, outcome)]


def tracker_stops(outcome=CORRECT, on_frame=1):
    """A trial the eye tracker cuts short before ``outcome`` is decided."""
    return lambda harness: [StopsTheTracker(5, outcome, harness.tracker, on_frame=on_frame)]


def dropped_frames(outcome=CORRECT):
    """A trial the subject finishes as ``outcome`` on a display that drops
    half its frames (recycled when the session runs frame QA's recycle)."""
    return lambda harness: [Overruns(6, outcome, harness.display)]


def tracker_stops_during_feedback(first=CORRECT):
    """A trial measured as ``first``, whose tracker stops while feedback is
    on screen."""
    return lambda harness: [
        RunForFrames(2, first),
        TrackerStopsDuringFeedback(
            harness.tracker, verdict=lambda ctx: True, then=CORRECT, duration_s=3 * FRAME_S
        ),
    ]


def run_session(
    tmp_path,
    plan,
    *,
    n_trials=1,
    policy=POLICY,
    reward=None,
    recycle=False,
    commands=None,
):
    """Run a session of one condition, serving the trials in ``plan`` in order.

    The session ends after ``n_trials`` completed trials; every trial that
    does not complete is served again, so ``plan`` holds one entry per
    attempt. A scripted tracker is wired the way the builder wires one — its
    health check included — and the entries stop it when they choose.
    ``recycle`` gives the session frame QA's ``recycle_trial`` (10% budget).
    """
    clock = FakeClock()
    served = iter(plan)
    box: dict = {}

    def build(setup):
        make = next(served)
        return TrialPlan(
            phases=make(box["harness"]), stimuli={"fixation": NullStimulus("fixation")}
        )

    harness = SessionHarness(
        tmp_path,
        n_trials=n_trials,
        build_trial=build,
        tracker=ScriptedTracker([], clock),
        clock=clock,
        reward=reward if reward is not None else SimulatedReward(),
        reward_policy=policy,
        commands=commands,
    )
    box["harness"] = harness
    if recycle:
        # The existing frame-QA session tests install the monitor the same
        # way: the harness's own is the default `log` policy.
        monitor = FrameMonitor(
            FrameQAConfig(
                policy="recycle_trial", max_dropped_fraction=0.10, max_consecutive_recycles=50
            ),
            1 / FRAME_S,
        )
        harness.engine._frame_monitor = monitor
        harness.runner._frame_monitor = monitor
    harness.runner.run()
    return harness


def rows(harness) -> list[dict[str, str]]:
    with harness.paths.trials_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def events_of(harness, trial_index: int, name: str):
    return [e for e in harness.collector.events if e.trial_index == trial_index and e.name == name]


def the_engine(frame_qa: FrameQAConfig | None = None, commands=None):
    """An engine wired to a scripted tracker through the builder's own health
    check, with the tracker's recording segment open as the runner opens it."""
    # The tracker's clock only times get_gaze(), which nothing here reads.
    tracker = ScriptedTracker([], FakeClock())
    harness = EngineHarness(
        frame_qa=frame_qa,
        commands=commands,
        health_checks=(make_tracker_health_check(tracker),),
    )
    tracker.start_trial(1, "attempt 1")
    return harness, tracker


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------


class TestTheFaultColumn:
    """One column names the cause, on every row, and "none" says nothing
    happened — a value, never an empty cell, as `n_dropped_frames` is 0."""

    def test_it_is_a_declared_framework_column_with_named_values(self):
        assert "fault" in TRIAL_RECORD_COLUMNS
        assert (NO_FAULT, FAULT_DROPPED_FRAMES, FAULT_TRACKER_STOPPED) == (
            "none",
            "dropped_frames",
            "tracker_stopped",
        )

    def test_the_engine_writes_it_on_every_trial_it_runs(self):
        """Clean, skipped, paused, recycled, and fed back: every record the
        engine hands back carries `fault`."""
        records = []
        harness = EngineHarness()
        records.append(harness.engine.run_trial(harness.ctx(), [RunForFrames(1, CORRECT)]))
        harness = EngineHarness(commands=ScriptedCommands([[Command.SKIP_TRIAL]]))
        records.append(harness.engine.run_trial(harness.ctx(), [RunForFrames(3, CORRECT)]))
        harness = EngineHarness(commands=ScriptedCommands([[Command.PAUSE]]))
        records.append(harness.engine.run_trial(harness.ctx(), [RunForFrames(3, CORRECT)]))
        harness = EngineHarness()
        ctx = harness.ctx(stimuli={"fixation": NullStimulus("fixation")})
        records.append(
            harness.engine.run_trial(ctx, [RunForFrames(1, PhaseAction.ADVANCE), feedback()])
        )
        assert [r.record["fault"] for r in records] == [NO_FAULT] * 4

    def test_a_clean_session_writes_none_in_every_row(self, tmp_path):
        harness = run_session(tmp_path, [clean(), clean()], n_trials=2)
        assert [row["fault"] for row in rows(harness)] == [NO_FAULT, NO_FAULT]

    def test_it_leads_the_trials_table_beside_abort_reason(self, tmp_path):
        harness = run_session(tmp_path, [tracker_stops(), clean()])
        with harness.paths.trials_path.open(encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header.index("fault") == header.index("abort_reason") + 1


class TestLostToFault:
    """Which fault a row names and whether it cost the trial its measurement
    are two questions; `lost_to_fault` answers the second from the row alone."""

    def test_a_recycled_trial_was_lost_to_dropped_frames(self):
        assert lost_to_fault("DROPPED_FRAMES", {"fault": "dropped_frames"}) == "dropped_frames"
        # A row written before the column existed is still recognised.
        assert lost_to_fault("DROPPED_FRAMES", {}) == "dropped_frames"

    def test_an_abort_by_the_tracker_was_lost_to_it(self):
        row = {"abort_reason": "tracker_stopped", "fault": "tracker_stopped"}
        assert lost_to_fault("ABORTED", row) == "tracker_stopped"

    def test_the_experimenters_skip_was_not(self):
        skipped = {"abort_reason": "skipped_by_user", "fault": "none"}
        assert lost_to_fault("ABORTED", skipped) is None
        # Not even on a trial whose tracker had stopped during its closing
        # phase before the skip: the skip is what ended it.
        row = {"abort_reason": "skipped_by_user", "fault": "tracker_stopped"}
        assert lost_to_fault("ABORTED", row) is None

    def test_a_trial_that_kept_its_outcome_was_not(self):
        assert lost_to_fault("CORRECT", {"fault": "tracker_stopped"}) is None
        assert lost_to_fault("FIX_BREAK", {"fault": "none"}) is None
        assert lost_to_fault("ABORTED", {}) is None

    def test_it_reads_trials_csv_rows_as_written(self, tmp_path):
        """The rule is expressible on the data: an analysis gets the session's
        answer from trials.csv."""
        harness = run_session(tmp_path, [tracker_stops(), clean()])
        assert [lost_to_fault(r["outcome"], r) for r in rows(harness)] == ["tracker_stopped", None]


# ---------------------------------------------------------------------------
# Dropped frames
# ---------------------------------------------------------------------------


class TestADroppedFramesTrial:
    """Frame QA recycles a trial the subject finished: it is flagged, and
    served again."""

    def test_it_is_flagged_and_served_again(self, tmp_path):
        harness = run_session(tmp_path, [dropped_frames(), clean()], recycle=True)

        first, second = rows(harness)
        assert (first["outcome"], first["completed"]) == ("DROPPED_FRAMES", "False")
        assert first["fault"] == FAULT_DROPPED_FRAMES
        assert first["outcome_before_frame_qa"] == "CORRECT"
        # The same condition, served again as its second attempt, completed.
        assert (second["attempt"], second["outcome"], second["fault"]) == ("2", "CORRECT", NO_FAULT)


# ---------------------------------------------------------------------------
# The tracker stopped
# ---------------------------------------------------------------------------


class TestATrackerStoppedTrial:
    """The tracker health check aborts a trial the subject had not finished:
    it is flagged, and served again."""

    def test_it_is_flagged_and_served_again(self, tmp_path):
        harness = run_session(tmp_path, [tracker_stops(), clean()])

        first, second = rows(harness)
        assert (first["outcome"], first["completed"]) == ("ABORTED", "False")
        assert first["abort_reason"] == FAULT_TRACKER_STOPPED
        assert first["fault"] == FAULT_TRACKER_STOPPED
        assert (second["attempt"], second["outcome"], second["fault"]) == ("2", "CORRECT", NO_FAULT)


# ---------------------------------------------------------------------------
# The edge case: the tracker stops after the measurement
# ---------------------------------------------------------------------------


class TestATrackerThatStopsDuringTheClosingPhase:
    """Before this change a failed health check aborted even the closing
    phase. When a measuring phase had already decided the outcome, that kept
    the outcome but cut the feedback off before it was drawn and wrote an
    `abort_reason` on a trial that was not aborted. When the closing phase
    was the one deciding it — a LandingCheck that ADVANCEs into TrialFeedback
    — the finished measurement came back ABORTED and was served again.

    Now the closing phase measures nothing by contract (`must_be_last`), so a
    stop there is flagged and ends nothing."""

    def run(self, phases_for, frame_qa=None, commands=None):
        harness, tracker = the_engine(frame_qa=frame_qa, commands=commands)
        fixation = NullStimulus("fixation")
        ctx = harness.ctx(stimuli={"fixation": fixation})
        result = harness.engine.run_trial(ctx, phases_for(harness, tracker))
        return result, fixation, harness

    def stopping_feedback(self, tracker, then=CORRECT):
        return TrackerStopsDuringFeedback(
            tracker, verdict=lambda ctx: True, then=then, duration_s=3 * FRAME_S
        )

    def test_a_decided_outcome_stands_and_the_row_is_flagged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="alhazen.core.engine"):
            result, fixation, harness = self.run(
                lambda h, tracker: [RunForFrames(2, CORRECT), self.stopping_feedback(tracker)]
            )

        assert result.outcome is CORRECT
        assert result.record["completed"] is True
        assert result.record["fault"] == FAULT_TRACKER_STOPPED
        assert "abort_reason" not in result.record
        assert result.lost_to_fault is None
        assert "during the closing phase 'trial_feedback', after the measurement" in caplog.text

    def test_the_feedback_runs_to_its_end(self):
        stopped, stopped_fixation, stopped_harness = self.run(
            lambda h, tracker: [RunForFrames(2, CORRECT), self.stopping_feedback(tracker)]
        )
        intact, intact_fixation, _ = self.run(
            lambda h, tracker: [
                RunForFrames(2, CORRECT),
                TrialFeedback(verdict=lambda ctx: True, then=CORRECT, duration_s=3 * FRAME_S),
            ]
        )
        # Shown for exactly as long as on a trial whose tracker kept going,
        # and its FEEDBACK went out — it used to be dropped unflipped.
        assert stopped_fixation.draw_count == intact_fixation.draw_count > 0
        names = stopped_harness.collector.names()
        assert names.count("FEEDBACK") == 1
        assert stopped.record["feedback"] == "success"

    def test_a_measurement_the_closing_phase_decides_is_not_lost(self):
        """LandingCheck → TrialFeedback(then=...): the body ADVANCEs and the
        closing phase returns the outcome. This is the case that used to
        come back ABORTED."""
        result, _fixation, _harness = self.run(
            lambda h, tracker: [
                RunForFrames(2, PhaseAction.ADVANCE),
                self.stopping_feedback(tracker, then=CORRECT),
            ]
        )
        assert result.outcome is CORRECT
        assert result.record["fault"] == FAULT_TRACKER_STOPPED
        assert result.lost_to_fault is None

    def test_a_fixation_break_stays_the_subjects(self):
        result, _fixation, _harness = self.run(
            lambda h, tracker: [RunForFrames(1, FIX_BREAK), self.stopping_feedback(tracker)]
        )
        assert result.outcome is FIX_BREAK
        assert result.record["fault"] == FAULT_TRACKER_STOPPED
        assert result.lost_to_fault is None

    def test_a_recycle_after_it_names_the_fault_that_cost_the_trial(self, caplog):
        """Both on one trial: the column names ONE cause, the one the trial is
        served again for. The tracker stop is still in the log."""
        with caplog.at_level(logging.WARNING, logger="alhazen.core.engine"):
            result, _fixation, _harness = self.run(
                lambda h, tracker: [
                    Overruns(6, CORRECT, h.display),
                    self.stopping_feedback(tracker),
                ],
                frame_qa=FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.1),
            )
        assert result.outcome.name == "DROPPED_FRAMES"
        assert result.record["fault"] == FAULT_DROPPED_FRAMES
        assert result.lost_to_fault == FAULT_DROPPED_FRAMES
        assert "health check failed (tracker_stopped) during the closing phase" in caplog.text

    def test_a_skip_after_it_is_still_a_skip(self):
        # Two body frames, then the closing phase: its first frame flags the
        # stop, and the experimenter's skip lands on its second.
        commands = ScriptedCommands([[], [], [], [Command.SKIP_TRIAL]])
        result, _fixation, _harness = self.run(
            lambda h, tracker: [
                RunForFrames(1, PhaseAction.ADVANCE),
                self.stopping_feedback(tracker),
            ],
            commands=commands,
        )
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == "skipped_by_user"
        assert result.record["fault"] == FAULT_TRACKER_STOPPED
        assert result.lost_to_fault is None

    def test_a_stop_while_measuring_still_aborts(self):
        result, _fixation, _harness = self.run(
            lambda h, tracker: [StopsTheTracker(5, CORRECT, tracker), feedback()]
        )
        assert result.outcome.name == "ABORTED"
        assert result.record["abort_reason"] == FAULT_TRACKER_STOPPED
        assert result.lost_to_fault == FAULT_TRACKER_STOPPED
        # ABORTED is not a trial result: no feedback for it.
        assert "feedback" not in result.record

    def test_in_a_session_it_is_paid_scheduled_and_counted_by_its_outcome(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(tmp_path, [tracker_stops_during_feedback()], reward=reward)

        # One row: CORRECT completed the session's only trial — no re-serve.
        (row,) = rows(harness)
        assert (row["outcome"], row["completed"], row["fault"]) == (
            "CORRECT",
            "True",
            "tracker_stopped",
        )
        # It was not aborted, and the row no longer says it was.
        assert row.get("abort_reason", "") == ""
        # Paid for the response, as any CORRECT is.
        assert reward.deliveries == [PAID]
        (paid,) = events_of(harness, 1, "REWARD")
        assert "fault" not in paid.payload
        # The trial's own line in the log says what happened.
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "trial 1 attempt 1: CORRECT (fault tracker_stopped during its closing phase" in log
