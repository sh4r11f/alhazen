"""The live monitor's guide to an eye-tracker procedure
(session/procedure_guide.py), and how the runner publishes it.

The guide is read off the ``(stage, detail)`` reports the trackers already
make, so these tests feed it those reports in the trackers' own words; the
monitor-level behaviour (when it is published, kept and replaced) is in
test_session_eyetracker.py, next to the monitor's other publishing tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from alhazen.core.commands import Command
from alhazen.devices.eyetracker.procedures import DriftResult, TargetError, ValidationResult
from alhazen.devices.eyetracker.protocol import CalibrationResult
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.session.eyetracker import PROCEDURE_STATUS
from alhazen.session.procedure_guide import (
    ABORTED_CHECKS,
    FAILED_CHECKS,
    TARGET_INSTRUCTIONS,
    ProcedureGuide,
    ended_calibration,
    ended_drift,
    ended_validation,
    failed_with_error,
    progressed,
    split_check,
    started,
)
from alhazen.testing import ScriptedCommands
from support import SessionHarness

# The TRACKPixx3's reports, as viewpixx.py writes them.
TPX_GUIDE = "eyes: both tracked · press SPACE to start, P or ESC to go back to the pause menu"
TPX_NO_EYE = "NO EYE IN THE CAMERA IMAGE — check position, focus and LED (accept is refused)"
TPX_FAILED_NOTE = (
    "the device reports NO calibration after the walk — check the camera sees the eyes "
    "(position, focus, LED), then calibrate again"
)


def report(guide: ProcedureGuide, stage: str, detail: str, advance: str = "manual"):
    return progressed(guide, stage, detail, advance=advance, t=2.0)


def calibration(ok: bool | None, *, aborted: bool = False, note: str = "") -> CalibrationResult:
    return CalibrationResult(
        ok=ok,
        layout="HV5",
        n_targets=5,
        eye="both",
        advance="manual",
        t=3.0,
        note=note,
        aborted=aborted,
    )


def validation(worst: float, *, aborted: bool = False) -> ValidationResult:
    targets = (TargetError(target_px=(0.0, 0.0), gaze_px=(1.0, 0.0), error_deg=worst, n_samples=9),)
    return ValidationResult(targets=targets, threshold_deg=1.0, t=3.0, aborted=aborted)


def drift(applied: bool, note: str = "") -> DriftResult:
    return DriftResult((0.0, 0.0), None, None, None, 2.0, applied, 3.0, note)


class TestTheReportsBecomeAGuide:
    def test_a_new_procedure_has_its_heading_and_nothing_else_yet(self) -> None:
        state = started("calibration", 1.0).as_state()
        assert state["heading"] == "Calibration"
        assert state["kind"] == "calibration"
        assert state["outcome"] == "running"
        assert state["instruction"] == "Starting"
        assert state["step"] is None and state["n"] is None
        assert state["failure"] is None and state["check"] is None
        assert started("validation", 1.0).heading == "Validation"
        assert started("drift_correction", 1.0).heading == "Drift correction"

    def test_the_trackpixx_guide_waits_for_space_with_the_eye_line(self) -> None:
        guide = report(started("calibration", 1.0), "calibration guide", TPX_GUIDE)
        assert guide.instruction == "Press SPACE to start, P or ESC to go back to the pause menu"
        assert guide.status_line == "eyes: both tracked"
        assert guide.status_alert is False
        assert guide.step is None
        assert guide.t == 2.0

    def test_a_target_is_the_step_and_the_eye_line_the_status(self) -> None:
        guide = report(
            started("calibration", 1.0), "calibrating", "target 2 of 9 · eyes: left only"
        )
        assert (guide.step, guide.n) == (2, 9)
        assert guide.status_line == "eyes: left only"
        assert guide.instruction == TARGET_INSTRUCTIONS["manual"]
        assert (
            report(guide, "calibrating", "target 3 of 9", "auto").instruction
            == (TARGET_INSTRUCTIONS["auto"])
        )

    def test_no_eye_in_the_image_is_an_alert(self) -> None:
        guide = report(started("calibration", 1.0), "calibrating", f"target 1 of 5 · {TPX_NO_EYE}")
        assert guide.status_alert is True
        assert guide.status_line == TPX_NO_EYE

    def test_the_eyelink_guide_and_its_host_pc_stage(self) -> None:
        guide = report(
            started("calibration", 1.0),
            "calibration guide",
            "press SPACE to open the Host PC setup, ESC to skip",
        )
        assert guide.instruction == "Press SPACE to open the Host PC setup, ESC to skip"
        assert guide.status_line is None
        guide = report(guide, "calibrating", "on the Host PC's setup screen")
        assert guide.instruction == "Calibrating on the Host PC's setup screen"
        assert guide.step is None

    def test_the_eyelink_camera_image_is_an_instruction_under_calibration(self) -> None:
        guide = report(
            started("calibration", 1.0), "camera image", "Left eye · shown on the live monitor"
        )
        assert guide.heading == "Calibration"
        assert guide.instruction == "Camera image: Left eye"
        assert guide.status_line == "Shown on the live monitor"

    def test_validation_and_drift_correction_walk_their_targets(self) -> None:
        guide = report(started("validation", 1.0), "validating", "target 4 of 5", "auto")
        assert (guide.step, guide.n, guide.instruction) == (4, 5, TARGET_INSTRUCTIONS["auto"])
        guide = report(started("drift_correction", 1.0), "drift correcting", "target 1 of 1")
        assert (guide.step, guide.n) == (1, 1)

    def test_the_trackpixx_failure_report_is_shown_at_once(self) -> None:
        guide = report(
            report(started("calibration", 1.0), "calibrating", "target 5 of 5"),
            "calibration failed",
            "the tracker reports no calibration",
        )
        assert guide.outcome == "failed"
        assert guide.failure == "The tracker reports no calibration"
        assert guide.instruction == ""

    def test_an_unknown_stage_shows_its_detail(self) -> None:
        guide = report(started("calibration", 1.0), "warming up", "the camera is starting")
        assert guide.instruction == "The camera is starting"
        assert guide.stage == "warming up"

    def test_the_monitor_s_own_start_report_says_starting(self) -> None:
        guide = report(started("validation", 1.0), "validating", "starting")
        assert guide.instruction == "Starting"


class TestTheResultEndsIt:
    def test_a_calibration_that_took_or_whose_outcome_is_unknown_takes_the_panel_down(self):
        guide = started("calibration", 1.0)
        assert ended_calibration(guide, calibration(True)) is None
        assert ended_calibration(guide, calibration(None)) is None

    def test_a_failed_calibration_says_what_failed_and_what_to_check(self) -> None:
        ended = ended_calibration(
            started("calibration", 1.0), calibration(False, note=TPX_FAILED_NOTE)
        )
        assert ended is not None
        assert ended.outcome == "failed"
        assert ended.failure == "The device reports NO calibration after the walk"
        assert (
            ended.check
            == "Check the camera sees the eyes (position, focus, LED), then calibrate again"
        )
        assert ended.instruction == ""
        assert ended.t == 3.0

    def test_a_note_without_a_check_gets_the_kind_s_check(self) -> None:
        ended = ended_calibration(
            started("calibration", 1.0), calibration(False, note="Host PC: POOR")
        )
        assert ended is not None
        assert (ended.failure, ended.check) == ("Host PC: POOR", FAILED_CHECKS["calibration"])

    def test_an_aborted_calibration_is_kept_as_stopped(self) -> None:
        note = "skipped at the guide; the tracker keeps its previous calibration"
        ended = ended_calibration(
            started("calibration", 1.0), calibration(None, aborted=True, note=note)
        )
        assert ended is not None
        assert ended.outcome == "aborted"
        assert ended.failure == "Skipped at the guide; the tracker keeps its previous calibration"
        assert ended.check == ABORTED_CHECKS["calibration"]

    def test_validation(self) -> None:
        guide = started("validation", 1.0)
        assert ended_validation(guide, validation(0.4)) is None
        failed = ended_validation(guide, validation(2.3))
        assert failed is not None and failed.outcome == "failed"
        assert failed.failure == "Validation FAILED: mean 2.30°, worst 2.30° (limit 1°)"
        assert failed.check == FAILED_CHECKS["validation"]
        stopped = ended_validation(guide, validation(0.4, aborted=True))
        assert stopped is not None and stopped.outcome == "aborted"
        assert stopped.check == ABORTED_CHECKS["validation"]

    def test_drift_correction(self) -> None:
        guide = started("drift_correction", 1.0)
        assert ended_drift(guide, drift(True)) is None
        stopped = ended_drift(guide, drift(False, "aborted"))
        assert stopped is not None and stopped.outcome == "aborted"
        refused = ended_drift(guide, drift(False, "too large for a drift — recalibrate"))
        assert refused is not None and refused.outcome == "failed"
        assert (
            refused.failure == "Drift correction not applied: too large for a drift — recalibrate"
        )
        assert refused.check == FAILED_CHECKS["drift_correction"]

    def test_an_error_is_a_failure(self) -> None:
        ended = failed_with_error(started("calibration", 1.0), RuntimeError("usb gone"), 4.0)
        assert (ended.outcome, ended.failure, ended.t) == (
            "failed",
            "Stopped with an error: usb gone",
            4.0,
        )

    @pytest.mark.parametrize(
        ("note", "expected"),
        [
            ("a — check b", ("A", "Check b")),
            ("a; check b", ("A", "Check b")),
            ("nothing to check here", ("Nothing to check here", None)),
            ("— check only", ("— check only", None)),
        ],
    )
    def test_split_check(self, note: str, expected: tuple[str, str | None]) -> None:
        assert split_check(note) == expected

    def test_the_state_is_plain_json(self) -> None:
        import json

        ended = ended_calibration(
            started("calibration", 1.0), calibration(False, note=TPX_FAILED_NOTE)
        )
        assert ended is not None
        state = json.loads(json.dumps(ended.as_state()))
        assert set(state) == {
            "kind",
            "heading",
            "stage",
            "instruction",
            "status_line",
            "status_alert",
            "step",
            "n",
            "outcome",
            "failure",
            "check",
            "t",
        }


# ----------------------------------------------------------------------
# Through the runner: what the live monitor is sent
# ----------------------------------------------------------------------


class FailingThenGoodTracker(ScriptedTracker):
    """Calibrates as told by ``outcomes``, one per call, reporting the
    TRACKPixx3's stages on the way."""

    def __init__(self, clock, outcomes: list[CalibrationResult]) -> None:
        super().__init__([], clock)
        self._outcomes = list(outcomes)
        self._hook: Any = None

    def set_progress_hook(self, hook: Any) -> None:
        self._hook = hook

    def calibrate(self) -> CalibrationResult:  # type: ignore[override]
        if self._hook is not None:
            self._hook("calibration guide", TPX_GUIDE)
            self._hook("calibrating", "target 1 of 5 · eyes: both tracked")
        return self._outcomes.pop(0)


class RecordingLiveMonitor:
    """A live monitor for the runner that keeps every state it is sent. Its
    page sends nothing: the pause is answered with the rig's keys. A runner
    that keeps waiting fails after ``poll_budget`` polls instead of hanging."""

    url = "http://127.0.0.1:0/"

    def __init__(self, poll_budget: int = 2000) -> None:
        self.states: list[dict[str, Any]] = []
        self.saved: dict[str, Any] | None = None
        self._polls = 0
        self._poll_budget = poll_budget

    def poll_commands(self):
        self._polls += 1
        if self._polls > self._poll_budget:
            raise AssertionError("the runner is still waiting at the pause")
        return []

    def publish(self, state) -> None:
        self.states.append(state)

    def publish_commands(self, records) -> None:
        pass

    def publish_camera(self, pixels, t, eyes=None) -> None:
        pass

    def poll_settings(self):
        return []

    def save(self, figures_dir, state) -> None:
        self.saved = state

    def stop(self) -> None:
        pass


def _harness(tmp_path, live_monitor, outcomes, keys: list[str], *, pause: bool = True):
    """Two trials; the first is paused (P) and the pause answered with
    ``keys``, one per poll, at the rig's keyboard."""
    from alhazen.testing import FakeClock

    clock = FakeClock()
    tracker = FailingThenGoodTracker(clock, outcomes)
    commands = ScriptedCommands(
        batches=[[Command.PAUSE]] if pause else [], raw_keys=[[], *([key] for key in keys)]
    )
    return SessionHarness(
        tmp_path,
        n_trials=2,
        clock=clock,
        tracker=tracker,
        commands=commands,
        use_pause_menu=True,
        live_monitor=live_monitor,
    )


class TestTheRunnerPublishesIt:
    def test_a_failure_stays_on_the_page_until_the_session_resumes(self, tmp_path) -> None:
        """Pause, Calibrate (fails), then Resume, from the page: the failure
        is on every publish from the end of the calibration to the resume,
        and on none after it."""
        bad = calibration(False, note=TPX_FAILED_NOTE)
        live_monitor = RecordingLiveMonitor()
        harness = _harness(tmp_path, live_monitor, [bad], ["c", "space"])
        harness.runner.run()

        states = live_monitor.states
        calibrating = [s for s in states if s["status"] == PROCEDURE_STATUS]
        assert calibrating, "no publish while calibrating"
        assert calibrating[0]["procedure"]["heading"] == "Calibration"
        assert calibrating[0]["procedure"]["outcome"] == "running"
        guide = [
            s["procedure"] for s in calibrating if s["procedure"]["stage"] == "calibration guide"
        ]
        assert guide and guide[0]["instruction"].startswith("Press SPACE to start")

        end = max(i for i, s in enumerate(states) if s["status"] == PROCEDURE_STATUS)
        resumed = next(i for i, s in enumerate(states) if i > end and s["status"] == "running")
        kept = states[end:resumed]
        assert any(s["status"] == "paused" for s in kept), "no paused publish after the procedure"
        for s in kept:
            assert s["procedure"]["outcome"] == "failed", s["status"]
            assert s["procedure"]["failure"] == "The device reports NO calibration after the walk"
            assert s["procedure"]["check"].startswith("Check the camera sees the eyes")
        assert all(s["procedure"] is None for s in states[resumed:])

    def test_a_calibration_that_took_leaves_no_panel(self, tmp_path) -> None:
        live_monitor = RecordingLiveMonitor()
        harness = _harness(tmp_path, live_monitor, [calibration(True)], ["c", "space"])
        harness.runner.run()
        end = max(i for i, s in enumerate(live_monitor.states) if s["status"] == PROCEDURE_STATUS)
        assert all(s["procedure"] is None for s in live_monitor.states[end:])

    def test_the_next_procedure_replaces_a_kept_failure(self, tmp_path) -> None:
        bad = calibration(False, note=TPX_FAILED_NOTE)
        live_monitor = RecordingLiveMonitor()
        harness = _harness(tmp_path, live_monitor, [bad, calibration(True)], ["c", "c", "space"])
        harness.runner.run()
        procedures = [
            s["procedure"] for s in live_monitor.states if s["status"] == PROCEDURE_STATUS
        ]
        starts = [i for i, p in enumerate(procedures) if p is not None and p["stage"] == "starting"]
        assert len(starts) == 2
        # The second calibration opens on a running guide, not the old failure.
        assert procedures[starts[1]]["outcome"] == "running"
        assert procedures[starts[1] - 1]["outcome"] == "failed"
        assert procedures[-1] is None

    def test_a_failure_before_trials_comes_down_when_a_trial_starts(self, tmp_path) -> None:
        """The calibration request before trial 1 publishes nothing once it is
        answered: the trial that starts says "running" and drops the guide."""
        live_monitor = RecordingLiveMonitor()
        harness = _harness(
            tmp_path,
            live_monitor,
            [calibration(None, aborted=True, note="aborted at the guide")],
            [],
            pause=False,
        )
        harness.eyetracker.calibrate()  # as the request before trial 1 would
        assert harness.eyetracker.procedure["outcome"] == "aborted"
        harness.runner.run()
        aborted = [
            i
            for i, s in enumerate(live_monitor.states)
            if s["procedure"] and s["procedure"]["outcome"] == "aborted"
        ]
        first_running = next(
            i
            for i, s in enumerate(live_monitor.states)
            if s["status"] == "running" and i > max(aborted, default=-1)
        )
        assert live_monitor.states[first_running]["procedure"] is None
        assert harness.eyetracker.procedure is None

    def test_the_saved_copy_never_carries_a_guide(self, tmp_path) -> None:
        bad = calibration(False, note=TPX_FAILED_NOTE)
        live_monitor = RecordingLiveMonitor()
        harness = _harness(tmp_path, live_monitor, [bad], ["c", "q"])
        harness.runner.run()
        assert harness.eyetracker.procedure is not None, "the failure was still up at quit"
        assert live_monitor.saved is not None
        assert live_monitor.saved["procedure"] is None
