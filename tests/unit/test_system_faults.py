"""System faults: a trial that fails because the rig did, not the subject.

The rule, as the experimenter asked for it: *the subject is rewarded when a
trial's failure is not their fault, but the failed trial repeats and is logged
and flagged in the data.* Exactly two failures count:

- **dropped frames** — frame QA's ``recycle_trial`` turned the trial into
  ``DROPPED_FRAMES``. The subject did finish the trial, so it is paid for the
  response (the 1.5.0 rule), and a failure streak ends at it as at any trial
  the subject completed;
- **the eye tracker stopped recording mid-trial** — the tracker health check
  aborted the trial (``ABORTED``, ``abort_reason`` ``tracker_stopped``). The
  trial was cut off, usually before any response, so it is paid the task's
  ``RewardPolicy.on_fault`` (nothing when unset), and it neither counts
  toward a failure streak nor ends one.

Both are served again, flagged in the row's one ``fault`` column, logged at
WARNING, and left out of the training criteria. The experimenter's skip and a
pause are not faults and behave exactly as before.

And one edge case, decided here: a tracker that stops during the closing
phase — after the measurement, while feedback is on screen — does not abort
the trial. The row is flagged, the trial keeps its outcome, and it is paid,
scheduled and counted by that outcome.

Everything runs on ``alhazen.testing`` fakes and simulated time; nothing
sleeps. A test phase stops the scripted tracker on the frame the test picks.
"""

from __future__ import annotations

import csv
import json
import logging

import pytest

from alhazen.config.models import FrameQAConfig, Model, RewardPulses
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
from alhazen.dashboard.panels import panel_payload
from alhazen.dashboard.spec import DashboardPanel
from alhazen.devices.eyetracker import ScriptedTracker
from alhazen.devices.reward import SimulatedReward
from alhazen.display.frames import FrameMonitor
from alhazen.errors import RewardError
from alhazen.session.builder import make_tracker_health_check
from alhazen.stimuli.base import NullStimulus
from alhazen.task.phases import TrialFeedback
from alhazen.task.plan import TrialPlan
from alhazen.task.reward_policy import RewardPolicy
from alhazen.testing import FakeClock, ScriptedCommands, ScriptedReward
from alhazen.training import Curriculum, Stage, StageCriteria, TrainingState, TrainingSupervisor
from support import FRAME_S, EngineHarness, RequestRewardOnFrames, RunForFrames, SessionHarness

CORRECT = Outcome("CORRECT", completed=True, success=True)
WRONG = Outcome("WRONG", completed=True, success=False)
FIX_BREAK = Outcome("FIX_BREAK", completed=False)

# What a correct response earns, and — deliberately different, so a test can
# tell which one reached the valve — what the task pays a trial the tracker
# cut short.
PAID = RewardPulses(n_pulses=2, pulse_ms=100, inter_pulse_ms=50)
FAULT_PAY = RewardPulses(n_pulses=1, pulse_ms=60, inter_pulse_ms=0)
POLICY = RewardPolicy(by_outcome={"CORRECT": PAID}, on_fault=FAULT_PAY)
NO_FAULT_REWARD = RewardPolicy(by_outcome={"CORRECT": PAID})


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


class DropsThenTheTrackerStops(RequestRewardOnFrames):
    """A pursuit-like phase that asks for a mid-trial drop on its first two
    frames and stops the tracker on the second: both drops are commanded, and
    the trial is aborted on the frame after."""

    name = "drops_then_the_tracker_stops"

    def __init__(self, tracker: ScriptedTracker) -> None:
        super().__init__(5, CORRECT, on_frames=(0, 1))
        self._tracker = tracker
        self._frames = 0

    def on_frame(self, ctx):
        step = super().on_frame(ctx)  # asks for this frame's drop first
        if self._frames == 1:
            self._tracker.stop_recording()
        self._frames += 1
        return step


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
    limit=None,
    training=None,
    commands=None,
    mid_trial_reward=False,
):
    """Run a session of one condition, serving the trials in ``plan`` in order.

    The session ends after ``n_trials`` completed trials; every trial that
    does not complete is served again, so ``plan`` holds one entry per
    attempt. A scripted tracker is wired the way the builder wires one — its
    health check included — and the entries stop it when they choose.
    ``recycle`` gives the session frame QA's ``recycle_trial`` (10% budget);
    ``limit`` is the task's ``max_consecutive_failures``; ``training`` a
    supervisor to drive.
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
        mid_trial_reward=mid_trial_reward,
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
    harness.runner._max_consecutive_failures = limit
    if training is not None:
        harness.runner._training = training
    harness.runner.run()
    return harness


def rows(harness) -> list[dict[str, str]]:
    with harness.paths.trials_path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def events_of(harness, trial_index: int, name: str):
    return [e for e in harness.collector.events if e.trial_index == trial_index and e.name == name]


def warnings_in_log(harness) -> list[str]:
    lines = harness.paths.log_path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if " WARNING " in line]


def fault_lines(harness) -> list[str]:
    """The runner's one-per-trial WARNING about a trial lost to a fault."""
    marker = "a system fault, not the subject's"
    return [line for line in warnings_in_log(harness) if marker in line]


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
        assert fault_lines(harness) == []

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
    """Frame QA recycles a trial the subject finished. The subject is paid for
    the response (the 1.5.0 rule), the trial is served again, flagged,
    logged, and held against nobody."""

    def test_it_is_flagged_and_served_again(self, tmp_path):
        harness = run_session(tmp_path, [dropped_frames(), clean()], recycle=True)

        first, second = rows(harness)
        assert (first["outcome"], first["completed"]) == ("DROPPED_FRAMES", "False")
        assert first["fault"] == FAULT_DROPPED_FRAMES
        assert first["outcome_before_frame_qa"] == "CORRECT"
        # The same condition, served again as its second attempt, completed.
        assert (second["attempt"], second["outcome"], second["fault"]) == ("2", "CORRECT", NO_FAULT)

    def test_it_is_paid_for_the_response_not_the_fault_reward(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(tmp_path, [dropped_frames(), clean()], recycle=True, reward=reward)

        # The task's on_fault (FAULT_PAY) is for a trial the tracker cut
        # short; this subject finished the trial and earned PAID.
        assert reward.deliveries == [PAID, PAID]
        (paid,) = events_of(harness, 1, "REWARD")
        # Exactly the 1.5.0 payload: no `fault` key, which in a REWARD means a
        # fault reward was paid.
        assert paid.payload == {
            "manual": False,
            "outcome": "CORRECT",
            "pulses": PAID.model_dump(mode="json"),
        }
        assert rows(harness)[0]["rewarded"] == "True"

    def test_a_wrong_response_is_still_unrewarded(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(
            tmp_path, [dropped_frames(WRONG), clean(WRONG)], recycle=True, reward=reward
        )

        assert reward.deliveries == []
        (declined,) = events_of(harness, 1, "NO_REWARD")
        assert declined.payload == {"outcome": "WRONG"}
        (line,) = fault_lines(harness)
        assert "Paid for the subject's response as on any trial: WRONG pays nothing" in line

    def test_it_is_logged_with_cause_pay_and_what_happens_next(self, tmp_path):
        harness = run_session(tmp_path, [dropped_frames(), clean()], recycle=True)

        (line,) = fault_lines(harness)
        assert "trial 1 attempt 1: the display dropped more frames than frame QA allows" in line
        assert "Paid for the subject's response, CORRECT, n_pulses=2" in line
        assert ", delivered" in line
        assert "Flagged fault=dropped_frames; the condition will be served again" in line


# ---------------------------------------------------------------------------
# The tracker stopped
# ---------------------------------------------------------------------------


class TestATrackerStoppedTrial:
    """The tracker health check aborts a trial the subject had not finished.
    The task's fault reward is paid, the trial is served again, flagged,
    logged, and held against nobody."""

    def test_it_is_flagged_and_served_again(self, tmp_path):
        harness = run_session(tmp_path, [tracker_stops(), clean()])

        first, second = rows(harness)
        assert (first["outcome"], first["completed"]) == ("ABORTED", "False")
        assert first["abort_reason"] == FAULT_TRACKER_STOPPED
        assert first["fault"] == FAULT_TRACKER_STOPPED
        assert (second["attempt"], second["outcome"], second["fault"]) == ("2", "CORRECT", NO_FAULT)

    def test_it_is_paid_the_tasks_fault_reward(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(tmp_path, [tracker_stops(), clean()], reward=reward)

        assert reward.deliveries == [FAULT_PAY, PAID]
        (paid,) = events_of(harness, 1, "REWARD")
        # The payload says what the delivery was for: the fault, on the
        # ABORTED it caused — never mistakable for a response's reward.
        assert paid.payload == {
            "manual": False,
            "outcome": "ABORTED",
            "fault": "tracker_stopped",
            "pulses": FAULT_PAY.model_dump(mode="json"),
        }
        assert rows(harness)[0]["rewarded"] == "True"
        # Not a completed trial, so never a NO_REWARD either way.
        assert events_of(harness, 1, "NO_REWARD") == []

    def test_it_is_logged_with_cause_pay_and_what_happens_next(self, tmp_path):
        harness = run_session(tmp_path, [tracker_stops(), clean()])

        (line,) = fault_lines(harness)
        assert "trial 1 attempt 1: the eye tracker stopped recording before the trial's" in line
        assert "Paid the task's fault reward (RewardPolicy.on_fault), n_pulses=1" in line
        assert ", delivered" in line
        assert "Flagged fault=tracker_stopped; the condition will be served again" in line
        assert "not counted against the subject" in line

    def test_a_task_with_no_fault_reward_pays_nothing_and_says_so(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(
            tmp_path, [tracker_stops(), clean()], policy=NO_FAULT_REWARD, reward=reward
        )

        assert reward.deliveries == [PAID]  # the completed retry only
        names = [e.name for e in harness.collector.events if e.trial_index == 1]
        assert not {"REWARD", "NO_REWARD", "REWARD_FAILED"} & set(names)
        assert rows(harness)[0].get("rewarded", "") == ""
        (line,) = fault_lines(harness)
        assert "The task sets no fault reward (RewardPolicy.on_fault), so nothing was paid" in line

    def test_an_aborted_entry_in_by_outcome_is_the_skips_not_the_trackers(self, tmp_path):
        """A tracker-stopped trial is paid on_fault or nothing: its ABORTED is
        the rig's, not a result the subject earned. by_outcome["ABORTED"]
        still pays the experimenter's skip, as it always did."""
        skip_pay = RewardPulses(n_pulses=4, pulse_ms=10)
        policy = RewardPolicy(by_outcome={"CORRECT": PAID, "ABORTED": skip_pay})
        reward = SimulatedReward()
        run_session(tmp_path, [tracker_stops(), clean()], policy=policy, reward=reward)
        assert reward.deliveries == [PAID]

        # Trial 1's first frame is the first poll: skipped there.
        reward = SimulatedReward()
        run_session(
            tmp_path / "skip",
            [clean(), clean()],
            policy=policy,
            reward=reward,
            commands=ScriptedCommands([[Command.SKIP_TRIAL]]),
        )
        assert reward.deliveries == [skip_pay, PAID]

    def test_a_fault_reward_that_fails_takes_the_pump_failure_path(self, tmp_path):
        class DeadOnFirst(SimulatedReward):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def deliver(self, pulses) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise RewardError("solenoid did not open")
                super().deliver(pulses)

        reward = DeadOnFirst()
        harness = run_session(tmp_path, [tracker_stops(), clean()], reward=reward)

        (failed,) = events_of(harness, 1, "REWARD_FAILED")
        assert failed.payload == {"outcome": "ABORTED", "fault": "tracker_stopped"}
        assert events_of(harness, 1, "REWARD") == []
        assert rows(harness)[0]["rewarded"] == "False"
        # The pause the pump failure opens, with its heading, as on any trial.
        assert any("REWARD FAILURE" in title for title, _body, _color in harness.display.menus)
        (line,) = fault_lines(harness)
        assert "the delivery FAILED at the pump" in line
        # The session went on: the retry was paid once the pump was back.
        assert reward.deliveries == [PAID]

    def test_the_fault_reward_is_scaled_like_every_delivery(self, tmp_path):
        policy = RewardPolicy(
            by_outcome={"CORRECT": PAID},
            on_fault=RewardPulses(n_pulses=2, pulse_ms=60, inter_pulse_ms=0),
            scale=1.5,
        )
        reward = SimulatedReward()
        run_session(tmp_path, [tracker_stops(), clean()], policy=policy, reward=reward)
        # 2 x 1.5 = 3 pulses; the pulse width is the pump's calibration and
        # is never scaled.
        assert reward.deliveries[0] == RewardPulses(n_pulses=3, pulse_ms=60, inter_pulse_ms=0)


class TestTheFaultRewardPolicy:
    def test_it_pays_nothing_unless_the_task_sets_it(self):
        assert RewardPolicy().on_fault is None
        assert RewardPolicy(by_outcome={"CORRECT": PAID}).pulses_for_fault() is None

    def test_scale_multiplies_its_pulse_count_only(self):
        policy = RewardPolicy(on_fault=RewardPulses(n_pulses=2, pulse_ms=80), scale=1.5)
        assert policy.pulses_for_fault() == RewardPulses(n_pulses=3, pulse_ms=80)

    def test_a_scale_that_rounds_it_to_nothing_pays_nothing(self):
        policy = RewardPolicy(on_fault=RewardPulses(n_pulses=1), scale=0.1)
        assert policy.pulses_for_fault() is None

    def test_a_training_stage_rescales_it_with_everything_else(self, tmp_path):
        task = StandInTask()
        TrainingSupervisor(
            curriculum=Curriculum(stages=[Stage(name="generous", reward_scale=3.0)]),
            state=TrainingState(stage="generous"),
            task=task,
            data_root=tmp_path,
            subject="m01",
            session_id="ses-001_run-01",
        )
        assert task.reward.pulses_for_fault().n_pulses == 3 * FAULT_PAY.n_pulses


# ---------------------------------------------------------------------------
# The skip and the pause are not faults
# ---------------------------------------------------------------------------


class TestTheSkipAndThePauseAreUnchanged:
    def test_a_skip_is_not_flagged_and_not_paid_the_fault_reward(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(
            tmp_path,
            [clean(), clean()],
            reward=reward,
            commands=ScriptedCommands([[Command.SKIP_TRIAL]]),
        )

        first, second = rows(harness)
        assert (first["outcome"], first["abort_reason"]) == ("ABORTED", "skipped_by_user")
        assert first["fault"] == NO_FAULT
        assert second["outcome"] == "CORRECT"
        assert reward.deliveries == [PAID]  # the retry; nothing for the skip
        assert fault_lines(harness) == []

    def test_a_pause_writes_no_row_and_is_not_paid_the_fault_reward(self, tmp_path):
        reward = SimulatedReward()
        harness = run_session(
            tmp_path,
            [clean(), clean()],
            reward=reward,
            commands=ScriptedCommands([[Command.PAUSE]]),
        )

        (row,) = rows(harness)
        assert (row["outcome"], row["fault"]) == ("CORRECT", NO_FAULT)
        assert reward.deliveries == [PAID]
        assert fault_lines(harness) == []


# ---------------------------------------------------------------------------
# Not held against the subject: the failure streak
# ---------------------------------------------------------------------------


class TestTheFailureStreak:
    """`max_consecutive_failures` pauses the session on a run of the
    subject's failures. A tracker-stopped trial neither counts nor ends the
    run; a dropped-frames trial ends it, as it has since 1.5.0 — the subject
    completed it, and ending a streak never counts against anyone."""

    def streak(self, tmp_path, outcomes, limit=2):
        """The runner's verdict after each (outcome, lost_to_fault) in turn:
        True on the trial that reaches ``limit`` failures in a row."""
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner._max_consecutive_failures = limit
        return [harness.runner._too_many_failures_in_a_row(o, fault=f) for o, f in outcomes]

    def test_a_tracker_stopped_trial_neither_counts_nor_ends_it(self, tmp_path):
        aborted = Outcome("ABORTED", completed=False)
        verdicts = self.streak(
            tmp_path,
            outcomes=[(FIX_BREAK, None), (aborted, FAULT_TRACKER_STOPPED), (FIX_BREAK, None)],
        )
        # Not counted (the limit of two is not reached on it), and not an
        # end: the next failure is the second in a row.
        assert verdicts == [False, False, True]

    def test_a_dropped_frames_trial_ends_it(self, tmp_path):
        recycled = Outcome("DROPPED_FRAMES", completed=False)
        verdicts = self.streak(
            tmp_path,
            outcomes=[(FIX_BREAK, None), (recycled, FAULT_DROPPED_FRAMES), (FIX_BREAK, None)],
        )
        assert verdicts == [False, False, False]

    def test_the_skip_still_counts(self, tmp_path):
        aborted = Outcome("ABORTED", completed=False)
        assert self.streak(tmp_path, outcomes=[(FIX_BREAK, None), (aborted, None)]) == [
            False,
            True,
        ]

    def test_in_a_session_a_tracker_stop_between_breaks_does_not_split_them(self, tmp_path):
        harness = run_session(
            tmp_path,
            [clean(FIX_BREAK), tracker_stops(), clean(FIX_BREAK), clean()],
            limit=2,
        )

        headings = [title for title, _body, _color in harness.display.menus]
        (heading,) = [h for h in headings if "FAILED IN A ROW" in h]
        assert "2 TRIALS FAILED IN A ROW — last FIX_BREAK" in heading
        assert [row["outcome"] for row in rows(harness)] == [
            "FIX_BREAK",
            "ABORTED",
            "FIX_BREAK",
            "CORRECT",
        ]

    def test_in_a_session_a_tracker_stop_does_not_count_toward_one(self, tmp_path):
        harness = run_session(
            tmp_path, [clean(FIX_BREAK), tracker_stops(), tracker_stops(), clean()], limit=2
        )
        assert not any("FAILED IN A ROW" in title for title, _b, _c in harness.display.menus)

    def test_in_a_session_a_dropped_frames_trial_ends_one(self, tmp_path):
        harness = run_session(
            tmp_path,
            [clean(FIX_BREAK), dropped_frames(), clean(FIX_BREAK), clean()],
            limit=2,
            recycle=True,
        )
        assert not any("FAILED IN A ROW" in title for title, _b, _c in harness.display.menus)


# ---------------------------------------------------------------------------
# Not held against the subject: training
# ---------------------------------------------------------------------------


class Params(Model):
    window_dva: float = 5.0


class StandInTask:
    """What the supervisor needs of a Task: its params and its reward."""

    def __init__(self) -> None:
        self.params = Params()
        self.reward = POLICY


def supervisor(
    tmp_path, criteria: StageCriteria | None = None, at: str = "one"
) -> TrainingSupervisor:
    """A two-stage curriculum whose stages both judge by ``criteria`` (by
    default a window too large to decide anything), with the subject at
    stage ``at``."""
    criteria = criteria or StageCriteria(window=50, min_trials=50)
    return TrainingSupervisor(
        curriculum=Curriculum(
            stages=[Stage(name="one", criteria=criteria), Stage(name="two", criteria=criteria)]
        ),
        state=TrainingState(stage=at),
        task=StandInTask(),
        data_root=tmp_path,
        subject="m01",
        session_id="ses-001_run-01",
    )


DROPPED = Outcome("DROPPED_FRAMES", completed=False)
ABORTED = Outcome("ABORTED", completed=False)
RECYCLED_ROW = {"fault": "dropped_frames", "outcome_before_frame_qa": "CORRECT"}
TRACKER_ROW = {"fault": "tracker_stopped", "abort_reason": "tracker_stopped"}
SKIPPED_ROW = {"fault": "none", "abort_reason": "skipped_by_user"}


class TestTrainingLeavesFaultTrialsOut:
    def test_neither_fault_reaches_the_window(self, tmp_path):
        training = supervisor(tmp_path)
        training.observe(DROPPED, dict(RECYCLED_ROW))
        training.observe(ABORTED, dict(TRACKER_ROW))
        assert training.state.window == []

    def test_the_skip_and_a_trial_that_kept_its_outcome_do(self, tmp_path):
        training = supervisor(tmp_path)
        training.observe(ABORTED, dict(SKIPPED_ROW))
        # The tracker stopped during this trial's feedback: its CORRECT is the
        # subject's own, and counts.
        training.observe(CORRECT, {"fault": "tracker_stopped", "rt_ms": 250.0})
        assert [s["outcome"] for s in training.state.window] == ["ABORTED", "CORRECT"]

    def test_faults_do_not_pull_completed_rate_down_into_a_demotion(self, tmp_path):
        criteria = StageCriteria(window=4, min_trials=4, demote_when={"completed_rate": 0.5})
        training = supervisor(tmp_path, criteria, at="two")
        for _ in range(4):
            training.observe(CORRECT, {})
        for _ in range(2):
            training.observe(DROPPED, dict(RECYCLED_ROW))
            training.observe(ABORTED, dict(TRACKER_ROW))
        # Counted, the four faults would be the whole window — a completed
        # rate of 0 — and would demote a subject that finished every trial
        # the rig let it finish.
        assert training.transition() is None
        assert training.stage.name == "two"

    def test_faults_do_not_fill_the_window_toward_min_trials(self, tmp_path):
        criteria = StageCriteria(window=4, min_trials=4, promote_when={"success_rate": 0.75})
        training = supervisor(tmp_path, criteria)
        for _ in range(3):
            training.observe(CORRECT, {})
        training.observe(ABORTED, dict(TRACKER_ROW))
        # Counted, the fault would make four attempts and promote on three
        # real trials; left out, three is not enough to decide anything.
        assert training.transition() is None
        training.observe(CORRECT, {})
        change = training.transition()
        assert change is not None and change.to_stage == "two"

    def test_in_a_session_the_window_holds_only_the_subjects_trials(self, tmp_path):
        training = supervisor(tmp_path)
        run_session(
            tmp_path,
            [
                tracker_stops(),
                dropped_frames(),
                clean(FIX_BREAK),
                tracker_stops_during_feedback(),
            ],
            recycle=True,
            training=training,
        )
        # The tracker-stopped and the recycled trials never arrive; the
        # fixation break and the trial whose tracker stopped only during its
        # feedback (which kept its CORRECT) do.
        assert [s["outcome"] for s in training.state.window] == ["FIX_BREAK", "CORRECT"]


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
        # Paid for the response, not the fault reward.
        assert reward.deliveries == [PAID]
        (paid,) = events_of(harness, 1, "REWARD")
        assert "fault" not in paid.payload
        # Not a trial lost to a fault, so no fault line; the trial's own line
        # says what happened.
        assert fault_lines(harness) == []
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "trial 1 attempt 1: CORRECT (fault tracker_stopped during its closing phase" in log


# ---------------------------------------------------------------------------
# Mid-trial reward
# ---------------------------------------------------------------------------


class TestMidTrialDropsOnAFaultTrial:
    """A pursuit task pays drops during the trial. The drops a faulted trial
    already delivered stay delivered and counted; the fault reward follows
    them on the same valve; every delivery is counted once."""

    def run(self, tmp_path, device):
        return run_session(
            tmp_path,
            [lambda harness: [DropsThenTheTrackerStops(harness.tracker)], clean()],
            reward=device,
            mid_trial_reward=True,
        )

    def test_the_drops_stay_counted_and_the_fault_reward_follows_them(self, tmp_path):
        device = ScriptedReward()
        harness = self.run(tmp_path, device)

        first, _second = rows(harness)
        assert (first["outcome"], first["fault"]) == ("ABORTED", "tracker_stopped")
        assert first["n_mid_trial_rewards"] == "2"
        assert first["n_mid_trial_reward_failures"] == "0"
        assert first["rewarded"] == "True"
        drop = RewardPulses(n_pulses=1, pulse_ms=50, inter_pulse_ms=0)
        # Both drops, then the fault reward behind them, then the retry's pay.
        assert device.attempts == [drop, drop, FAULT_PAY, PAID]
        assert device.max_on_valve == 1
        (line,) = fault_lines(harness)
        assert "2 mid-trial drop(s) delivered before the fault stay counted" in line

    def test_the_reward_panel_counts_every_delivery_once(self, tmp_path):
        harness = self.run(tmp_path, ScriptedReward())

        events = harness.recorder.events
        fault_rewards = [
            e for e in events if e["event"] == "REWARD" and "fault" in json.loads(e["payload_json"])
        ]
        assert len(fault_rewards) == 1
        data = panel_payload(
            DashboardPanel(kind="rewards", title="Reward"), harness.recorder.trials, events
        )
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}
        # Two drops (each at its REWARD_DELIVERED), the fault reward, and the
        # retry's CORRECT.
        assert stats["deliveries"] == "4"


class TestTheDashboardStillAddsUp:
    def test_the_outcomes_panel_shows_what_each_trial_ended_as(self, tmp_path):
        harness = run_session(tmp_path, [tracker_stops(), dropped_frames(), clean()], recycle=True)
        data = panel_payload(
            DashboardPanel(kind="outcomes", title="Outcomes"), harness.recorder.trials, []
        )
        counts = {item["label"]: item["value"] for item in data["items"]}
        assert counts == {"ABORTED": 1, "DROPPED_FRAMES": 1, "CORRECT": 1}

    @pytest.mark.parametrize("policy", [POLICY, NO_FAULT_REWARD])
    def test_the_reward_panel_counts_a_fault_reward_as_a_delivery(self, tmp_path, policy):
        harness = run_session(tmp_path, [tracker_stops(), clean()], policy=policy)
        data = panel_payload(
            DashboardPanel(kind="rewards", title="Reward"), [], harness.recorder.events
        )
        stats = {stat["label"]: stat["value"] for stat in data["stats"]}
        assert stats["deliveries"] == ("2" if policy.on_fault is not None else "1")
        # Never counted as unrewarded: the trial was not a completed one.
        assert "unrewarded" not in stats
