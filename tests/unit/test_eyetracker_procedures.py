"""Validation and drift correction (devices/eyetracker/procedures.py).

The procedures are generic over the ``EyeTracker`` protocol, so they are
tested here against a subject who looks wherever the target is — plus a
chosen offset, a blink or two, some jitter — with a scripted experimenter
pressing keys at chosen session times. FakeDisplay's flips advance the
FakeClock one frame at a time, so every timing in the walk is exact.

Coordinates are checked deliberately: the walk converts screen-px gaze to
centered px the way the input provider does, and a mistake there would show
up as a validation that "passes" while every fixation window is off.
"""

from __future__ import annotations

import logging
import math

import pytest

from alhazen.config.models import EyeTrackerConfig
from alhazen.devices.eyetracker.procedures import (
    ABORT_KEY,
    ACCEPT_KEYS,
    PROCEDURE_KEYS,
    PROCEDURE_TRIAL_INDEX,
    REDO_KEY,
    DriftResult,
    GazeCorrection,
    ProcedureTiming,
    TargetError,
    ValidationResult,
    drift_correct,
    validate,
    validation_targets,
)
from alhazen.devices.eyetracker.protocol import EyeTracker, GazeSample
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.devices.eyetracker.viewpixx import calibration_targets
from alhazen.testing import FakeClock, FakeDisplay, FakeStimulus
from support import FRAME_S, SCREEN

# Short enough that a five-target walk is a few hundred frames; long enough
# that settle, sample and the frame period are clearly distinct.
FAST = ProcedureTiming(settle_s=0.1, sample_s=0.1, stable_deg=1.0, timeout_s=1.0)


class Subject:
    """Where the fake subject is looking: the target on screen, plus an
    offset in centered px, unless they are blinking. Shared between the
    target factory (which learns what is on screen) and the tracker."""

    def __init__(self) -> None:
        self.target = (0.0, 0.0)
        self.offset_px = (0.0, 0.0)
        self.blink = False
        # Called per read to perturb the position — jitter, a drifting eye.
        self.perturb = lambda t: (0.0, 0.0)
        self.shown: list[tuple[float, float]] = []

    def make_target(self, position: tuple[float, float]) -> FakeStimulus:
        self.target = position
        self.shown.append(position)
        return FakeStimulus("target")


class FollowingTracker(ScriptedTracker):
    """A tracker whose gaze is wherever the Subject looks, in SCREEN px, on
    the session clock — so the walk's conversion is exercised, not assumed."""

    def __init__(self, subject: Subject, clock: FakeClock) -> None:
        super().__init__([], clock)
        self.subject = subject

    def get_gaze(self) -> GazeSample | None:
        t = self._clock.now()
        # perturb() runs first: it is where a test scripts a blink by time.
        jx, jy = self.subject.perturb(t)
        if self.subject.blink:
            return None
        cx = self.subject.target[0] + self.subject.offset_px[0] + jx
        cy = self.subject.target[1] + self.subject.offset_px[1] + jy
        gx, gy = SCREEN.centered_to_screen(cx, cy)
        return GazeSample(gx=gx, gy=gy, t=t)


class Experimenter:
    """Keys pressed at session times: ``press(t, key)`` queues one, and the
    walk's poll sees it on the first frame at or after ``t``."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._pending: list[tuple[float, str]] = []
        self.polls = 0

    def press(self, t: float, key: str) -> None:
        self._pending.append((t, key))

    def poll(self) -> list[str]:
        self.polls += 1
        now = self._clock.now()
        due = [key for t, key in self._pending if t <= now]
        self._pending = [(t, key) for t, key in self._pending if t > now]
        return due


class Rig:
    """Everything one procedure call needs, built around one clock."""

    def __init__(self, **cfg_kwargs) -> None:
        self.clock = FakeClock()
        self.display = FakeDisplay(self.clock, FRAME_S)
        self.subject = Subject()
        self.tracker = FollowingTracker(self.subject, self.clock)
        self.experimenter = Experimenter(self.clock)
        self.cfg = EyeTrackerConfig(backend="scripted", **cfg_kwargs)
        self.correction = GazeCorrection()

    def validate(self, **kwargs) -> ValidationResult:
        kwargs.setdefault("timing", FAST)
        kwargs.setdefault("correction", self.correction)
        kwargs.setdefault("make_target", self.subject.make_target)
        return validate(
            self.tracker,
            self.display,
            SCREEN,
            self.clock,
            self.cfg,
            poll_keys=self.experimenter.poll,
            **kwargs,
        )

    def drift_correct(self, **kwargs) -> DriftResult:
        kwargs.setdefault("timing", FAST)
        kwargs.setdefault("make_target", self.subject.make_target)
        return drift_correct(
            self.tracker,
            self.display,
            SCREEN,
            self.clock,
            self.cfg,
            self.correction,
            poll_keys=self.experimenter.poll,
            **kwargs,
        )

    def accept_each_target(self, n: int, *, every_s: float = 0.5, first_at: float = 0.3) -> None:
        """A manual-mode experimenter who presses SPACE once per target."""
        for i in range(n):
            self.experimenter.press(first_at + i * every_s, "space")


class TestKeys:
    def test_the_three_roles_are_the_calibration_walks(self):
        assert "space" in ACCEPT_KEYS
        assert REDO_KEY == "backspace"
        assert ABORT_KEY == "escape"
        assert set(PROCEDURE_KEYS) == {*ACCEPT_KEYS, REDO_KEY, ABORT_KEY}


class TestValidationTargets:
    def test_a_self_driven_layout_is_the_calibrations_own_grid(self):
        cfg = EyeTrackerConfig(backend="scripted", calibration_type="HV9", calibration_area=0.8)
        assert validation_targets(cfg, SCREEN) == calibration_targets("HV9", SCREEN, 0.8)

    def test_a_host_owned_layout_falls_back_to_hv5_and_says_so(self, caplog):
        cfg = EyeTrackerConfig(backend="eyelink", calibration_type="H3")
        with caplog.at_level(logging.INFO):
            targets = validation_targets(cfg, SCREEN)
        assert targets == calibration_targets("HV5", SCREEN, cfg.calibration_area)
        assert "validating over HV5" in caplog.text


class TestManualValidation:
    def test_a_perfect_subject_passes_with_zero_error(self):
        rig = Rig(calibration_type="HV5")
        rig.accept_each_target(5)
        result = rig.validate()
        assert not result.aborted
        assert len(result.targets) == 5
        assert result.n_missed == 0
        assert result.max_error_deg == pytest.approx(0.0)
        assert result.accepted
        assert result.advance == "manual"
        assert result.t == rig.clock.now()
        # Every target was shown, in the calibration's order.
        assert rig.subject.shown == validation_targets(rig.cfg, SCREEN)

    def test_the_error_is_the_offset_in_degrees(self):
        rig = Rig(calibration_type="HV5", accuracy_max_deg=1.0)
        rig.subject.offset_px = (SCREEN.deg2px(0.5), 0.0)  # half a degree right
        rig.accept_each_target(5)
        result = rig.validate()
        assert result.mean_error_deg == pytest.approx(0.5)
        assert result.max_error_deg == pytest.approx(0.5)
        assert result.accepted
        # The gaze reported is in centered px, beside the target it was on.
        first = result.targets[0]
        assert first.gaze_px == pytest.approx((SCREEN.deg2px(0.5), 0.0))
        assert first.n_samples > 0

    def test_an_error_over_the_threshold_fails(self, caplog):
        rig = Rig(calibration_type="HV5", accuracy_max_deg=1.0)
        rig.subject.offset_px = (0.0, SCREEN.deg2px(1.5))
        rig.accept_each_target(5)
        with caplog.at_level(logging.WARNING):
            result = rig.validate()
        assert not result.accepted
        assert "FAILED" in result.summary()
        assert "FAILED" in caplog.text  # loud, not just returned

    def test_the_window_is_the_sample_after_the_accept(self):
        # The subject moves onto the target only after the experimenter
        # accepts: what is measured must be the gaze *after* SPACE, not the
        # gaze while they were still looking elsewhere.
        rig = Rig(calibration_type="HV5")
        target_shown = rig.subject.shown
        rig.subject.offset_px = (SCREEN.deg2px(3.0), 0.0)

        def move_on_target(t: float) -> tuple[float, float]:
            # From t=0.3 the subject is on target (offset cancelled).
            return (-SCREEN.deg2px(3.0), 0.0) if t >= 0.3 else (0.0, 0.0)

        rig.subject.perturb = move_on_target
        rig.experimenter.press(0.3, "space")
        result = rig.validate(targets=[(0.0, 0.0)])
        assert target_shown
        assert result.targets[0].error_deg == pytest.approx(0.0)

    def test_an_accept_during_the_settle_waits_for_it(self):
        # SPACE the instant the target appears: the window still opens only
        # once the settle time has passed, so the saccade is not averaged in.
        rig = Rig(calibration_type="HV5")
        moves: list[float] = []

        def landing(t: float) -> tuple[float, float]:
            moves.append(t)
            # Off by 5° until the settle ends, on target after.
            return (SCREEN.deg2px(5.0), 0.0) if t < FAST.settle_s else (0.0, 0.0)

        rig.subject.perturb = landing
        rig.experimenter.press(0.0, "space")
        result = rig.validate(targets=[(0.0, 0.0)])
        assert result.targets[0].error_deg == pytest.approx(0.0)
        # And the window is a full sample_s long, counted from the settle.
        assert result.targets[0].n_samples >= round(FAST.sample_s / FRAME_S)

    def test_a_blink_inside_the_window_is_dropped_not_fatal(self):
        rig = Rig(calibration_type="HV5")
        blink_frames = {"n": 0}

        def blink_once(t: float) -> tuple[float, float]:
            # One frame of no eye in the middle of the window.
            if 0.15 <= t < 0.15 + FRAME_S and blink_frames["n"] == 0:
                blink_frames["n"] += 1
                rig.subject.blink = True
            else:
                rig.subject.blink = False
            return (0.0, 0.0)

        rig.subject.perturb = blink_once
        rig.experimenter.press(0.12, "space")
        result = rig.validate(targets=[(0.0, 0.0)])
        assert result.targets[0].error_deg == pytest.approx(0.0)
        assert result.accepted

    def test_no_gaze_at_all_is_a_missed_target_and_a_failed_validation(self):
        rig = Rig(calibration_type="HV5")
        rig.subject.blink = True
        rig.accept_each_target(5)
        result = rig.validate()
        assert result.n_missed == 5
        assert result.mean_error_deg is None
        assert not result.accepted
        assert "no target measured" in result.summary()
        assert all(t.gaze_px is None and t.n_samples == 0 for t in result.targets)

    def test_one_missed_target_fails_even_when_the_rest_are_perfect(self):
        rig = Rig(calibration_type="HV5")
        # The subject blinks through the whole of the third target.
        rig.subject.perturb = lambda t: (0.0, 0.0)

        def blink_third(position: tuple[float, float]) -> FakeStimulus:
            rig.subject.blink = len(rig.subject.shown) == 2
            return rig.subject.make_target(position)

        rig.accept_each_target(5)
        result = rig.validate(make_target=blink_third)
        assert result.n_missed == 1
        assert result.max_error_deg == pytest.approx(0.0)
        assert not result.accepted
        assert "1 target(s) missed" in result.summary()

    def test_escape_keeps_what_was_measured_and_gives_no_verdict(self, caplog):
        rig = Rig(calibration_type="HV5")
        rig.experimenter.press(0.3, "space")
        rig.experimenter.press(0.8, "space")
        rig.experimenter.press(1.3, "escape")
        with caplog.at_level(logging.WARNING):
            result = rig.validate()
        assert result.aborted
        assert len(result.targets) == 2
        assert not result.accepted
        assert "aborted after 2 target(s)" in result.summary()
        assert "aborted by the experimenter at target 3" in caplog.text
        assert len(rig.subject.shown) == 3

    def test_backspace_remeasures_the_previous_target(self):
        rig = Rig(calibration_type="HV5")
        rig.experimenter.press(0.3, "space")  # target 1
        rig.experimenter.press(0.8, "space")  # target 2
        rig.experimenter.press(1.2, "backspace")  # on target 3: back to target 2
        rig.experimenter.press(1.5, "space")  # target 2 again
        rig.experimenter.press(2.0, "space")  # 3
        rig.experimenter.press(2.5, "space")  # 4
        rig.experimenter.press(3.0, "space")  # 5
        result = rig.validate()
        grid = validation_targets(rig.cfg, SCREEN)
        assert rig.subject.shown == [grid[0], grid[1], grid[2], grid[1], grid[2], grid[3], grid[4]]
        assert [t.target_px for t in result.targets] == grid
        assert result.accepted

    def test_backspace_on_the_first_target_stays_on_it(self):
        rig = Rig(calibration_type="HV5")
        rig.experimenter.press(0.1, "backspace")
        rig.accept_each_target(5, first_at=0.4)
        result = rig.validate()
        grid = validation_targets(rig.cfg, SCREEN)
        assert rig.subject.shown[:2] == [grid[0], grid[0]]
        assert len(result.targets) == 5

    def test_keys_are_read_case_insensitively(self):
        rig = Rig(calibration_type="HV5")
        for i in range(5):
            rig.experimenter.press(0.3 + i * 0.5, "SPACE")
        assert rig.validate().accepted

    def test_return_accepts_too(self):
        rig = Rig(calibration_type="HV5")
        for i in range(5):
            rig.experimenter.press(0.3 + i * 0.5, "return")
        assert rig.validate().accepted


class TestAutoValidation:
    def test_a_steady_subject_is_measured_without_a_key(self):
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        result = rig.validate()
        assert result.advance == "auto"
        assert len(result.targets) == 5
        assert result.accepted
        assert rig.experimenter.polls > 0  # keys were still being read

    def test_the_advance_can_be_overridden_per_call(self):
        rig = Rig(calibration_type="HV5", calibration_advance="manual")
        result = rig.validate(advance="auto")
        assert result.advance == "auto" and result.accepted

    def test_a_subject_who_never_settles_times_out_as_missed(self, caplog):
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        # Two degrees of jitter, every frame: no window is ever stable.
        flip = {"sign": 1.0}

        def jitter(t: float) -> tuple[float, float]:
            flip["sign"] = -flip["sign"]
            return (flip["sign"] * SCREEN.deg2px(2.0), 0.0)

        rig.subject.perturb = jitter
        with caplog.at_level(logging.WARNING):
            result = rig.validate(targets=[(0.0, 0.0)])
        assert result.n_missed == 1
        assert not result.accepted
        assert "no stable fixation within 1.0 s" in caplog.text
        # It gave up at the timeout, not before and not much after.
        assert rig.clock.now() == pytest.approx(FAST.timeout_s, abs=2 * FRAME_S)

    def test_the_settle_is_respected_before_the_first_window(self):
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        rig.validate(targets=[(0.0, 0.0)])
        assert rig.clock.now() >= FAST.settle_s + FAST.sample_s * 0.9

    def test_space_takes_the_window_as_it_stands(self):
        # A subject who is steady but 2° off: auto would still accept the
        # window (steady is steady), so what SPACE changes is *when*: the
        # first full window rather than waiting.
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        rig.subject.offset_px = (SCREEN.deg2px(2.0), 0.0)
        rig.experimenter.press(0.0, "space")  # too early: no window yet, ignored
        result = rig.validate(targets=[(0.0, 0.0)])
        assert result.targets[0].error_deg == pytest.approx(2.0)

    def test_a_blink_restarts_the_rolling_window(self):
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        seen = {"blink_at": None}

        def blink_late(t: float) -> tuple[float, float]:
            # The eye vanishes just as the first window would have closed.
            if 0.18 <= t < 0.18 + FRAME_S:
                rig.subject.blink = True
                seen["blink_at"] = t
            else:
                rig.subject.blink = False
            return (0.0, 0.0)

        rig.subject.perturb = blink_late
        result = rig.validate(targets=[(0.0, 0.0)])
        assert seen["blink_at"] is not None
        assert result.accepted
        # Every sample in the accepted window came after the blink.
        assert rig.clock.now() > 0.18 + FAST.sample_s * 0.9

    def test_escape_aborts_auto_mode_too(self):
        rig = Rig(calibration_type="HV5", calibration_advance="auto")
        rig.experimenter.press(0.05, "escape")
        result = rig.validate()
        assert result.aborted and result.targets == ()


class TestRecordingSegment:
    def test_a_procedure_opens_its_own_segment_under_trial_zero(self):
        rig = Rig(calibration_type="HV5")
        rig.accept_each_target(5)
        rig.validate()
        assert rig.tracker.trials_started == [(PROCEDURE_TRIAL_INDEX, "validation")]
        assert not rig.tracker.is_recording()  # closed again

    def test_a_segment_already_open_is_left_alone(self):
        rig = Rig(calibration_type="HV5")
        rig.tracker.start_trial(7, "ok")
        rig.accept_each_target(5)
        rig.validate()
        assert rig.tracker.trials_started == [(7, "ok")]
        assert rig.tracker.is_recording()  # not ours to close

    def test_the_screen_is_cleared_afterwards_even_on_abort(self):
        rig = Rig(calibration_type="HV5")
        rig.experimenter.press(0.05, "escape")
        flips_before = rig.display.flip_count
        rig.validate()
        assert rig.display.flip_count > flips_before
        assert not rig.tracker.is_recording()


class TestProgress:
    def test_the_hook_hears_every_target_at_most_twice_a_second(self):
        rig = Rig(calibration_type="HV5")
        reports: list[tuple[str, str]] = []
        rig.accept_each_target(5, every_s=1.0)
        rig.validate(progress=lambda stage, detail: reports.append((stage, detail)))
        stages = {stage for stage, _ in reports}
        assert stages == {"validating"}
        details = [detail for _, detail in reports]
        assert details[0] == "target 1 of 5"
        assert details[-1] == "target 5 of 5"
        assert len(reports) <= 2 * 5 * 1.0 + 5  # throttled: ~2 Hz per target


class TestGazeCorrection:
    def test_nothing_applied_is_the_identity(self):
        correction = GazeCorrection()
        assert not correction.active
        assert correction.apply((10.0, -5.0)) == (10.0, -5.0)
        assert correction.t is None

    def test_shifts_accumulate_and_reset(self):
        correction = GazeCorrection()
        correction.shift_by(3.0, -1.0, t=1.0)
        correction.shift_by(1.0, 1.0, t=2.0)
        assert correction.offset == (4.0, 0.0)
        assert correction.active
        assert correction.apply((0.0, 0.0)) == (4.0, 0.0)
        assert correction.t == 2.0
        correction.reset(t=3.0)
        assert correction.offset == (0.0, 0.0)
        assert not correction.active
        assert correction.t == 3.0


class TestDriftCorrection:
    def test_a_small_offset_is_applied(self, caplog):
        rig = Rig(drift_max_deg=3.0)
        rig.subject.offset_px = (SCREEN.deg2px(1.0), -SCREEN.deg2px(0.5))
        rig.experimenter.press(0.3, "space")
        with caplog.at_level(logging.INFO):
            result = rig.drift_correct()
        assert result.applied
        assert result.offset_deg == pytest.approx(math.hypot(1.0, 0.5))
        # target − gaze: the shift that puts the reported gaze on the target.
        assert result.offset_px == pytest.approx((-SCREEN.deg2px(1.0), SCREEN.deg2px(0.5)))
        assert rig.correction.offset == pytest.approx(result.offset_px)
        assert rig.correction.t == result.t
        assert "applied" in caplog.text
        assert rig.subject.shown == [(0.0, 0.0)]

    def test_the_correction_is_measured_through_itself(self):
        # A second drift correction sees the residual, not the whole offset:
        # the walk reads gaze the way the input provider does, corrected.
        rig = Rig(drift_max_deg=3.0)
        rig.subject.offset_px = (SCREEN.deg2px(1.0), 0.0)
        rig.experimenter.press(0.3, "space")
        rig.drift_correct()
        rig.experimenter.press(rig.clock.now() + 0.3, "space")
        second = rig.drift_correct()
        assert second.offset_deg == pytest.approx(0.0, abs=1e-9)
        assert rig.correction.offset == pytest.approx((-SCREEN.deg2px(1.0), 0.0))

    def test_a_validation_after_a_correction_sees_the_corrected_gaze(self):
        rig = Rig(calibration_type="HV5", drift_max_deg=3.0)
        rig.subject.offset_px = (SCREEN.deg2px(1.0), 0.0)
        rig.experimenter.press(0.3, "space")
        rig.drift_correct()
        t0 = rig.clock.now()
        for i in range(5):
            rig.experimenter.press(t0 + 0.3 + i * 0.5, "space")
        result = rig.validate()
        assert result.max_error_deg == pytest.approx(0.0, abs=1e-9)
        assert result.accepted

    def test_an_offset_too_large_is_refused_and_leaves_the_correction_alone(self, caplog):
        rig = Rig(drift_max_deg=3.0)
        rig.subject.offset_px = (SCREEN.deg2px(4.0), 0.0)
        rig.experimenter.press(0.3, "space")
        with caplog.at_level(logging.WARNING):
            result = rig.drift_correct()
        assert not result.applied
        assert result.offset_deg == pytest.approx(4.0)
        assert "recalibrate" in result.note
        assert "REFUSED" in result.summary()
        assert "REFUSED" in caplog.text
        assert not rig.correction.active

    def test_no_gaze_is_reported_not_applied(self):
        rig = Rig(drift_max_deg=3.0)
        rig.subject.blink = True
        rig.experimenter.press(0.3, "space")
        result = rig.drift_correct()
        assert not result.applied
        assert result.gaze_px is None and result.offset_deg is None
        assert "no gaze measured" in result.summary()
        assert not rig.correction.active

    def test_escape_aborts(self):
        rig = Rig(drift_max_deg=3.0)
        rig.experimenter.press(0.05, "escape")
        result = rig.drift_correct()
        assert not result.applied and result.note == "aborted"

    def test_backspace_measures_again(self):
        rig = Rig(drift_max_deg=3.0)
        rig.experimenter.press(0.05, "backspace")
        rig.experimenter.press(0.4, "space")
        result = rig.drift_correct()
        assert result.applied
        assert rig.subject.shown == [(0.0, 0.0), (0.0, 0.0)]

    def test_auto_mode_needs_no_key(self):
        rig = Rig(drift_max_deg=3.0, calibration_advance="auto")
        rig.subject.offset_px = (0.0, SCREEN.deg2px(0.5))
        result = rig.drift_correct()
        assert result.applied and result.offset_deg == pytest.approx(0.5)

    def test_the_payloads_are_json_shaped(self):
        rig = Rig(calibration_type="HV5", drift_max_deg=3.0)
        rig.experimenter.press(0.3, "space")
        drift = rig.drift_correct().payload()
        assert drift["applied"] is True
        assert drift["target_px"] == [0.0, 0.0]
        assert isinstance(drift["offset_px"], list)
        t0 = rig.clock.now()
        for i in range(5):
            rig.experimenter.press(t0 + 0.3 + i * 0.5, "space")
        validation = rig.validate().payload()
        assert validation["accepted"] is True
        assert validation["n_targets"] == 5
        assert len(validation["targets"]) == 5
        assert isinstance(validation["targets"][0]["target_px"], list)

    def test_a_segment_is_opened_for_the_measurement(self):
        rig = Rig(drift_max_deg=3.0)
        rig.experimenter.press(0.3, "space")
        rig.drift_correct()
        assert rig.tracker.trials_started == [(PROCEDURE_TRIAL_INDEX, "drift correction")]


class TestProtocol:
    def test_the_following_tracker_is_still_a_tracker(self):
        # The fake here must not have grown past the protocol: an experiment
        # package's own fake is held to the same shape.
        assert isinstance(FollowingTracker(Subject(), FakeClock()), EyeTracker)

    def test_a_target_error_records_a_miss_explicitly(self):
        miss = TargetError((0.0, 0.0), None, None, 0)
        assert miss.gaze_px is None and miss.error_deg is None
