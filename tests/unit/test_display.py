"""Screen geometry and frame QA mechanics."""

from __future__ import annotations

import pytest

from alhazen.config.models import FrameQAConfig, MonitorConfig
from alhazen.display.frames import FrameMonitor
from alhazen.display.screen import Screen, within_radius
from alhazen.display.simulated import SimulatedDisplay
from alhazen.errors import FrameQAError


class TestScreen:
    def setup_method(self):
        self.screen = Screen.from_monitor(
            MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=60.0,
                distance_cm=60.0,
                refresh_rate_hz=60.0,
            )
        )

    def test_px2deg_is_exact_inverse_of_deg2px(self):
        # The invariant that keeps recorded positions honest: whatever model
        # places a stimulus must read positions back identically.
        for deg in (0.0, 0.5, 3.7, 15.0, -8.2):
            assert self.screen.px2deg(self.screen.deg2px(deg)) == pytest.approx(deg)

    def test_px_per_deg_magnitude(self):
        # 60 cm distance, 32 px/cm: 1 dva ~ 60*tan(1 deg)*32 ~ 33.5 px.
        assert self.screen.px_per_deg == pytest.approx(33.5, abs=0.1)

    def test_screen_centered_roundtrip_and_y_flip(self):
        cx, cy = self.screen.screen_to_centered(960.0, 540.0)
        assert (cx, cy) == (0.0, 0.0)
        # Screen y grows down; centered y grows up.
        _, top = self.screen.screen_to_centered(960.0, 0.0)
        assert top == 540.0
        sx, sy = self.screen.centered_to_screen(-100.0, 200.0)
        assert self.screen.screen_to_centered(sx, sy) == (-100.0, 200.0)

    def test_within_radius_boundary_inclusive(self):
        assert within_radius((3.0, 4.0), (0.0, 0.0), 5.0)
        assert not within_radius((3.0, 4.1), (0.0, 0.0), 5.0)


class TestFrameMonitor:
    def make(self, policy="warn", tolerance=0.5, budget=3):
        cfg = FrameQAConfig(policy=policy, tolerance=tolerance, max_dropped_per_trial=budget)
        return FrameMonitor(cfg, refresh_rate_hz=100.0)  # expected 10 ms

    def test_first_flip_only_establishes_reference(self):
        monitor = self.make()
        monitor.start_trial(1)
        assert monitor.note_flip(5.0) is False
        assert monitor.records == []

    def test_detects_drop_beyond_tolerance(self):
        monitor = self.make(tolerance=0.5)
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        assert monitor.note_flip(0.010) is False  # exactly one period
        assert monitor.note_flip(0.024) is False  # 14 ms < 15 ms threshold
        assert monitor.note_flip(0.040) is True  # 16 ms > threshold
        assert [r.dropped for r in monitor.records] == [False, False, True]

    def test_trial_gap_is_not_a_drop(self):
        monitor = self.make()
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        monitor.note_flip(0.010)
        monitor.start_trial(2)  # long inter-trial gap follows
        assert monitor.note_flip(5.0) is False

    def test_abort_run_raises_only_past_budget(self):
        monitor = self.make(policy="abort_run", budget=1)
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        assert monitor.note_flip(0.1) is True  # first drop: within budget
        with pytest.raises(FrameQAError):
            monitor.note_flip(0.2)

    def test_marks_trials_flag(self):
        assert self.make(policy="mark_trial").marks_trials
        assert self.make(policy="abort_run").marks_trials
        assert not self.make(policy="warn").marks_trials
        assert not self.make(policy="log").marks_trials

    def test_save_writes_full_log(self, tmp_path):
        monitor = self.make()
        monitor.start_trial(1)
        monitor.note_flip(0.0)
        monitor.note_flip(0.010)
        monitor.note_flip(0.040)
        path = tmp_path / "frames.csv"
        monitor.save(path)
        lines = path.read_text().strip().splitlines()
        assert lines[0] == "trial_index,t,interval_s,dropped"
        assert len(lines) == 3  # header + 2 measured intervals
        assert lines[2].endswith("True")


class TestSimulatedDisplay:
    def test_unpaced_reports_nominal_rate(self):
        display = SimulatedDisplay(nominal_refresh_hz=60.0, frame_period_s=0.0)
        display.open()
        assert display.measure_refresh_rate(10) == 60.0
        display.flip()
        assert display.flip_count == 1

    def test_messages_are_recorded(self):
        display = SimulatedDisplay(nominal_refresh_hz=60.0, frame_period_s=0.0)
        display.show_message("hello")
        assert display.messages == ["hello"]
