"""SessionRunner: the outer-loop contract against fakes."""

from __future__ import annotations

import csv

import pytest
import yaml

from alhazen.core.commands import Command
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.testing import FakeClock, ScriptedCommands
from support import COMPLETED, SessionHarness


def read_trials(harness):
    with harness.paths.trials_path.open() as f:
        return list(csv.DictReader(f))


class TestHappyPath:
    def test_full_session_writes_everything(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=2)
        harness.runner.run()

        rows = read_trials(harness)
        assert len(rows) == 2
        assert [r["outcome"] for r in rows] == ["COMPLETED", "COMPLETED"]
        assert [r["trial_index"] for r in rows] == ["1", "2"]
        assert rows[0]["condition"] == "a"

        assert harness.paths.snapshot_path.exists()
        snap = yaml.safe_load(harness.paths.snapshot_path.read_text())
        assert snap["config"]["info"]["seed"] == 7

        assert harness.paths.events_path.exists()
        assert harness.paths.frames_path.exists()
        assert harness.paths.manifest_path.exists()
        assert harness.paths.log_path.exists()
        assert (tmp_path / "participants.tsv").read_text().splitlines()[1] == "sub-t01"

        assert harness.display.closed

    def test_score_hook_applied(self, tmp_path):
        def score(record):
            record["my_metric"] = 42.0
            return record

        harness = SessionHarness(tmp_path, n_trials=1, score=score)
        harness.runner.run()
        assert read_trials(harness)[0]["my_metric"] == "42.0"

    def test_manifest_covers_run_dir(self, tmp_path):
        from alhazen.data.manifest import verify_manifest

        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner.run()
        assert verify_manifest(harness.paths.run_dir, harness.paths.manifest_path) == []


class TestSessionLogStructure:
    """The log is a record of the session, not of its dropped frames: it
    says how the session started, what each trial was, and how it ended.
    A log that simply stops is what a crashed session used to look like."""

    def read_log(self, harness):
        return harness.paths.log_path.read_text(encoding="utf-8").splitlines()

    def test_it_records_start_devices_notes_every_trial_and_the_end(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=2)
        harness.runner.setup_notes = ["mode: test — a rehearsal", "reduced: n: 8 -> 1"]
        harness.runner.run()

        log = self.read_log(harness)
        assert any("session start: subject t01" in line for line in log)
        assert any("devices: display " in line and "eyetracker none" in line for line in log)
        assert any("setup: mode: test — a rehearsal" in line for line in log)
        assert any("setup: reduced: n: 8 -> 1" in line for line in log)
        assert any("trial 1 attempt 1: COMPLETED" in line for line in log)
        # The harness serves one condition twice, so the second trial is its attempt 2.
        assert any("trial 2 attempt 2: COMPLETED" in line for line in log)
        assert "session end: complete — 2 trials served, 2 rows recorded (COMPLETED 2)" in log[-1]

    def test_an_incomplete_trial_says_it_was_re_served(self, tmp_path):
        from alhazen.task.plan import TrialPlan
        from support import FAILED, RunForFrames

        answers = iter([FAILED, COMPLETED, COMPLETED])

        def build(setup):
            return TrialPlan(phases=[RunForFrames(1, next(answers))])

        harness = SessionHarness(tmp_path, n_trials=2, build_trial=build)
        harness.runner.run()

        log = self.read_log(harness)
        assert any("trial 1 attempt 1: FAILED — not completed" in line for line in log)
        assert "3 trials served, 3 rows recorded (COMPLETED 2, FAILED 1)" in log[-1]

    def test_a_session_that_fails_says_so_and_why(self, tmp_path):
        def broken_build(setup):
            raise RuntimeError("task bug")

        harness = SessionHarness(tmp_path, n_trials=1, build_trial=broken_build)
        with pytest.raises(RuntimeError):
            harness.runner.run()

        log = self.read_log(harness)
        assert any(
            "ERROR" in line
            and "session end: FAILED on trial 1 after 0 rows" in line
            and "RuntimeError: task bug" in line
            for line in log
        )

    def test_a_cancelled_session_is_not_a_complete_one(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner._instructions = "press space"
        harness.runner._await_start = lambda: False
        harness.runner.run()

        assert "session end: cancelled — 0 trials served" in self.read_log(harness)[-1]


class TestParadigmSummary:
    def test_a_scheduler_with_a_summary_writes_it(self, tmp_path):
        import numpy as np

        from alhazen.paradigms.constant import ConstantStimuli

        source = ConstantStimuli(
            {"side": ["left", "right"]}, n_per_condition=1, rng=np.random.default_rng(0)
        )
        harness = SessionHarness(tmp_path, source=source)
        harness.runner.run()

        assert harness.paths.paradigm_path.exists()
        with harness.paths.paradigm_path.open() as f:
            rows = list(csv.DictReader(f))
        # The table that says whether the session ended balanced.
        assert sorted(r["side"] for r in rows) == ["left", "right"]
        assert all(r["n_completed"] == "1" for r in rows)

    def test_a_scheduler_with_nothing_to_say_writes_no_file(self, tmp_path):
        # An absent file means "this paradigm had no summary", not "something
        # failed to write" — so SimpleSequence leaves none.
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner.run()
        assert not harness.paths.paradigm_path.exists()


class TestPauseSemantics:
    def test_paused_trial_requeues_but_writes_no_row(self, tmp_path):
        # PAUSE lands on the first frame of trial 1; unattended runs
        # auto-resume, the condition is re-served, and the session still
        # collects its 2 completed measurements.
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands)
        harness.runner.run()

        rows = read_trials(harness)
        assert [r["outcome"] for r in rows] == ["COMPLETED", "COMPLETED"]
        # The paused attempt consumed trial_index 1 and attempt 1.
        assert [r["trial_index"] for r in rows] == ["2", "3"]
        assert [r["attempt"] for r in rows] == ["2", "3"]
        assert "PAUSED" in harness.collector.names()
        assert "RESUMED" in harness.collector.names()

    def test_quit_ends_session_but_saves_data(self, tmp_path):
        commands = ScriptedCommands([[Command.QUIT]])
        harness = SessionHarness(tmp_path, n_trials=5, commands=commands)
        harness.runner.run()
        assert read_trials(harness) == []
        assert harness.paths.trials_path.exists()
        assert harness.paths.manifest_path.exists()

    def test_skip_writes_aborted_row_and_requeues(self, tmp_path):
        commands = ScriptedCommands([[Command.SKIP_TRIAL]])
        harness = SessionHarness(tmp_path, n_trials=1, commands=commands)
        harness.runner.run()
        rows = read_trials(harness)
        assert [r["outcome"] for r in rows] == ["ABORTED", "COMPLETED"]
        assert rows[0]["abort_reason"] == "skipped_by_user"


class TestTeardownResilience:
    def test_all_steps_attempted_and_first_error_reraised(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=1)

        def broken_write():
            raise OSError("disk full")

        harness.recorder.write = broken_write  # type: ignore[method-assign]
        with pytest.raises(OSError, match="disk full"):
            harness.runner.run()
        # Every later step still ran.
        assert harness.paths.frames_path.exists()
        assert harness.paths.manifest_path.exists()
        assert harness.display.closed

    def test_teardown_error_never_masks_session_error(self, tmp_path):
        def broken_build(setup):
            raise RuntimeError("task bug")

        harness = SessionHarness(tmp_path, n_trials=1, build_trial=broken_build)

        def broken_write():
            raise OSError("disk full")

        harness.recorder.write = broken_write  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="task bug"):
            harness.runner.run()


class TestTrackerCalibrationBeforeTrialOne:
    """A tracker that can say it holds no calibration stops the session at
    the pause screen before trial 1, with that reason. Gaze from an
    uncalibrated device is not a position, and a session that ran on one
    would look like a subject who never fixated."""

    class Tracker(ScriptedTracker):
        def __init__(self, clock, calibrated: bool) -> None:
            super().__init__([], clock)
            self.calibrated = calibrated
            self.asked = 0

        def calibration_state(self) -> bool:
            self.asked += 1
            return self.calibrated

    def test_no_calibration_pauses_with_the_reason_then_runs(self, tmp_path):
        clock = FakeClock()
        tracker = self.Tracker(clock, calibrated=False)
        harness = SessionHarness(tmp_path, n_trials=1, tracker=tracker, clock=clock)
        harness.runner.run()

        assert tracker.asked == 1
        # Unattended, so the pause resolved by resuming — but the screen said
        # why, the log said why, and the session then ran its trial.
        assert any(
            "TRACKER NOT CALIBRATED" in title or "TRACKER NOT CALIBRATED" in body
            for title, body, _ in harness.display.menus
        )
        assert "RESUMED" in harness.collector.names()
        assert [r["outcome"] for r in read_trials(harness)] == ["COMPLETED"]
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "reports NO calibration before trial 1" in log

    def test_a_calibrated_tracker_is_not_interrupted(self, tmp_path):
        clock = FakeClock()
        tracker = self.Tracker(clock, calibrated=True)
        harness = SessionHarness(tmp_path, n_trials=1, tracker=tracker, clock=clock)
        harness.runner.run()

        assert tracker.asked == 1
        assert harness.display.menus == []
        assert "RESUMED" not in harness.collector.names()

    def test_a_tracker_without_the_capability_is_not_asked(self, tmp_path):
        clock = FakeClock()
        harness = SessionHarness(
            tmp_path, n_trials=1, tracker=ScriptedTracker([], clock), clock=clock
        )
        harness.runner.run()
        assert harness.display.menus == []


class TestTooManyFailuresInARow:
    """A session that completed none of 33 trials — every one a fixation
    break, the eye sitting just outside the window on a calibration that
    passed — ran to its end with nothing on screen saying so. The task now
    names how many in a row is too many, and the session pauses there."""

    def harness(self, tmp_path, answers, limit):
        from alhazen.task.plan import TrialPlan
        from support import RunForFrames

        outcomes = iter(answers)

        def build(setup):
            return TrialPlan(phases=[RunForFrames(1, next(outcomes))])

        harness = SessionHarness(tmp_path, n_trials=1, build_trial=build)
        harness.runner._max_consecutive_failures = limit
        return harness

    def test_the_limit_pauses_with_the_reason_and_the_count_restarts(self, tmp_path):
        from support import FAILED

        # Three failures, then success: the limit of two pauses once, after
        # the second; the third failure starts a fresh count.
        harness = self.harness(tmp_path, [FAILED, FAILED, FAILED, COMPLETED], limit=2)
        harness.runner.run()

        headings = [title for title, _body, _color in harness.display.menus]
        assert sum("2 TRIALS FAILED IN A ROW" in h for h in headings) == 1, headings
        assert any("last FAILED" in h for h in headings)
        assert harness.collector.names().count("RESUMED") == 1
        assert [r["outcome"] for r in read_trials(harness)] == [
            "FAILED",
            "FAILED",
            "FAILED",
            "COMPLETED",
        ]
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "2 trials in a row not completed, the last FAILED on trial 2: pausing" in log

    def test_a_completed_trial_resets_the_count(self, tmp_path):
        from support import FAILED

        harness = self.harness(tmp_path, [FAILED, COMPLETED, FAILED, COMPLETED], limit=2)
        harness.runner.run()
        assert harness.display.menus == []

    def test_a_completed_trial_with_a_dead_pump_still_resets_the_count(self, tmp_path):
        """The reward failure has its own pause and its own `continue`, which
        used to carry the counter past the trial untouched. Two breaks, a
        completed trial the pump could not pay for, one more break — and the
        screen said three in a row, over a trial the subject had done."""
        from alhazen.config.models import RewardPulses
        from alhazen.devices.reward import SimulatedReward
        from alhazen.errors import RewardError
        from alhazen.task.plan import TrialPlan
        from alhazen.task.reward_policy import RewardPolicy
        from support import FAILED, RunForFrames

        PAID = RewardPulses(n_pulses=1)

        class DeadPump(SimulatedReward):
            """Fails its first delivery — the one the completed trial in the
            middle of the run of failures asks for."""

            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            def deliver(self, pulses) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise RewardError("solenoid did not open")
                super().deliver(pulses)

        # Two conditions; a trial that did not complete is re-served, so this
        # is served as FAILED, FAILED, COMPLETED (pump dies), FAILED, COMPLETED.
        outcomes = iter([FAILED, FAILED, COMPLETED, FAILED, COMPLETED])

        def build(setup):
            return TrialPlan(phases=[RunForFrames(1, next(outcomes))])

        pauses: list = []

        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            build_trial=build,
            reward=DeadPump(),
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": PAID}),
        )
        harness.runner._max_consecutive_failures = 3
        harness.runner._on_pause = lambda menu: (pauses.append(menu.title), "resume")[1]
        harness.runner.run()

        # One pause, and it is the pump's — not a count of three that
        # included a trial the subject completed.
        assert len(pauses) == 1, pauses
        assert "REWARD FAILURE" in pauses[0]
        assert not any("FAILED IN A ROW" in title for title in pauses)

    def test_a_display_fault_is_not_counted_as_the_subject_failing(self, tmp_path):
        """DROPPED_FRAMES is the panel, not the eye. Frame QA counts those
        itself and stops the run with its own message; counting them here as
        well stopped the session to ask someone to check a calibration that
        was fine."""
        from alhazen.core.trial import DROPPED_FRAMES

        harness = self.harness(tmp_path, [COMPLETED], limit=2)
        for _ in range(5):
            assert not harness.runner._too_many_failures_in_a_row(DROPPED_FRAMES)

    def test_no_limit_never_pauses(self, tmp_path):
        from support import FAILED

        harness = self.harness(tmp_path, [FAILED] * 5 + [COMPLETED], limit=None)
        harness.runner.run()
        assert harness.display.menus == []

    def test_a_limit_below_one_is_refused(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=1)
        from alhazen.session.runner import SessionRunner

        with pytest.raises(ValueError, match="max_consecutive_failures must be >= 1"):
            SessionRunner.__init__(
                harness.runner,
                cfg=harness.cfg,
                paths=harness.paths,
                display=harness.display,
                screen=harness.runner._screen,
                clock=harness.clock,
                bus=harness.bus,
                engine=harness.engine,
                source=harness.source,
                build_trial=lambda setup: None,
                recorder=harness.recorder,
                frame_monitor=harness.frame_monitor,
                commands=harness.commands,
                refresh_rate_hz=60.0,
                task_rng=harness.runner._task_rng,
                max_consecutive_failures=0,
            )
