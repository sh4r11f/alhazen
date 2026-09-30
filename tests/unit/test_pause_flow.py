"""The pause, end to end through the runner: what reaches the screen and when.

These are the cases the unit tests of the menu itself cannot cover, because
they are about the runner's loop rather than the menu's contents: that the
menu is actually drawn, that it stays up across a non-terminal choice, and
that an unattended session does not sit at it forever.
"""

from __future__ import annotations

import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.commands import Command
from alhazen.devices.eyetracker import GazeSample
from alhazen.devices.eyetracker.procedures import PROCEDURE_TRIAL_INDEX
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.session.pause import PAUSE_COLOR
from alhazen.testing import FakeClock, FakeDisplay, ScriptedCommands
from support import SCREEN, SessionHarness


def read_trials(harness):
    import csv

    with harness.paths.trials_path.open() as f:
        return list(csv.DictReader(f))


def two_blocks():
    """Two blocks of one trial each: the session takes one block break."""
    import numpy as np

    from alhazen.paradigms.base import Condition, SimpleSequence
    from alhazen.paradigms.blocks import BlockPlan

    def block():
        return SimpleSequence([Condition({"condition": "a"})], rng=np.random.default_rng(0))

    return BlockPlan([block(), block()], trials_per_block=1)


class TimedKeys(ScriptedCommands):
    """Raw keys pressed at simulated times rather than at polls.

    A validation or drift-correction walk polls the keyboard every frame for
    its own keys (SPACE, BACKSPACE, ESC), so a script that hands out one
    batch per poll would feed the walk the keys meant for the menu after it.
    Keys due by the clock go to whoever polls once the clock gets there.
    """

    def __init__(self, clock: FakeClock, batches, presses: list[tuple[float, str]]) -> None:
        super().__init__(batches=batches)
        self._clock = clock
        self._presses = sorted(presses)

    def poll_raw_keys(self) -> list[str]:
        now = self._clock.now()
        due = [key for t, key in self._presses if t <= now]
        self._presses = [(t, key) for t, key in self._presses if t > now]
        return due


class TestTheMenuReachesTheScreen:
    def test_a_pause_draws_the_menu_in_its_own_colour(self, tmp_path):
        commands = ScriptedCommands(batches=[[Command.PAUSE]], raw_keys=[[], ["space"]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands, use_pause_menu=True)

        harness.runner.run()

        assert harness.display.menus, "the pause drew no menu"
        title, body, color = harness.display.menus[0]
        assert title == "PAUSED"
        assert "resume" in body
        # The colour is the part that carries the meaning across a room.
        assert color == PAUSE_COLOR

    def test_an_unattended_session_shows_the_menu_and_carries_on(self, tmp_path):
        """No pause strategy wired: the session must not block, but the log
        and the screen still record that it stopped."""
        commands = ScriptedCommands(batches=[[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands)

        harness.runner.run()

        assert harness.display.menus
        names = [event.name for event in harness.collector.events]
        assert "PAUSED" in names and "RESUMED" in names


class TestTheMenuStaysUpUntilResumeOrQuit:
    def test_calibrating_returns_to_the_menu_instead_of_resuming(self, tmp_path):
        """Before 1.1 the calibrate key calibrated and resumed in one press,
        so an experimenter who wanted to calibrate AND reward had to pause
        twice. The menu now stays up until it is dismissed."""
        # A real tracker double, counting calibrations. Hand-rolling one here
        # meant a stub that satisfied the calibrate path and nothing else.
        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=0.0, gy=0.0, t=0.0))], clock)
        calibrations: list = []
        tracker.calibrate = lambda: calibrations.append(1)  # type: ignore[method-assign]

        commands = ScriptedCommands(
            batches=[[Command.PAUSE]],
            # calibrate, calibrate, then resume: three presses, one pause.
            raw_keys=[[], ["c"], [], ["c"], [], ["space"]],
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            commands=commands,
            use_pause_menu=True,
            tracker=tracker,
            clock=clock,
        )

        harness.runner.run()

        assert len(calibrations) == 2
        # Redrawn after each calibration, so a calibration screen cannot
        # leave the display showing something that is no longer true.
        assert len(harness.display.menus) >= 3

    def test_quitting_ends_the_session(self, tmp_path):
        commands = ScriptedCommands(batches=[[Command.PAUSE]], raw_keys=[[], ["q"]])
        harness = SessionHarness(tmp_path, n_trials=3, commands=commands, use_pause_menu=True)

        harness.runner.run()

        names = [event.name for event in harness.collector.events]
        assert "PAUSED" in names and "RESUMED" not in names
        assert harness.recorder.trials == []


class TestTheProceduresRunFromTheMenu:
    """V and D run a validation and a drift correction through the session's
    eye-tracker monitor, then return to the menu like C does."""

    def test_validate_and_drift_correct_then_resume(self, tmp_path):
        clock = FakeClock()
        # A subject who stares 20 px (half a degree) right of the screen's
        # centre whatever is shown: every target is measured, the centre one
        # with a 0.5° error, and a drift correction has something to correct.
        gaze = GazeSample(gx=SCREEN.width_px / 2 + 20.0, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        # Each procedure takes a few simulated seconds (settle + sample per
        # target, auto-advanced on the simulated display), so the next key
        # is pressed well after the previous walk is over.
        commands = TimedKeys(
            clock,
            batches=[[Command.PAUSE]],
            presses=[(0.0, "v"), (30.0, "d"), (60.0, "space")],
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            commands=commands,
            use_pause_menu=True,
            tracker=tracker,
            clock=clock,
        )

        harness.runner.run()

        monitor = harness.eyetracker
        assert monitor is not None
        validation = monitor.validation
        assert validation is not None and not validation.aborted
        assert len(validation.targets) == 5 and validation.n_missed == 0
        drift = monitor.drift
        assert drift is not None and drift.applied
        assert drift.offset_deg == 0.5
        # The correction the input provider applies from now on is the
        # measured offset, reversed.
        assert monitor.correction.offset == (-20.0, 0.0)
        # Both procedures went on the record, and the session then ran on.
        names = [event.name for event in harness.collector.events]
        assert "VALIDATION" in names and "DRIFT_CORRECTION" in names
        assert "RESUMED" in names
        assert [row["trial_index"] for row in harness.recorder.trials] == [2, 3]
        # Redrawn after each procedure, as after a calibration.
        assert len(harness.display.menus) >= 3


class TestAFailedProcedureIsSaidOnTheRigsOwnScreen:
    """A validation that fails has to say so on the screen the experimenter
    is facing, not only in the log and the browser's notice line. One did
    not, and a session resumed on a calibration its design rejected with
    nobody the wiser."""

    def test_the_menu_comes_back_with_the_verdict_as_its_heading(self, tmp_path):
        clock = FakeClock()
        # Two degrees right of every target, against a one-degree limit.
        gaze = GazeSample(gx=SCREEN.width_px / 2 + 80.0, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = TimedKeys(
            clock, batches=[[Command.PAUSE]], presses=[(0.0, "v"), (30.0, "space")]
        )
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            commands=commands,
            use_pause_menu=True,
            tracker=tracker,
            clock=clock,
        )

        harness.runner.run()

        validation = harness.eyetracker.validation
        assert validation is not None and not validation.accepted
        headings = [title for title, _body, _color in harness.display.menus]
        # The worst target is a far corner the subject never looks at, so the
        # number is large; what matters is that the verdict and the limit led.
        from alhazen.session.pause import WARNING_COLOR

        # How far over, and both ways on.
        warned = [
            (title, color)
            for title, _body, color in harness.display.menus
            if title.startswith("VALIDATION")
        ]
        assert warned, headings
        assert all("ABOVE THE 1° LIMIT" in title for title, _color in warned), warned
        assert all("SPACE resumes on it, C recalibrates" in title for title, _color in warned)
        # A warning, not a fault: its own colour, and SPACE did resume.
        assert all(color == WARNING_COLOR for _title, color in warned)
        # Before the validation the menu carried no heading of its own.
        assert not any("VALIDATION" in h for h in headings[:1])
        # Resuming on it is recorded, with the numbers, and said in the log.
        (resumed,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        shortfall = resumed.payload["on_failed_validation"]
        assert shortfall["threshold_deg"] == 1.0
        assert shortfall["max_error_deg"] == validation.max_error_deg
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "resumed on a validation that did not pass" in log


class TestAValidationThatDidNotPassIsAWarning:
    """The heading says how the validation fell short and offers both ways
    on; it is never a fault that tells the experimenter what they must do."""

    @staticmethod
    def validation(errors):
        from alhazen.devices.eyetracker.procedures import TargetError, ValidationResult

        return ValidationResult(
            targets=tuple(
                TargetError(
                    target_px=(0.0, 0.0),
                    gaze_px=None if error is None else (0.0, 0.0),
                    error_deg=error,
                    n_samples=0 if error is None else 10,
                )
                for error in errors
            ),
            threshold_deg=1.0,
            t=1.0,
        )

    def headings(self, tmp_path, errors):
        """What the pause screen leads with once this validation has run
        from the pause menu, as (warning, fault): the heading when it is in
        the warning colour, and when it is in the fault colour, else None.

        Driven through a session: a pause on trial 1, V on its menu, then
        SPACE on the menu the validation leaves up.
        """
        from alhazen.session.pause import FAULT_COLOR, WARNING_COLOR

        monitor = TestAProcedureThatSucceedsTakesTheHeadingBackDown.StubMonitor(
            [self.validation(errors)]
        )
        actions = iter(["validate", "resume"])
        menus = []

        def on_pause(menu):
            menus.append(menu)
            return next(actions)

        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            commands=ScriptedCommands([[Command.PAUSE]]),
            eyetracker=monitor,
            on_pause=on_pause,
        )
        harness.runner.run()

        assert monitor.validation is not None, "the validation never ran"
        after = menus[1]  # the menu the validation left up
        warning = after.title if after.color == WARNING_COLOR else None
        fault = after.title if after.color == FAULT_COLOR else None
        return warning, fault

    def test_over_the_limit(self, tmp_path):
        warning, fault = self.headings(tmp_path, (0.4, 1.3))
        assert warning == (
            "VALIDATION ABOVE THE 1° LIMIT — worst 1.30° — SPACE resumes on it, C recalibrates"
        )
        assert fault is None

    def test_within_the_limit_but_a_target_missed(self, tmp_path):
        warning, _fault = self.headings(tmp_path, (0.4, None))
        assert warning == (
            "VALIDATION INCOMPLETE — worst 0.40° within the 1° limit, 1 target(s) missed"
            " — SPACE resumes on it, C recalibrates"
        )

    def test_nothing_measured(self, tmp_path):
        warning, _fault = self.headings(tmp_path, (None, None))
        assert warning == "VALIDATION MEASURED NO TARGET — SPACE resumes on it, C recalibrates"

    def test_a_validation_that_passed_heads_nothing(self, tmp_path):
        warning, fault = self.headings(tmp_path, (0.4, 0.6))
        assert (warning, fault) == (None, None)


class TestTheSessionTakesTheBlockBreak:
    """The break between blocks is the session's job: the pause screen comes
    up headed with the block count, in the rest colour rather than the fault
    colour, and stays up until the experimenter resumes."""

    def two_blocks(self):
        import numpy as np

        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.paradigms.blocks import BlockPlan

        def block():
            return SimpleSequence([Condition({"condition": "a"})], rng=np.random.default_rng(0))

        return BlockPlan([block(), block()], trials_per_block=1)

    def test_the_break_is_headed_with_the_block_count_and_waits_for_space(self, tmp_path):
        from alhazen.session.pause import REST_COLOR

        commands = ScriptedCommands(batches=[], raw_keys=[[], [], ["space"]])
        harness = SessionHarness(
            tmp_path, commands=commands, use_pause_menu=True, source=self.two_blocks()
        )
        harness.runner.run()

        (menu,) = harness.display.menus
        title, body, color = menu
        assert title == "BLOCK 1 OF 2 COMPLETE — REST"
        assert color == REST_COLOR
        assert "between blocks" in body
        rows = read_trials(harness)
        assert [r["block"] for r in rows] == ["1", "2"]
        names = harness.collector.names()
        assert names.count("PAUSED") == 1 and names.count("RESUMED") == 1
        (paused,) = [e for e in harness.collector.events if e.name == "PAUSED"]
        assert paused.payload == {"reason": "block_break", "blocks_done": 1, "blocks_total": 2}
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "block 1 of 2 complete: taking the break" in log

    def test_an_unattended_session_takes_the_break_and_carries_on(self, tmp_path):
        harness = SessionHarness(tmp_path, source=self.two_blocks())
        harness.runner.run()
        titles = [title for title, _body, _color in harness.display.menus]
        assert titles == ["BLOCK 1 OF 2 COMPLETE — REST"]
        assert len(read_trials(harness)) == 2


class TestAProcedureThatSucceedsTakesTheHeadingBackDown:
    """The heading a failed procedure puts up was never removed. An
    experimenter who validated (failed), recalibrated and validated again
    (passed) was still looking at a red VALIDATION FAILED, and the pause's
    own heading — a block break's REST — never came back."""

    def validation(self, error_deg, t):
        from alhazen.devices.eyetracker.procedures import TargetError, ValidationResult

        return ValidationResult(
            targets=(
                TargetError(
                    target_px=(0.0, 0.0), gaze_px=(0.0, 0.0), error_deg=error_deg, n_samples=10
                ),
            ),
            threshold_deg=1.0,
            t=t,
        )

    class StubMonitor:
        """Stands in for EyeTrackerMonitor: two validations, the first
        failing and the second passing, and nothing else."""

        def __init__(self, results) -> None:
            self._results = list(results)
            self.calibration = None
            self.drift = None
            self.validation = None
            self.publisher = None

        def validate(self):
            self.validation = self._results.pop(0)
            return self.validation

    def test_the_pauses_own_heading_comes_back(self, tmp_path):
        actions = iter(["validate", "validate", "resume"])
        seen: list[str] = []

        def on_pause(menu):
            seen.append(menu.title)
            return next(actions)

        # The rest is a session's break between two blocks.
        harness = SessionHarness(
            tmp_path,
            source=two_blocks(),
            eyetracker=self.StubMonitor([self.validation(2.3, t=1.0), self.validation(0.4, t=2.0)]),
            on_pause=on_pause,
        )
        harness.runner.run()

        # The break resumed, and the session went on to the second block.
        assert "RESUMED" in harness.collector.names()
        assert len(read_trials(harness)) == 2

        # First the break's own heading; then the failure; then the break's
        # heading again, because the second validation passed.
        assert len(seen) == 3, seen
        assert "REST" in seen[0]
        assert "VALIDATION ABOVE THE 1° LIMIT" in seen[1] and "2.30°" in seen[1]
        assert seen[2] == seen[0], seen


class FakeLiveMonitor:
    """Enough LiveMonitorController for the runner: a URL, somewhere for
    snapshots to go, and a command queue that is always empty — a browser
    nobody has opened, which is what an unattended rig has.

    ``poll_commands`` gives up after ``poll_budget`` calls rather than
    returning [] forever, so a runner that waits for a click that will never
    come fails this suite in a second instead of hanging it.
    """

    url = "http://127.0.0.1:0/"

    def __init__(self, poll_budget: int = 200) -> None:
        self.published: list[tuple[str, str | None]] = []
        self.frames = 0
        self._polls = 0
        self._poll_budget = poll_budget

    def poll_commands(self):
        self._polls += 1
        if self._polls > self._poll_budget:
            raise AssertionError(
                "the runner is still waiting for a live monitor command in a run with no "
                "keyboard wired — an unattended session cannot be resumed by a browser "
                "nobody has open"
            )
        return []

    def publish(self, state) -> None:
        self.published.append((state.get("status"), state.get("message")))

    def publish_camera(self, pixels, t) -> None:
        self.frames += 1

    def poll_settings(self):
        return []

    def save(self, figures_dir, state) -> None:
        self.saved = state

    def stop(self) -> None:
        self.stopped = True


class TestAnUnattendedRunIsNeverLeftWaiting:
    """`live_monitor.enabled` in a rig file turned every pause in an unattended
    run into a hang: the runner asked whether a live monitor existed before it
    asked whether anyone was there to answer. A live monitor is a window onto
    the session, not a person at it. With block breaks that became every
    simulated run of a multi-block experiment — 28 trials and then nothing,
    forever."""

    def two_blocks(self):
        import numpy as np

        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.paradigms.blocks import BlockPlan

        def block():
            return SimpleSequence([Condition({"condition": "a"})], rng=np.random.default_rng(0))

        return BlockPlan([block(), block()], trials_per_block=1)

    def test_the_block_break_resumes_itself_when_the_live_monitor_is_on(self, tmp_path):
        live_monitor = FakeLiveMonitor()
        harness = SessionHarness(tmp_path, source=self.two_blocks(), live_monitor=live_monitor)

        harness.runner.run()

        assert len(read_trials(harness)) == 2, "the session did not get past the break"
        titles = [title for title, _body, _color in harness.display.menus]
        assert titles == ["BLOCK 1 OF 2 COMPLETE — REST"]
        # The browser is told the session carried on, so a live monitor left
        # open on a dry run does not sit on "paused" while trials go by.
        statuses = [status for status, _message in live_monitor.published]
        assert "running" in statuses
        assert any(
            message and "Unattended" in message for _status, message in live_monitor.published
        )

    def test_a_skipped_pause_is_a_warning_not_a_silence(self, tmp_path, caplog):
        """A pause that did not pause is a difference between what the
        session was asked to do and what it did, and the run that finds out
        should be the dry run."""
        import logging

        harness = SessionHarness(tmp_path, source=self.two_blocks(), live_monitor=FakeLiveMonitor())
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            harness.runner.run()
        assert any("nobody to answer it" in record.getMessage() for record in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_a_keyboard_pause_still_goes_through_the_browser(self, tmp_path):
        """The fix must not take the live monitor out of an attended run: with a
        pause strategy wired, the browser is still what resolves the pause."""
        from alhazen.live_monitor.runtime import LiveMonitorCommand

        class OneResume(FakeLiveMonitor):
            def poll_commands(self):
                super().poll_commands()
                return [LiveMonitorCommand(name="resume", request_id="r1")]

        live_monitor = OneResume()
        commands = ScriptedCommands(batches=[[Command.PAUSE]], raw_keys=[[], []])
        harness = SessionHarness(
            tmp_path,
            n_trials=2,
            commands=commands,
            use_pause_menu=True,
            live_monitor=live_monitor,
        )

        harness.runner.run()

        statuses = [status for status, _message in live_monitor.published]
        assert "paused" in statuses, statuses
        assert len(read_trials(harness)) == 2


class TestASimulationsBreakResumesByItself:
    """In simulate mode the break between blocks waited for SPACE whenever a
    keyboard was wired, which it is on a real display: a rehearsal watched on
    the rig's screen sat on BLOCK 1 OF 2 COMPLETE until someone pressed a key.
    With a rest timeout set, a rest nobody resolves in time resumes by itself.
    A key pressed first is still obeyed, and a fault never times out."""

    REST = "BLOCK 1 OF 2 COMPLETE — REST"

    def two_blocks(self):
        import numpy as np

        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.paradigms.blocks import BlockPlan

        def block():
            return SimpleSequence([Condition({"condition": "a"})], rng=np.random.default_rng(0))

        return BlockPlan([block(), block()], trials_per_block=1)

    @staticmethod
    def _break_length(harness):
        (paused,) = [e for e in harness.collector.events if e.name == "PAUSED"]
        (resumed,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        return resumed.t - paused.t

    def test_nobody_pressing_anything_resumes_after_the_wait(self, tmp_path):
        harness = SessionHarness(
            tmp_path, use_pause_menu=True, source=self.two_blocks(), rest_resume_after_s=10.0
        )

        harness.runner.run()

        assert len(read_trials(harness)) == 2, "the session did not get past the break"
        assert self._break_length(harness) >= 10.0
        title, body, _color = harness.display.menus[0]
        assert title == self.REST
        assert "resumes by itself in 10 s" in body
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "resumed by itself after 10 s" in log

    def test_a_key_pressed_before_the_wait_is_over_is_obeyed(self, tmp_path):
        commands = ScriptedCommands(batches=[], raw_keys=[[], ["space"]])
        harness = SessionHarness(
            tmp_path,
            commands=commands,
            use_pause_menu=True,
            source=self.two_blocks(),
            rest_resume_after_s=10.0,
        )

        harness.runner.run()

        assert len(read_trials(harness)) == 2
        assert self._break_length(harness) < 1.0
        assert "resumed by itself" not in harness.paths.log_path.read_text(encoding="utf-8")

    def test_acting_on_the_menu_stops_the_clock(self, tmp_path):
        """A key that is not resume or quit means somebody is there: from then
        on the rest waits for them, and the screen stops promising otherwise."""
        from alhazen.devices.eyetracker.procedures import TargetError, ValidationResult

        class StubMonitor:
            calibration = None
            drift = None
            validation = None
            publisher = None
            has_camera = False

            def validate(self):
                self.validation = ValidationResult(
                    targets=(
                        TargetError(
                            target_px=(0.0, 0.0), gaze_px=(0.0, 0.0), error_deg=0.2, n_samples=10
                        ),
                    ),
                    threshold_deg=1.0,
                    t=0.0,
                )
                return self.validation

        seen = []

        def on_pause(menu):
            seen.append(menu.subtitle)
            return "resume"

        # V is the first key the break's clock reads: nothing polls the raw
        # keys during a trial.
        monitor = StubMonitor()
        harness = SessionHarness(
            tmp_path,
            commands=ScriptedCommands(batches=[], raw_keys=[["v"]]),
            source=self.two_blocks(),
            eyetracker=monitor,
            rest_resume_after_s=10.0,
            on_pause=on_pause,
        )
        harness.runner.run()

        # The break resumed, and the session went on to the second block.
        assert "RESUMED" in harness.collector.names()
        assert len(read_trials(harness)) == 2
        assert monitor.validation is not None, "the key was not obeyed"
        assert len(seen) == 1, "the attended loop never took over from the clock"
        assert "resumes by itself" not in seen[0]

    def test_a_fault_never_resumes_by_itself(self, tmp_path):
        from alhazen.config.models import RewardPulses
        from alhazen.task.reward_policy import RewardPolicy
        from alhazen.testing import ScriptedReward

        titles = []
        # The pump fails the one trial's pay: a fault pause, in a session
        # whose rests would resume by themselves after 10 s.
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            reward=ScriptedReward(fail=[1]),
            reward_policy=RewardPolicy(by_outcome={"COMPLETED": RewardPulses()}),
            rest_resume_after_s=10.0,
            on_pause=lambda menu: (titles.append(menu.title), "resume")[1],
        )
        harness.runner.run()

        (failed,) = [e for e in harness.collector.events if e.name == "REWARD_FAILED"]
        (resumed,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        assert titles == ["REWARD FAILURE — check the pump"]
        # Resumed by the person's SPACE at once, not by a 10 s clock.
        assert resumed.t - failed.t < 1.0

    def test_with_the_live_monitor_on_the_break_still_resumes_by_itself(self, tmp_path):
        live_monitor = FakeLiveMonitor(poll_budget=5000)
        harness = SessionHarness(
            tmp_path,
            use_pause_menu=True,
            source=self.two_blocks(),
            live_monitor=live_monitor,
            rest_resume_after_s=10.0,
        )

        harness.runner.run()

        assert len(read_trials(harness)) == 2
        assert self._break_length(harness) >= 10.0
        assert any(
            message and "by itself" in message for _status, message in live_monitor.published
        )

    def test_with_no_keyboard_wired_it_still_resumes_at_once(self, tmp_path):
        """Unattended runs already resumed immediately, since nobody can act.
        The wait is only for a run someone could be watching."""
        harness = SessionHarness(tmp_path, source=self.two_blocks(), rest_resume_after_s=10.0)

        harness.runner.run()

        assert self._break_length(harness) < 1.0
        _title, body, _color = harness.display.menus[0]
        assert "resumes by itself" not in body


def validating_blocks():
    """Two blocks of one trial each, whose break ends with a validation of
    the eye tracker (BlockConfig.validate_after_break)."""
    import numpy as np

    from alhazen.paradigms.base import Condition, SimpleSequence
    from alhazen.paradigms.blocks import BlockPlan

    def block():
        return SimpleSequence([Condition({"condition": "a"})], rng=np.random.default_rng(0))

    return BlockPlan([block(), block()], trials_per_block=1, validate_after_break=True)


# The rig's eye-tracker config for these sessions: validation targets packed
# within half a degree of the screen's centre (2% of the screen), so a
# subject staring at the centre passes a 1° limit and one staring 2° to the
# side does not.
NEAR_CENTRE = EyeTrackerConfig(
    backend="scripted", calibration_area=0.02, validate_after_calibration=False
)


class OffOnceTracker(ScriptedTracker):
    """A subject who looks 2° right of the screen's centre through the first
    validation walk, and at the centre from the second on: a validation that
    fails once and passes when it is run again.

    A walk opens a recording segment under the procedures' own trial index
    (0), which is how this tracker counts them; trials start at 1."""

    OFF_PX = 80.0  # 2° at the test screen's 40 px per degree

    def get_gaze(self) -> GazeSample | None:
        walks = sum(1 for index, _status in self.trials_started if index == PROCEDURE_TRIAL_INDEX)
        offset = self.OFF_PX if walks <= 1 else 0.0
        return GazeSample(
            gx=SCREEN.width_px / 2 + offset, gy=SCREEN.height_px / 2, t=self._clock.now()
        )


class TestTheBreakEndsWithAValidation:
    """`validate_after_break`: the design validates the calibration between
    blocks, and the pilot showed nothing made that happen. SPACE at the end of
    the break now runs the V key's validation before the next block; one that
    fails keeps the break up under its amber heading until the experimenter
    resumes from there. These run whole sessions with a subject whose first
    validation fails and whose second passes."""

    def run(self, tmp_path, presses, **harness):
        clock = FakeClock()
        commands = TimedKeys(clock, batches=[], presses=presses)
        session = SessionHarness(
            tmp_path,
            commands=commands,
            use_pause_menu=True,
            source=validating_blocks(),
            tracker=OffOnceTracker([], clock),
            clock=clock,
            eyetracker_config=NEAR_CENTRE,
            **harness,
        )
        session.runner.run()
        return session

    def test_a_failed_validation_holds_the_break_until_one_passes(self, tmp_path):
        from alhazen.session.pause import REST_COLOR, WARNING_COLOR

        # SPACE ends the rest and runs the validation, which fails; V runs it
        # again, which passes; SPACE then starts block 2. Each walk takes a
        # few simulated seconds, so each key comes well after the last one.
        harness = self.run(tmp_path, [(0.0, "space"), (30.0, "v"), (60.0, "space")])

        validations = [e for e in harness.collector.events if e.name == "VALIDATION"]
        assert [e.payload["accepted"] for e in validations] == [False, True]
        # The break is one pause: PAUSED, the two validations, one RESUMED —
        # which records no failed validation, since the last one passed — and
        # only then block 2's first trial.
        names = harness.collector.names()
        paused = names.index("PAUSED")
        resumed = names.index("RESUMED")
        assert names.count("RESUMED") == 1
        assert paused < names.index("VALIDATION") < resumed
        assert names[resumed:].count("TRIAL_START") == 1
        (event,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        assert "on_failed_validation" not in event.payload
        rows = read_trials(harness)
        assert [r["block"] for r in rows] == ["1", "2"]

        # What the rig's screen showed: the rest, whose SPACE row said it
        # validates first; the failure, in amber, with both ways on; the rest
        # again once the validation passed, whose SPACE now just resumes.
        (rest, rest_body, _), (failed, _, failed_color), (back, back_body, back_color) = (
            harness.display.menus[:3]
        )
        assert rest == back == "BLOCK 1 OF 2 COMPLETE — REST"
        assert "validate the calibration, then resume" in rest_body
        assert failed.startswith("VALIDATION ABOVE THE 1° LIMIT")
        assert failed.endswith("SPACE resumes on it, C recalibrates")
        assert failed_color == WARNING_COLOR
        assert back_color == REST_COLOR
        assert "validate the calibration, then resume" not in back_body

        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "the eye tracker's calibration is validated before the next block" in log
        assert "validating the eye tracker's calibration before resuming" in log
        assert log.index("validation FAILED") < log.index("validation passed")
        assert "resumed on a validation that did not pass" not in log

    def test_space_on_the_failed_heading_resumes_on_it_and_says_so(self, tmp_path):
        harness = self.run(tmp_path, [(0.0, "space"), (30.0, "space")])

        validations = [e for e in harness.collector.events if e.name == "VALIDATION"]
        assert [e.payload["accepted"] for e in validations] == [False]
        (event,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        assert event.payload["on_failed_validation"]["threshold_deg"] == 1.0
        assert len(read_trials(harness)) == 2
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "resumed on a validation that did not pass, as the experimenter chose" in log

    def test_an_unattended_session_validates_at_the_break_and_carries_on(self, tmp_path):
        # No keyboard (a headless simulation): the break resumes at once, its
        # validation runs by itself and goes on the record, and nothing waits.
        clock = FakeClock()
        harness = SessionHarness(
            tmp_path,
            source=validating_blocks(),
            tracker=OffOnceTracker([], clock),
            clock=clock,
            eyetracker_config=NEAR_CENTRE,
        )
        harness.runner.run()

        assert len(read_trials(harness)) == 2
        (validation,) = [e for e in harness.collector.events if e.name == "VALIDATION"]
        assert validation.payload["advance"] == "auto"
        (event,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        assert "on_failed_validation" in event.payload
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "with nobody there to decide" in log

    def test_a_runner_with_no_eye_tracker_to_validate_is_refused(self, tmp_path):
        # Enforced where every session passes, however it was wired: the
        # runner, before anything is written.
        from alhazen.errors import ConfigError

        with pytest.raises(ConfigError, match="no eye tracker to validate"):
            SessionHarness(tmp_path, source=validating_blocks())

    def test_a_flag_that_is_not_a_bool_is_refused(self, tmp_path):
        import numpy as np

        from alhazen.paradigms.base import Condition, SimpleSequence

        class Odd(SimpleSequence):
            # Truthy, and not what anybody meant by switching it on.
            validate_after_break = "no"

        with pytest.raises(TypeError, match="must be True or False"):
            SessionHarness(
                tmp_path, source=Odd([Condition({"c": "a"})], rng=np.random.default_rng(0))
            )

    def test_a_break_that_asks_for_none_takes_none(self, tmp_path):
        clock = FakeClock()
        harness = SessionHarness(
            tmp_path,
            commands=TimedKeys(clock, batches=[], presses=[(0.0, "space")]),
            use_pause_menu=True,
            source=two_blocks(),
            tracker=OffOnceTracker([], clock),
            clock=clock,
        )
        harness.runner.run()

        assert "VALIDATION" not in harness.collector.names()
        assert len(read_trials(harness)) == 2


class TestASimulationsBreakValidationCannotHang:
    """Simulate mode on a real display: a keyboard is wired, so the rest waits
    10 s for somebody and then resumes by itself. The validation after it
    must not then wait for a SPACE at every target — the rig's own advance
    mode, "manual" by default, which the display's kind no longer overrides
    here — nor for a SPACE under a failed heading nobody will read."""

    class RenderingDisplay(FakeDisplay):
        """A FakeDisplay that reports itself as a real window, so nothing
        treats it as the simulated display that forces automatic advance."""

        kind = "psychopy"

    class NobodyThere(ScriptedCommands):
        """No key is ever pressed. A session still asking for one after ten
        simulated minutes is hung, and fails here instead of hanging the
        suite."""

        def __init__(self, clock: FakeClock) -> None:
            super().__init__()
            self._clock = clock

        def poll_raw_keys(self) -> list[str]:
            if self._clock.now() > 600.0:
                raise AssertionError(
                    "still waiting for a key after 600 simulated seconds: nobody is going to "
                    "press one in a simulation"
                )
            return []

    def test_the_rest_the_validation_and_its_failure_all_end_by_themselves(
        self, tmp_path, monkeypatch
    ):
        from alhazen.devices.eyetracker import procedures
        from alhazen.testing import FakeStimulus

        # A real window's fixation point needs PsychoPy; the walk's target is
        # drawn through this factory, and a recording stand-in does as well.
        monkeypatch.setattr(procedures, "make_fixation", lambda *args: FakeStimulus("target"))
        clock = FakeClock()
        harness = SessionHarness(
            tmp_path,
            commands=self.NobodyThere(clock),
            use_pause_menu=True,
            source=validating_blocks(),
            tracker=OffOnceTracker([], clock),
            clock=clock,
            display=self.RenderingDisplay(clock, 1 / 60),
            # "manual" is the default; said here because it is the point.
            eyetracker_config=NEAR_CENTRE.model_copy(update={"calibration_advance": "manual"}),
            rest_resume_after_s=10.0,
        )

        harness.runner.run()

        assert len(read_trials(harness)) == 2, "the session did not get past the break"
        (validation,) = [e for e in harness.collector.events if e.name == "VALIDATION"]
        assert validation.payload["advance"] == "auto"
        assert validation.payload["accepted"] is False
        (event,) = [e for e in harness.collector.events if e.name == "RESUMED"]
        assert "on_failed_validation" in event.payload
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "resumed by itself after 10 s" in log
        assert "with nobody there to decide" in log
