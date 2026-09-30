"""PauseController: resolving a pause, without a runner.

The pause flow is pinned through whole sessions in test_pause_flow.py and
test_pause_menu.py (and the live monitor's side in test_live_monitor.py). These
drive the controller directly with the runner's hooks replaced by
recorders, which is enough to pin how a pause ends: resumed, by itself, or
quit, and what a menu choice that is neither reaches.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from alhazen.core.commands import Command
from alhazen.session.pause import PauseMenu
from alhazen.session.pause_control import PauseController
from alhazen.testing import FakeClock, FakeDisplay, ScriptedCommands


class Pauses:
    """A PauseController on a fake display and clock, with the runner's
    publisher, emitter and stage-command handler recorded."""

    def __init__(
        self,
        answers: list[str] | None = None,
        *,
        raw_keys: list[list[str]] | None = None,
        rest_resume_after_s: float | None = None,
        monitor: Any = None,
    ) -> None:
        """``monitor`` stands in for the session's EyeTrackerMonitor; None
        is a session with no eye tracker."""
        self.clock = FakeClock()
        self.display = FakeDisplay(self.clock, 1 / 60)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[Command] = []
        self.menus: list[PauseMenu] = []
        self.published: list[tuple[str, str | None]] = []
        # None when no keyboard is wired: an unattended run.
        on_pause = None
        if answers is not None:
            script = iter(answers)

            def on_pause(menu: PauseMenu) -> str:
                self.menus.append(menu)
                return next(script)

        self.controller = PauseController(
            display=self.display,
            clock=self.clock,
            commands=ScriptedCommands(raw_keys=raw_keys),
            wait=self.clock.advance,
            on_pause=on_pause,
            rest_resume_after_s=rest_resume_after_s,
            eyetracker=monitor,
            live_monitor=None,
            has_training=True,
            manual_reward=None,
            manual_reward_payload={},
            publish=lambda status, message: self.published.append((status, message)),
            last_message=lambda: None,
            emit_session_event=lambda name, payload: self.events.append((name, payload)),
            on_session_command=self.commands.append,
        )


class TestHowAPauseEnds:
    def test_an_unattended_pause_resumes_at_once_with_its_menu_drawn(self):
        pauses = Pauses()
        assert pauses.controller.handle({}, fault="REWARD FAILURE — check the pump")
        assert pauses.display.menus[-1][0].startswith("REWARD FAILURE")
        assert pauses.events == [("RESUMED", {})]

    def test_resume_goes_on(self):
        pauses = Pauses(["resume"])
        assert pauses.controller.handle({})
        assert pauses.events == [("RESUMED", {})]

    def test_quit_stops_without_a_resumed_event(self):
        pauses = Pauses(["quit"])
        assert not pauses.controller.handle({})
        assert pauses.events == []

    def test_a_rest_nobody_answers_resumes_by_itself(self):
        pauses = Pauses([], rest_resume_after_s=0.5)
        assert pauses.controller.handle({}, rest="BLOCK 1 OF 2 COMPLETE — REST")
        assert pauses.clock.now() >= 0.5
        assert pauses.events == [("RESUMED", {})]

    def test_a_key_during_a_rest_cancels_its_timeout(self):
        # SPACE is resume; anything pressed before the deadline is somebody
        # at the rig, and the rest then waits for them.
        pauses = Pauses([], raw_keys=[["space"]], rest_resume_after_s=0.5)
        assert pauses.controller.handle({}, rest="BLOCK 1 OF 2 COMPLETE — REST")
        assert pauses.clock.now() < 0.5


class TestTheMenusOtherChoices:
    def test_a_stage_key_reaches_the_runners_stage_handler_and_the_menu_stays_up(self):
        pauses = Pauses(["promote_stage", "resume"])
        assert pauses.controller.handle({})
        assert pauses.commands == [Command.PROMOTE_STAGE]
        assert len(pauses.menus) == 2

    def test_a_procedure_with_no_tracker_wired_is_said_not_run(self, caplog):
        pauses = Pauses(["calibrate", "resume"])
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            assert pauses.controller.handle({})
        assert "calibrate requested while paused, but no eye tracker is wired" in caplog.text
        assert pauses.events == [("RESUMED", {})]

    def test_an_unknown_action_is_logged_not_ignored(self, caplog):
        pauses = Pauses(["frobnicate", "resume"])
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            assert pauses.controller.handle({})
        assert "unhandled pause action 'frobnicate'" in caplog.text


REST = "BLOCK 1 OF 2 COMPLETE — REST"


def validation(worst_deg: float | None, *, aborted: bool = False, t: float = 1.0) -> Any:
    """A validation over one target, against a 1° limit: ``worst_deg`` is its
    error, None a missed target."""
    from alhazen.devices.eyetracker.procedures import TargetError, ValidationResult

    return ValidationResult(
        targets=(
            TargetError(
                target_px=(0.0, 0.0),
                gaze_px=None if worst_deg is None else (0.0, 0.0),
                error_deg=worst_deg,
                n_samples=0 if worst_deg is None else 10,
            ),
        ),
        threshold_deg=1.0,
        t=t,
        aborted=aborted,
    )


class ScriptedMonitor:
    """Stands in for EyeTrackerMonitor. Each validate() hands out the next
    scripted result and records how it was asked to advance (None: as the
    rig says); calibrate() is a calibration that takes, and so clears the
    validation, as the real monitor's does. ``held`` is the validation the
    monitor already holds when the pause begins."""

    def __init__(self, results: list[Any], held: Any = None) -> None:
        self._results = list(results)
        self.validation = held
        self.calibration: Any = None
        self.drift = None
        self.advances: list[str | None] = []

    def validate(self, advance: str | None = None) -> Any:
        self.advances.append(advance)
        self.validation = self._results.pop(0)
        return self.validation

    def calibrate(self) -> Any:
        from alhazen.devices.eyetracker.protocol import CalibrationResult

        self.calibration = CalibrationResult(
            ok=True, layout="HV5", n_targets=5, eye="left", advance="manual", t=2.0
        )
        self.validation = None
        return self.calibration


class TestAResumeThatOwesAValidation:
    """A block break under `validate_after_break`: resuming runs the V key's
    validation first. One that passes resumes; one that does not waits under
    its amber heading until the experimenter resumes on it; one abandoned with
    ESC brings the break back, and SPACE validates again. A validation run from
    the menu during the pause counts, one from before it does not, and nothing
    can hang where nobody is there to answer."""

    @staticmethod
    def handle(pauses: Pauses) -> bool:
        return pauses.controller.handle({}, rest=REST, validate_before_resuming=True)

    def test_one_that_passes_resumes_at_once(self):
        monitor = ScriptedMonitor([validation(0.4)])
        pauses = Pauses(["resume"], monitor=monitor)
        assert self.handle(pauses)
        assert len(monitor.advances) == 1
        assert pauses.events == [("RESUMED", {})]
        # The row the experimenter pressed said what SPACE would do.
        assert pauses.menus[0].now[0].label == "validate the calibration, then resume"

    def test_one_that_does_not_pass_waits_under_its_heading_and_space_resumes_on_it(self):
        from alhazen.session.pause import WARNING_COLOR

        monitor = ScriptedMonitor([validation(2.3)])
        pauses = Pauses(["resume", "resume"], monitor=monitor)
        assert self.handle(pauses)
        # One validation, not two: the second SPACE answers the first.
        assert len(monitor.advances) == 1
        after = pauses.menus[1]
        assert after.title == (
            "VALIDATION ABOVE THE 1° LIMIT — worst 2.30° — SPACE resumes on it, C recalibrates"
        )
        assert after.color == WARNING_COLOR
        assert after.now[0].label == "resume"
        ((name, payload),) = pauses.events
        assert name == "RESUMED"
        assert payload["on_failed_validation"]["max_error_deg"] == 2.3

    def test_an_abandoned_one_brings_the_break_back_and_space_validates_again(self):
        monitor = ScriptedMonitor([validation(None, aborted=True), validation(0.4)])
        pauses = Pauses(["resume", "resume"], monitor=monitor)
        assert self.handle(pauses)
        assert len(monitor.advances) == 2
        assert pauses.menus[1].title == REST
        assert pauses.menus[1].now[0].label == "validate the calibration, then resume"
        assert pauses.events == [("RESUMED", {})]

    def test_one_run_from_the_menu_during_the_pause_counts(self):
        monitor = ScriptedMonitor([validation(0.4)])
        pauses = Pauses(["validate", "resume"], monitor=monitor)
        assert self.handle(pauses)
        assert len(monitor.advances) == 1  # V's own, and none again on SPACE
        assert pauses.menus[1].title == REST
        assert pauses.menus[1].now[0].label == "resume"

    def test_one_from_before_the_pause_does_not(self):
        monitor = ScriptedMonitor([validation(0.4)], held=validation(0.3, t=0.5))
        pauses = Pauses(["resume"], monitor=monitor)
        assert self.handle(pauses)
        assert len(monitor.advances) == 1

    def test_a_calibration_during_the_pause_voids_what_was_validated(self):
        # V passes, then C replaces the model it measured: SPACE validates
        # the new one before anything resumes.
        monitor = ScriptedMonitor([validation(0.4), validation(0.5)])
        pauses = Pauses(["validate", "calibrate", "resume"], monitor=monitor)
        assert self.handle(pauses)
        assert len(monitor.advances) == 2
        assert pauses.menus[2].now[0].label == "validate the calibration, then resume"

    def test_at_the_rig_the_walk_advances_as_the_rig_says(self):
        monitor = ScriptedMonitor([validation(0.4)])
        pauses = Pauses(["resume"], monitor=monitor)
        assert self.handle(pauses)
        assert monitor.advances == [None]

    def test_unattended_it_still_runs_by_itself_and_resumes_on_what_it_found(self, caplog):
        monitor = ScriptedMonitor([validation(2.3)])
        pauses = Pauses(monitor=monitor)  # no keyboard wired
        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            assert self.handle(pauses)
        # "auto": nobody is there to accept a target, so a manual walk
        # would never end.
        assert monitor.advances == ["auto"]
        ((_name, payload),) = pauses.events
        assert payload["on_failed_validation"]["max_error_deg"] == 2.3
        assert "once the calibration it asks for is validated" in caplog.text
        assert "with nobody there to decide" in caplog.text
        assert "as the experimenter chose" not in caplog.text

    def test_a_rest_that_ends_by_itself_validates_by_itself_and_goes_on(self, caplog):
        # A simulation's break: a keyboard is wired, nobody presses anything.
        monitor = ScriptedMonitor([validation(2.3)])
        pauses = Pauses([], rest_resume_after_s=0.5, monitor=monitor)
        with caplog.at_level(logging.INFO, logger="alhazen.session.runner"):
            assert self.handle(pauses)
        assert pauses.clock.now() >= 0.5
        assert monitor.advances == ["auto"]
        # No second wait under the failed heading: nobody is there to read it.
        assert pauses.menus == []
        assert "resumed by itself after 0.5 s" in caplog.text
        assert "with nobody there to decide" in caplog.text

    def test_in_a_simulation_somebody_who_pressed_space_is_waited_for(self):
        # SPACE before the deadline means somebody is at the rig: the walk
        # still advances by itself (a simulation's subject accepts nothing),
        # but a validation that did not pass waits for them.
        monitor = ScriptedMonitor([validation(2.3)])
        pauses = Pauses(["resume"], raw_keys=[["space"]], rest_resume_after_s=0.5, monitor=monitor)
        assert self.handle(pauses)
        assert monitor.advances == ["auto"]
        assert pauses.menus[0].title.startswith("VALIDATION ABOVE THE 1° LIMIT")
        assert pauses.clock.now() < 0.5
        ((_name, payload),) = pauses.events
        assert "on_failed_validation" in payload

    def test_a_pause_that_owes_nothing_resumes_as_it_always_did(self):
        monitor = ScriptedMonitor([])
        pauses = Pauses(["resume"], monitor=monitor)
        assert pauses.controller.handle({}, rest=REST)
        assert monitor.advances == []
        assert pauses.menus[0].now[0].label == "resume"

    def test_without_an_eye_tracker_it_is_refused_not_skipped(self):
        pauses = Pauses(["resume"])
        with pytest.raises(ValueError, match="needs an eye tracker"):
            self.handle(pauses)
        assert pauses.events == []
