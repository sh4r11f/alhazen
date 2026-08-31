"""Measurement mode: the arithmetic, and what the report is willing to claim.

The procedures that touch a display, a keyboard or a tracker take their
hardware as an injected callable, so everything here runs with none of it.
What is checked is the part that could be quietly wrong: a statistic that
misreports a distribution, a judgement that passes a rig it should fail, and
a report that says "OK" about something it did not measure.
"""

from __future__ import annotations

import json

import pytest

from alhazen.config.models import MonitorConfig
from alhazen.display.screen import Screen
from alhazen.modes.measure import (
    MEASUREMENTS,
    Measurement,
    MeasurementReport,
    accuracy,
    frame_timing,
    judge_refresh,
    measure_key_latency,
    measure_tracker_accuracy,
    run_measurements,
    summarise,
)

MONITOR = MonitorConfig(
    width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=60.0
)
SCREEN = Screen.from_monitor(MONITOR)


class FlipCounter:
    """Stands in for a display: counts flips, records messages."""

    def __init__(self):
        self.flips = 0
        self.messages = []

    def flip(self, clear=True):
        self.flips += 1

    def show_message(self, text):
        self.messages.append(text)


class TestSummarise:
    def test_it_reports_the_median_not_the_mean(self):
        """Every distribution here has a long right tail — a late frame, a
        slow press — and a mean reports the tail as the typical case."""
        stats = summarise([10.0, 10.0, 10.0, 10.0, 1000.0])

        assert stats["median"] == 10.0

    def test_a_single_sample_has_no_spread(self):
        assert summarise([4.0]) == {"n": 1, "median": 4.0, "iqr": 0.0, "min": 4.0, "max": 4.0}

    def test_an_empty_sample_is_refused_rather_than_averaged(self):
        with pytest.raises(ValueError, match="nothing to summarise"):
            summarise([])


class TestFrameTiming:
    def test_a_clean_display_reports_its_configured_rate(self):
        timing = frame_timing([1 / 60] * 120, 60.0)

        assert timing["n_dropped"] == 0
        assert judge_refresh(timing)[0] is True

    def test_one_long_frame_is_counted_as_dropped(self):
        """The number an average hides, and the one a session cares about."""
        timing = frame_timing([1 / 60] * 119 + [1 / 20], 60.0)

        assert timing["n_dropped"] == 1
        assert judge_refresh(timing)[0] is False

    def test_a_panel_running_at_the_wrong_rate_fails(self):
        ok, summary = judge_refresh(frame_timing([1 / 144] * 120, 60.0))

        assert ok is False
        assert "144" in summary and "60" in summary

    def test_it_uses_the_same_late_frame_rule_a_session_does(self):
        """50% over the expected interval, matching FrameMonitor's default,
        so a rig that measures clean here and drops frames in a session is
        telling you about the experiment, not the panel."""
        expected = 1 / 60

        assert frame_timing([expected * 1.4], 60.0)["n_dropped"] == 0
        assert frame_timing([expected * 1.6], 60.0)["n_dropped"] == 1

    def test_no_intervals_is_refused(self):
        with pytest.raises(ValueError, match="no intervals"):
            frame_timing([], 60.0)


class TestAccuracy:
    def test_a_perfect_tracker_has_no_error(self):
        targets = [(0.0, 0.0), (100.0, 100.0)]

        assert accuracy(targets, targets, SCREEN)["median"] == 0.0

    def test_the_error_is_reported_in_degrees(self):
        one_degree = SCREEN.deg2px(1.0)

        result = accuracy([(0.0, 0.0)], [(one_degree, 0.0)], SCREEN)

        assert result["median"] == pytest.approx(1.0)

    def test_every_target_is_reported_individually(self):
        """A tracker that is fine at the centre and 3 degrees out at one
        corner is a different problem from one that is evenly bad, and only
        the per-target list distinguishes them."""
        result = accuracy([(0.0, 0.0), (500.0, 0.0)], [(0.0, 0.0), (600.0, 0.0)], SCREEN)

        assert len(result["per_target_dva"]) == 2
        assert result["per_target_dva"][0]["error_dva"] == 0.0
        assert result["per_target_dva"][1]["error_dva"] > 0

    def test_mismatched_lengths_are_refused(self):
        with pytest.raises(ValueError, match="1 targets but 2"):
            accuracy([(0.0, 0.0)], [(0.0, 0.0), (1.0, 1.0)], SCREEN)


class TestTrackerJudgement:
    def _measure(self, offset_dva):
        offset = SCREEN.deg2px(offset_dva)
        return measure_tracker_accuracy(
            None, SCREEN, lambda p: (p[0] + offset, p[1]), targets=[(0.0, 0.0), (8.0, 0.0)]
        )

    def test_a_well_calibrated_tracker_passes(self):
        assert self._measure(0.3).ok is True

    def test_worse_than_a_degree_fails_and_says_what_to_do(self):
        """A fixation window narrower than the error refuses trials the
        subject is actually making."""
        result = self._measure(1.5)

        assert result.ok is False
        assert any("Recalibrate" in note for note in result.detail["notes"])


class TestKeyLatency:
    def test_it_separates_the_polling_lag_from_the_whole_path(self):
        """Poll lag is alhazen's own contribution and is the only part it
        controls; flip-to-key is dominated by the person."""
        display = FlipCounter()
        # arrived 200 ms after the flip, noticed 5 ms after that. The flips
        # are stamped by the injected clock, so both ends of every subtraction
        # are on one epoch — which is the whole point of injecting it.
        times = iter([(0.200, 0.205), (0.220, 0.225), (0.210, 0.215)])

        def wait():
            arrived, noticed = next(times)
            return "space", arrived, noticed

        result = measure_key_latency(display, wait, n_presses=3, now=lambda: 0.0)

        assert result.detail["poll_lag_s"]["median"] == pytest.approx(0.005, abs=1e-3)
        assert result.detail["press_latency_s"]["median"] == pytest.approx(0.21, abs=0.02)

    def test_it_is_reported_not_judged(self):
        """There is no right answer: a distribution dominated by human
        reaction time is a fact about the rig, not a pass or a fail."""
        display = FlipCounter()

        result = measure_key_latency(
            display, lambda: ("a", 0.2, 0.21), n_presses=2, now=lambda: 0.0
        )

        assert result.ok is None

    def test_it_asks_for_each_press_and_flips_for_each(self):
        display = FlipCounter()

        measure_key_latency(
            display, lambda: ("a", 0.2, 0.21), n_presses=3, show=display.show_message
        )

        assert display.flips == 3
        assert len(display.messages) == 3
        assert "3" in display.messages[-1]


class TestReport:
    def test_a_measurement_with_no_right_answer_does_not_make_the_rig_ok_or_not(self):
        report = MeasurementReport("rig.yaml", [Measurement("keys", "some numbers", None)])

        assert report.ok is True  # nothing failed
        assert "-- " in report.render()  # and it is not shown as a pass

    def test_one_failure_fails_the_report(self):
        report = MeasurementReport(
            "rig.yaml",
            [Measurement("a", "fine", True), Measurement("b", "bad", False)],
        )

        assert report.ok is False

    def test_notes_reach_the_rendered_report(self):
        report = MeasurementReport(
            "rig.yaml", [Measurement("a", "s", False, {"notes": ["check the cable"]})]
        )

        assert "check the cable" in report.render()

    def test_it_saves_json_that_can_be_compared_with_next_months(self, tmp_path):
        report = MeasurementReport(
            "rig.yaml", [Measurement("display timing", "60 Hz", True, {"measured_hz": 60.0})]
        )

        written = report.save(tmp_path / "sub" / "m.json")

        saved = json.loads(written.read_text())
        assert saved["ok"] is True
        assert saved["measurements"][0]["measured_hz"] == 60.0


class TestTheDriverRefusesNonsense:
    def test_an_unknown_measurement_to_skip_is_named(self):
        with pytest.raises(ValueError, match="nothing to skip called wobble"):
            run_measurements(None, "rig.yaml", skip=["wobble"])

    def test_the_error_lists_what_can_be_skipped(self):
        with pytest.raises(ValueError, match="display, geometry, keys, tracker"):
            run_measurements(None, "rig.yaml", skip=["nope"])

    def test_every_named_measurement_is_skippable(self):
        """Guard against MEASUREMENTS and the driver's own branches drifting
        apart, which would silently ignore a skip the CLI accepted."""
        for name in MEASUREMENTS:
            with pytest.raises(Exception) as caught:
                run_measurements(None, "rig.yaml", skip=[name])
            assert "nothing to skip" not in str(caught.value)


class TestSamplingOneValidationTarget:
    """The tracker path had three defects that no test could reach, because
    it was the only part of this module that went at the device layer
    directly. This is that part, with its two hardware ends injected."""

    def _screen(self):
        from alhazen.config.models import MonitorConfig
        from alhazen.display.screen import Screen

        return Screen.from_monitor(
            MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=52.0,
                distance_cm=57.0,
                refresh_rate_hz=120.0,
            )
        )

    def test_it_returns_the_gaze_in_centred_pixels(self):
        from types import SimpleNamespace

        from alhazen.modes.measure import sample_target

        screen = self._screen()
        # Screen px, origin top-left: the centre of a 1920x1080 panel.
        gaze = SimpleNamespace(gx=960.0, gy=540.0)

        result = sample_target((0.0, 0.0), lambda _p: None, lambda: gaze, screen)

        assert result == pytest.approx((0.0, 0.0), abs=1e-6)

    def test_a_blink_asks_again_rather_than_inventing_a_sample(self):
        """Substituting a default would report perfect accuracy at a point
        that was never measured — the worst of the three options."""
        from types import SimpleNamespace

        from alhazen.modes.measure import sample_target

        samples = iter([None, None, SimpleNamespace(gx=960.0, gy=540.0)])
        shown, said = [], []

        result = sample_target(
            (10.0, 20.0),
            shown.append,
            lambda: next(samples),
            self._screen(),
            echo=said.append,
        )

        assert result == pytest.approx((0.0, 0.0), abs=1e-6)
        # Re-presented each time, so the operator has something to look at.
        assert shown == [(10.0, 20.0)] * 3
        assert len(said) == 2 and "no eye" in said[0]

    def test_it_does_not_throw_away_the_targets_already_collected(self):
        """A blink raising would lose every point measured before it, which
        on the ninth target of nine is the whole validation."""
        from types import SimpleNamespace

        from alhazen.modes.measure import measure_tracker_accuracy

        screen = self._screen()
        blinked = {"once": False}

        def present(position):
            if not blinked["once"]:
                blinked["once"] = True
                # One blink, then a clean sample: exercises the retry inside
                # the accuracy loop rather than around it.
                return sample_target_with_one_blink(position, screen)
            return position

        def sample_target_with_one_blink(position, screen):
            from alhazen.modes.measure import sample_target

            samples = iter([None, SimpleNamespace(gx=960.0, gy=540.0)])
            return sample_target(position, lambda _p: None, lambda: next(samples), screen)

        result = measure_tracker_accuracy(
            tracker=None, screen=screen, present_target=present, targets=((0.0, 0.0), (4.0, 0.0))
        )

        assert result.detail["n"] == 2
