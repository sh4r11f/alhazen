"""The pause, end to end through the runner: what reaches the screen and when.

These are the cases the unit tests of the menu itself cannot cover, because
they are about the runner's loop rather than the menu's contents: that the
menu is actually drawn, that it stays up across a non-terminal choice, and
that an unattended session does not sit at it forever.
"""

from __future__ import annotations

from alhazen.core.commands import Command
from alhazen.devices.eyetracker import GazeSample
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.session.pause import PAUSE_COLOR
from alhazen.testing import FakeClock, ScriptedCommands
from support import SessionHarness


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
