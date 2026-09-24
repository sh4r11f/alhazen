"""SessionRunner: the outer-loop contract against fakes."""

from __future__ import annotations

import csv
import logging

import pytest
import yaml

from alhazen.core.commands import Command
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.testing import FakeClock, ScriptedCommands, ScriptedReward
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
        harness = SessionHarness(
            tmp_path, n_trials=1, instructions="press space", await_start=lambda: False
        )
        harness.runner.run()

        assert "session end: cancelled — 0 trials served" in self.read_log(harness)[-1]


class TestInstructions:
    def test_the_instructions_are_shown_as_prose(self, tmp_path):
        """Instructions are hard-wrapped prose (an instructions.md), so they
        go to the display as given with reflow on, and the display joins the
        wrapped lines — rather than wrapping them a second time."""
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            instructions="Look at the\ndot.\n\nPress SPACE.",
            await_start=lambda: False,
        )
        harness.runner.run()

        assert harness.display.message_calls[0] == ("Look at the\ndot.\n\nPress SPACE.", True)

    def test_a_display_without_reflow_still_shows_them(self, tmp_path):
        """A display backend written before ``reflow`` existed takes the text
        alone. The runner relies on the default rather than passing the
        argument, so such a backend keeps working."""
        harness = SessionHarness(
            tmp_path, n_trials=1, instructions="Look at the\ndot.", await_start=lambda: False
        )
        shown: list[str] = []
        # An instance attribute shadows FakeDisplay's method: this display's
        # show_message has the pre-reflow, one-argument signature.
        harness.display.show_message = shown.append  # type: ignore[method-assign]
        harness.runner.run()

        assert shown == ["Look at the\ndot."]


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


class ClosableSync:
    """A sync output that remembers being closed. A real one holds an NI-DAQ
    task per line until then, and the next session cannot open them."""

    def __init__(self) -> None:
        self.closed = False

    def pulse(self, line: str) -> None:
        return

    def close(self) -> None:
        self.closed = True


class StoppableDashboard:
    """A dashboard that remembers being stopped — the real one is a child
    process — and, with ``fail=True``, refuses every publish the way one
    whose child has died does, naming the status it was given."""

    url = "http://127.0.0.1:0/"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.states: list[dict] = []
        self.stopped = False

    def publish(self, state: dict) -> None:
        if self.fail:
            raise RuntimeError(f"the dashboard could not publish the {state['status']!r} state")
        self.states.append(state)

    def publish_camera(self, pixels, t: float) -> None:
        return

    def poll_settings(self) -> list:
        return []

    def poll_commands(self) -> list:
        return []

    def save(self, figures_dir, state: dict) -> None:
        # A file on disk, so "nothing was written into the run directory"
        # covers the saved dashboard too.
        (figures_dir / "dashboard_state.json").write_text(state["status"])

    def stop(self) -> None:
        self.stopped = True


class HandBackTraining:
    """The two calls teardown makes on a curriculum, recorded: saving the
    subject's state, and handing the task back as it was passed in."""

    def __init__(self) -> None:
        self.saved = False
        self.restored = False

    def save(self) -> None:
        self.saved = True

    def restore_base(self) -> None:
        self.restored = True


class TestASetupFailureStillTearsDown:
    """By the time run() is called the builder has opened the window,
    connected the tracker, and started the reward device, the sync lines and
    the dashboard's process. The steps that set the session up before trial 1
    ran outside the try whose finally tears it down, so one that failed left
    every device held and wrote no "session end" line anywhere."""

    def session(
        self,
        tmp_path,
        dashboard: StoppableDashboard | None = None,
        training: HandBackTraining | None = None,
    ):
        clock = FakeClock()
        tracker = ScriptedTracker([], clock)
        reward = ScriptedReward()
        sync = ClosableSync()
        dashboard = dashboard if dashboard is not None else StoppableDashboard()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            tracker=tracker,
            reward=reward,
            sync=sync,
            clock=clock,
            dashboard=dashboard,
            training=training,
        )
        return harness, tracker, reward, sync, dashboard

    @staticmethod
    def assert_released(harness, tracker, reward, sync, dashboard) -> None:
        assert harness.display.closed, "the window was left open"
        assert len(tracker.shutdowns) == 1, "the tracker's link was left held"
        assert reward.closed, "the reward device was left open"
        assert sync.closed, "the sync lines were left held"
        assert dashboard.stopped, "the dashboard's process was left running"

    def test_a_session_log_that_cannot_be_opened(self, tmp_path, caplog):
        harness, *devices = self.session(tmp_path)
        harness.paths.log_path.mkdir()  # a directory cannot be opened as the log file

        with (
            caplog.at_level(logging.ERROR, logger="alhazen.session.runner"),
            pytest.raises(OSError, match="session.log"),
        ):
            harness.runner.run()

        self.assert_released(harness, *devices)
        # No log of its own, so how it ended goes where the program's other
        # logging goes.
        assert "session end: FAILED" in caplog.text
        # The snapshot was written, so this is a run — a failed one — and
        # teardown writes it like any other.
        assert harness.paths.trials_path.exists()
        assert harness.paths.manifest_path.exists()

    def test_a_participants_registry_that_cannot_be_written(self, tmp_path):
        harness, *devices = self.session(tmp_path)
        # A directory in the registry's place cannot be opened: the same
        # OSError a registry locked by another program (a spreadsheet) raises.
        (tmp_path / "participants.tsv").mkdir()

        with pytest.raises(OSError, match="participants.tsv"):
            harness.runner.run()

        self.assert_released(harness, *devices)
        # The log is attached before the subject is registered, so this
        # failure is in the run's own log.
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "session end: FAILED" in log
        assert "participants.tsv" in log
        assert harness.paths.manifest_path.exists()

    def test_a_first_dashboard_publish_that_fails(self, tmp_path):
        # Every publish fails, the final one in teardown included: the error
        # that propagates is still the one that ended the session.
        harness, *devices = self.session(tmp_path, StoppableDashboard(fail=True))

        with pytest.raises(RuntimeError, match="'running'"):
            harness.runner.run()

        self.assert_released(harness, *devices)
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "session end: FAILED" in log
        assert harness.paths.manifest_path.exists()

    def test_a_snapshot_that_cannot_be_written_releases_and_writes_nothing(self, tmp_path, caplog):
        harness, tracker, reward, sync, dashboard = self.session(tmp_path)
        harness.paths.snapshot_path.mkdir()  # a directory cannot be written as the snapshot

        with (
            caplog.at_level(logging.WARNING, logger="alhazen.session.runner"),
            pytest.raises(OSError, match="config_snapshot.yaml"),
        ):
            harness.runner.run()

        self.assert_released(harness, tracker, reward, sync, dashboard)
        # A directory with no snapshot is not a run: nothing is written into
        # it — no data files, no log, no manifest, no saved dashboard — and
        # the tracker is released without being handed a destination for its
        # recording.
        assert [p for p in harness.paths.run_dir.rglob("*") if p.is_file()] == []
        assert tracker.shutdowns == [None]
        assert dashboard.states == []
        assert "session end: FAILED" in caplog.text
        assert "config snapshot was never written" in caplog.text

    def test_a_snapshot_failure_hands_the_task_back_but_saves_no_training_state(self, tmp_path):
        training = HandBackTraining()
        harness, *_ = self.session(tmp_path, training=training)
        harness.paths.snapshot_path.mkdir()

        with pytest.raises(OSError, match="config_snapshot.yaml"):
            harness.runner.run()

        # The task a caller still holds is put back as it was passed in, but
        # a session that never started does not move the subject's record.
        assert training.restored
        assert not training.saved


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

        return SessionHarness(
            tmp_path, n_trials=1, build_trial=build, max_consecutive_failures=limit
        )

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
            max_consecutive_failures=3,
            on_pause=lambda menu: (pauses.append(menu.title), "resume")[1],
        )
        harness.runner.run()

        # One pause, and it is the pump's — not a count of three that
        # included a trial the subject completed.
        assert len(pauses) == 1, pauses
        assert "REWARD FAILURE" in pauses[0]
        assert not any("FAILED IN A ROW" in title for title in pauses)

    def test_a_recycled_trial_ends_the_streak_because_the_subject_completed_it(self, tmp_path):
        """The engine only recycles a trial the subject completed; the display
        failed, not the eye. Recycles used to be skipped over rather than end
        the streak, which joined separate runs of failures into one."""
        from support import FAILED

        # Driven through a session: the display drops frames on the third
        # trial, which the subject completed, so frame QA recycles it into
        # DROPPED_FRAMES between two pairs of failures.
        plan = [FAILED, FAILED, ("slow", COMPLETED), FAILED, FAILED, COMPLETED]
        harness, pauses = self._frames_session(tmp_path, plan, limit=3, kind="psychopy")

        # The runner saw exactly FAILED, FAILED, DROPPED_FRAMES, FAILED, FAILED ...
        outcomes = [row["outcome"] for row in read_trials(harness)]
        assert outcomes[:5] == ["FAILED", "FAILED", "DROPPED_FRAMES", "FAILED", "FAILED"]
        assert outcomes[5:] == ["COMPLETED"]
        # ... and no run of three was ever counted from it.
        assert not any("FAILED IN A ROW" in title for title in pauses), pauses

    def _frames_session(self, tmp_path, plan, limit, kind, budget=0.10):
        """A session on a display that drops frames when told to.

        ``plan`` holds outcomes, and ``("slow", outcome)`` for a trial that ends
        with that outcome after every one of its frames overran. Frame QA
        recycles past ``budget``, with a consecutive-recycle limit high enough
        never to be what stops the run. ``kind`` is what the display reports
        itself as.
        """
        from alhazen.config.models import FrameQAConfig
        from alhazen.task.plan import TrialPlan
        from support import FRAME_S, RunForFrames

        served = iter(plan)
        box = {}

        class Overruns(RunForFrames):
            def on_frame(self, ctx):
                box["harness"].display.next_flip_extra = FRAME_S
                return super().on_frame(ctx)

        def build(setup):
            item = next(served)
            if isinstance(item, tuple):
                return TrialPlan(phases=[Overruns(20, item[1])])
            return TrialPlan(phases=[RunForFrames(20, item)])

        # The session ends after this many completed trials; a slow trial the
        # subject completes is recycled and re-served, so only clean ones count.
        clean_completions = sum(1 for item in plan if item is COMPLETED)
        pauses = []
        harness = SessionHarness(
            tmp_path,
            n_trials=clean_completions,
            build_trial=build,
            frame_qa=FrameQAConfig(
                policy="recycle_trial", max_dropped_fraction=budget, max_consecutive_recycles=50
            ),
            max_consecutive_failures=limit,
            on_pause=lambda menu: (pauses.append(menu.title), "resume")[1],
        )
        box["harness"] = harness
        harness.display.kind = kind
        harness.runner.run()
        return harness, pauses

    def test_a_display_that_recycles_completed_trials_cannot_manufacture_a_streak(self, tmp_path):
        """kde-vergence's rehearsal, trial for trial. Every recycled trial was
        one the subject completed, and those completions separated two, two
        and two failures. Skipped over, they left a "6 trials in a row" that
        was never in a row, and the pause told the operator to check the
        calibration while the panel dropped half its frames."""
        from alhazen.core.trial import Outcome

        no_saccade = Outcome("NO_SACCADE", completed=False)
        fix_break = Outcome("FIX_BREAK", completed=False)
        slow = ("slow", COMPLETED)
        plan = [slow, fix_break, fix_break, slow, no_saccade, no_saccade, slow, slow]
        plan += [fix_break, no_saccade] + [COMPLETED] * 3

        harness, pauses = self._frames_session(tmp_path, plan, limit=6, kind="psychopy")

        assert not any("FAILED IN A ROW" in title for title in pauses), pauses
        outcomes = [row["outcome"] for row in read_trials(harness)]
        assert outcomes.count("DROPPED_FRAMES") == 4

    def test_a_streak_on_a_failing_display_says_to_check_the_display_first(self, tmp_path):
        """A panel missing vsyncs can cause real fixation breaks. When failures
        in the streak happened on trials that dropped more frames than frame QA
        allows, the pause leads with the display rather than sending the
        experimenter to recalibrate a calibration that is fine."""
        from support import FAILED

        slow_failure = ("slow", FAILED)
        plan = [slow_failure, slow_failure, FAILED, COMPLETED]

        harness, pauses = self._frames_session(tmp_path, plan, limit=3, kind="psychopy")

        (title,) = [t for t in pauses if "FAILED IN A ROW" in t]
        assert "2 of them dropped over 10% of their frames" in title
        assert "check the display before recalibrating" in title
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "check the display before recalibrating" in log

    def test_the_pause_states_the_budget_as_configured(self, tmp_path):
        """Whole percents wrote a 7.5% budget as "8%", so the heading and the
        log claimed the trials dropped over 8% of their frames: a number
        nobody set, and one those trials need not have reached."""
        from support import FAILED

        slow_failure = ("slow", FAILED)
        plan = [slow_failure, slow_failure, FAILED, COMPLETED]

        harness, pauses = self._frames_session(
            tmp_path, plan, limit=3, kind="psychopy", budget=0.075
        )

        (title,) = [t for t in pauses if "FAILED IN A ROW" in t]
        assert "2 of them dropped over 7.5% of their frames" in title
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "2 of them dropped more than 7.5% of their frames" in log
        assert "8%" not in title

    def test_on_a_simulated_display_frame_times_are_not_evidence(self, tmp_path):
        """A simulated display's frame times are the host's scheduler, so the
        same streak there keeps the subject-side heading."""
        from support import FAILED

        slow_failure = ("slow", FAILED)
        plan = [slow_failure, slow_failure, FAILED, COMPLETED]

        _harness, pauses = self._frames_session(tmp_path, plan, limit=3, kind="simulated")

        (title,) = [t for t in pauses if "FAILED IN A ROW" in t]
        assert "check the calibration" in title
        assert "display" not in title

    def test_no_limit_never_pauses(self, tmp_path):
        from support import FAILED

        harness = self.harness(tmp_path, [FAILED] * 5 + [COMPLETED], limit=None)
        harness.runner.run()
        assert harness.display.menus == []

    def test_a_limit_below_one_is_refused(self, tmp_path):
        # The harness hands the limit to SessionRunner's constructor, which
        # is what refuses it.
        with pytest.raises(ValueError, match="max_consecutive_failures must be >= 1"):
            SessionHarness(tmp_path, n_trials=1, max_consecutive_failures=0)
