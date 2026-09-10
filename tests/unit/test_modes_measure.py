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
    """Stands in for a display: counts flips, records messages, and — like
    the real show_message — flips when it shows one."""

    def __init__(self):
        self.flips = 0
        self.messages = []
        # What each flip presented: the message drawn for it, or None for a
        # flip that cleared the screen.
        self.presented = []

    def flip(self, clear=True):
        self.flips += 1
        self.presented.append(None)

    def show_message(self, text):
        self.messages.append(text)
        self.flips += 1
        self.presented.append(text)


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

    def test_the_timed_flip_is_the_one_that_showed_the_prompt(self):
        """show_message flips. A second flip after it cleared the prompt off
        the screen AND was the flip the latency was measured from — so the
        prompt vanished and the number was wrong, which is the worse half."""
        display = FlipCounter()
        stamps = []

        def now():
            stamps.append(list(display.presented))
            return 0.0

        measure_key_latency(
            display, lambda: ("a", 0.2, 0.21), n_presses=2, show=display.show_message, now=now
        )

        # At every stamp, the last thing flipped was the prompt, not a blank.
        assert all(presented[-1] is not None for presented in stamps)
        assert None not in display.presented

    def test_without_a_prompt_the_flip_itself_is_the_marker(self):
        display = FlipCounter()
        measure_key_latency(display, lambda: ("a", 0.2, 0.21), n_presses=2, now=lambda: 0.0)
        assert display.flips == 2


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

    def test_the_trackers_own_reason_is_what_the_operator_reads(self):
        """ "No eye" was the wrong diagnosis for an afternoon on a device that
        had no calibration. A tracker that can say which it was is asked."""
        from types import SimpleNamespace

        from alhazen.modes.measure import sample_target

        samples = iter([None, SimpleNamespace(gx=960.0, gy=540.0)])
        said = []

        sample_target(
            (0.0, 0.0),
            lambda _p: None,
            lambda: next(samples),
            self._screen(),
            echo=said.append,
            gaze_status=lambda: "NO CALIBRATION on the device — the camera SEES the eye",
        )

        assert said == [
            "  no gaze position: NO CALIBRATION on the device — the camera SEES the eye "
            "— look at the dot and press again"
        ]

    def test_the_accuracy_report_names_the_calibration_it_measured(self):
        from alhazen.modes.measure import measure_tracker_accuracy

        screen = self._screen()
        result = measure_tracker_accuracy(
            tracker=None,
            screen=screen,
            present_target=lambda position: position,
            targets=((0.0, 0.0),),
            calibration="calibrated: HV9 (9 targets), left (both eyes calibrated), manual",
        )

        assert result.ok is True
        assert result.detail["calibration"].startswith("calibrated: HV9")
        assert result.detail["notes"] == [
            "Measured against: calibrated: HV9 (9 targets), left (both eyes calibrated), manual"
        ]

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


class TestCalibrationBeforeAccuracy:
    """Uncalibrated gaze against target positions is not an accuracy
    measurement; on a device with no calibration it is NaN against a number.
    The verdict of the calibrate() that precedes the check decides whether
    there is anything to measure."""

    def _result(self, **kwargs):
        from alhazen.devices.eyetracker.protocol import CalibrationResult

        base = dict(ok=True, layout="HV9", n_targets=9, eye="left", advance="manual", t=1.0)
        return CalibrationResult(**{**base, **kwargs})

    def test_a_calibration_that_took_lets_the_check_run(self):
        from alhazen.modes.measure import calibration_verdict

        proceed, line = calibration_verdict(self._result(note="the device reports a calibration"))
        assert proceed
        assert line.startswith("calibrated: HV9 (9 targets), left, manual")

    def test_an_aborted_or_failed_calibration_refuses_the_check(self):
        from alhazen.modes.measure import calibration_verdict

        proceed, line = calibration_verdict(self._result(ok=None, aborted=True, note="aborted"))
        assert not proceed and line.startswith("aborted")
        proceed, line = calibration_verdict(self._result(ok=False, note="did not take"))
        assert not proceed and line.startswith("NOT calibrated")

    def test_a_tracker_that_reports_nothing_is_measured_and_said_so(self):
        from alhazen.modes.measure import calibration_verdict

        proceed, line = calibration_verdict(None)
        assert proceed
        assert line == "this tracker reports no calibration result"


class TestItSaysWhenTheDisplayIsCrawling:
    """A compositor throttling an unfocused window gives about 1 fps. At that
    rate the default run is two minutes of a command that has printed one line
    and looks hung — and what it would eventually report is what it already
    knows after eight flips."""

    def _rig(self, hz=60.0):
        from alhazen.config.models import MonitorConfig, RigConfig

        return RigConfig(
            monitor=MonitorConfig(
                width_px=1920,
                height_px=1080,
                width_cm=52.0,
                distance_cm=57.0,
                refresh_rate_hz=hz,
            ),
            data_root="data",
        )

    class _SlowDisplay:
        """Flips that take `period` of simulated time, via a patched clock."""

        def __init__(self, period):
            self.period = period
            self.t = 0.0

        def flip(self):
            self.t += self.period

    def _run(self, monkeypatch, period_s, n_flips=16):
        from alhazen.modes import measure

        display = self._SlowDisplay(period_s)
        monkeypatch.setattr(measure.time, "perf_counter", lambda: display.t)
        said = []
        result = measure.measure_display(self._rig(), display, n_flips=n_flips, echo=said.append)
        return result, said

    def test_a_crawling_display_is_called_out_before_the_run_ends(self, monkeypatch):
        # 1 fps against a configured 60 Hz.
        _result, said = self._run(monkeypatch, 1.0)

        assert said, "a display running 60x slow said nothing"
        assert "1 Hz" in said[0] and "60 Hz" in said[0]
        # And it says how long the wait will be, which is the actionable part.
        assert "s." in said[0]

    def test_a_healthy_display_says_nothing(self, monkeypatch):
        _result, said = self._run(monkeypatch, 1 / 60)

        assert said == []

    def test_it_still_returns_the_measurement(self, monkeypatch):
        """The warning is a courtesy; the report is the job."""
        result, _said = self._run(monkeypatch, 1.0)

        assert result.name == "display timing"
        assert result.ok is False  # 1 Hz is not 60 Hz


class TestTheRulerClosesTheLoop:
    """`measure_geometry` said what a bar should measure and sent the
    operator to run `alhazen calibrate ruler` by hand; one expected the bar
    on screen and did not get one. It is now the last step of the run."""

    def test_it_is_the_last_measurement_and_skippable(self):
        from alhazen.config.models import RigConfig

        assert MEASUREMENTS[-1] == "ruler"
        # The driver validates --skip against the list, so "ruler" is a
        # name it accepts; a wrong one is still refused by name.
        rig = RigConfig(monitor=MONITOR, data_root="data")
        with pytest.raises(ValueError, match="nothing to skip called rulr"):
            run_measurements(rig, "rig.yaml", skip=("rulr",))

    def test_the_summary_carries_the_number_the_tape_is_compared_against(self):
        from alhazen.config.models import RigConfig
        from alhazen.modes.measure import ruler_measurement

        rig = RigConfig(monitor=MONITOR, data_root="data")
        measurement = ruler_measurement(rig)

        assert measurement.ok is None  # only a tape can judge it
        expected_cm = SCREEN.deg2px(10.0) * MONITOR.width_cm / MONITOR.width_px
        assert measurement.detail["expected_cm"] == pytest.approx(expected_cm)
        assert f"{expected_cm:.2f} cm between the ticks" in measurement.summary
        assert any("calibrate ruler" in note for note in measurement.detail["notes"])

    def test_a_key_left_over_from_an_earlier_measurement_does_not_close_it(self, monkeypatch):
        """`draw_ruler` used to run in a process that had just opened its own
        window, so psychopy's key buffer was empty. Measure mode calls it on a
        window that has already collected presses from the tracker and key
        measurements, and one of those left over ended the ruler before a
        single flip: a black screen, and a report saying a bar was drawn."""
        import sys
        import types

        from alhazen.cli.calibrate import draw_ruler_on
        from alhazen.config.models import RigConfig

        class FakeEvent:
            """psychopy's global key buffer, with a press already in it."""

            def __init__(self) -> None:
                self.buffer = ["space"]  # left over from the previous step
                self.polls = 0
                self.cleared_after_polls = None

            def clearEvents(self) -> None:
                self.cleared_after_polls = self.polls
                self.buffer = []

            def getKeys(self):
                self.polls += 1
                if self.polls > 3:  # the operator's own key, eventually
                    return ["space"]
                keys, self.buffer = self.buffer, []
                return keys

        class FakeStim:
            def __init__(self, *args, **kwargs) -> None:
                self.draws = 0

            def draw(self) -> None:
                self.draws += 1

        class FakeDisplay:
            window = object()

            def __init__(self) -> None:
                self.flips = 0

            def flip(self) -> None:
                self.flips += 1

        event = FakeEvent()
        psychopy = types.ModuleType("psychopy")
        psychopy.event = event
        psychopy.visual = types.SimpleNamespace(Rect=FakeStim, TextStim=FakeStim)
        monkeypatch.setitem(sys.modules, "psychopy", psychopy)

        display = FakeDisplay()
        draw_ruler_on(display, RigConfig(monitor=MONITOR, data_root="data"))

        # Cleared before anything was polled, and the bar actually reached
        # the screen rather than the loop falling through on a stale key.
        assert event.cleared_after_polls == 0
        assert display.flips == 3
