"""The calibration request before trial 1.

The regression: a tracker that already held a calibration — a TRACKPixx3
keeps one across runs, an EyeLink's lives on its Host PC — started its trials
without anyone being asked. Now a real tracker always gets the request,
calibrating is the default, reuse is an explicit and recorded choice offered
only when this rig's own record fits, and an aborted or failed calibration
never falls through to trials on the old model.
"""

from __future__ import annotations

import json

import pytest

from alhazen.config.models import EyeTrackerConfig, MonitorConfig
from alhazen.devices.eyetracker.protocol import CalibrationResult, CalibrationTarget
from alhazen.session.startup_calibration import (
    CalibrationLedger,
    StartupCalibration,
    asks_for_calibration,
    ledger_entry,
    previous_calibration,
)

MONITOR = MonitorConfig(
    width_px=1920, height_px=1080, width_cm=52.0, distance_cm=57.0, refresh_rate_hz=60.0
)
VIEWPIXX = EyeTrackerConfig(backend="viewpixx")
EYELINK = EyeTrackerConfig(backend="eyelink")


def result(ok=True, aborted=False, note="", targets=()):
    return CalibrationResult(
        ok=ok,
        layout="HV5",
        n_targets=5,
        eye="both",
        advance="manual",
        t=1.0,
        note=note,
        aborted=aborted,
        targets=tuple(targets),
    )


class Device:
    """A tracker that can say whether it holds a calibration (TRACKPixx3)."""

    def __init__(self, holds=True):
        self.holds = holds

    def calibration_state(self):
        return self.holds


class HostTracker:
    """A tracker that cannot say (EyeLink: the Host PC owns it)."""


class Monitor:
    """EyeTrackerMonitor's calibrate(): answers from a script."""

    def __init__(self, results, device=None, ledger=None, cfg=VIEWPIXX):
        self.results = list(results)
        self.calls = 0
        self.device = device
        self.ledger = ledger
        self.cfg = cfg

    def calibrate(self):
        self.calls += 1
        outcome = self.results.pop(0)
        if self.device is not None and not outcome.aborted:
            self.device.holds = outcome.ok is True
        if outcome.ok is True and self.ledger is not None:
            self.ledger.append(
                ledger_entry(outcome, self.cfg, MONITOR, subject="s01", session=2, run_dir="run")
            )
        return outcome


class Keys:
    """The keyboard: a list of batches, one per poll; the first poll (the
    drain before the request shows) returns `buffered`."""

    def __init__(self, presses, buffered=(), pad=35):
        # Each press comes after `pad` empty polls (10 ms each on the fake
        # clock), past the request's 0.3 s arming window, as a person's does.
        self.batches = [list(buffered)]
        for key in presses:
            self.batches += [[]] * pad + [[key] if key else []]
        self.idle = 0

    def __call__(self):
        if self.batches:
            return self.batches.pop(0)
        self.idle += 1
        if self.idle > 1000:
            raise AssertionError("the request is still waiting for a key nobody pressed")
        return []


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def wait(self, s):
        self.t += s


def make(
    tmp_path,
    presses,
    *,
    results=(),
    tracker=None,
    cfg=VIEWPIXX,
    record=None,
    subject="s01",
    buffered=(),
    pad=35,
):
    ledger = CalibrationLedger(tmp_path / "calibrations" / "rig-lab.jsonl")
    if record is not None:
        ledger.append(record)
    tracker = tracker if tracker is not None else Device()
    monitor = Monitor(
        results, device=tracker if isinstance(tracker, Device) else None, ledger=ledger, cfg=cfg
    )
    shown, events, clock = [], [], Clock()
    request = StartupCalibration(
        monitor=monitor,
        tracker=tracker,
        cfg=cfg,
        screen_monitor=MONITOR,
        ledger=ledger,
        subject=subject,
        session=2,
        show=lambda title, body: shown.append((title, body)),
        poll_keys=Keys(presses, buffered, pad),
        emit=lambda name, payload: events.append((name, payload)),
        wait=clock.wait,
        now=clock.now,
    )
    return request, monitor, shown, events, ledger


def good_record(**changes):
    entry = ledger_entry(
        result(targets=[CalibrationTarget((0.0, 0.0), (1.0, 1.0), (2.0, 0.0), 0.2, 0.3)]),
        VIEWPIXX,
        MONITOR,
        subject="s01",
        session=1,
        run_dir="old-run",
    )
    return {**entry, **changes}


class TestTheRequestIsAlwaysMade:
    def test_a_held_calibration_still_gets_the_request_and_enter_calibrates(self, tmp_path):
        """The bug: the device held one, so nobody was asked."""
        request, monitor, shown, events, _ = make(
            tmp_path, ["return"], results=[result()], record=good_record()
        )
        assert request.request() is True
        assert monitor.calls == 1
        assert shown[0][0] == "CALIBRATE BEFORE TRIALS"
        assert events == [("CALIBRATION_CHOICE", events[0][1])]
        assert events[0][1]["choice"] == "calibrated"

    def test_reuse_is_explicit_recorded_and_runs_no_calibration(self, tmp_path):
        request, monitor, shown, events, _ = make(tmp_path, ["r"], record=good_record())
        assert request.request() is True
        assert monitor.calls == 0
        assert events[0][1]["choice"] == "reused previous"
        assert events[0][1]["previous"]["record"]["run_dir"] == "old-run"
        body = shown[0][1]
        assert "R           reuse the previous calibration" in body
        assert "mean error 0.25°, worst 0.30°" in body

    def test_escape_cancels_the_session(self, tmp_path):
        request, monitor, _shown, events, _ = make(tmp_path, ["escape"])
        assert request.request() is False
        assert monitor.calls == 0 and events[0][1]["choice"] == "cancelled"

    def test_a_key_already_buffered_or_pressed_at_once_cannot_answer(self, tmp_path):
        """The launch keypress, or an impatient R, must not reuse anything:
        the buffer is drained before the request shows, and a key inside the
        first 0.3 s is dropped. 40 empty polls of 10 ms pass the window."""
        presses = ["r"] + [None] * 40 + ["return"]
        request, monitor, _shown, events, _ = make(
            tmp_path, presses, results=[result()], record=good_record(), buffered=["r"], pad=0
        )
        assert request.request() is True
        assert monitor.calls == 1 and events[0][1]["choice"] == "calibrated"


class TestNothingFallsThroughToTheOldModel:
    def test_an_aborted_calibration_asks_again(self, tmp_path):
        request, monitor, shown, events, _ = make(
            tmp_path,
            ["c", "c"],
            results=[result(ok=None, aborted=True, note="ESC at target 2"), result()],
        )
        assert request.request() is True
        assert monitor.calls == 2
        assert "aborted (ESC at target 2)" in shown[1][1]
        assert [e[1]["choice"] for e in events] == ["calibrated"]

    def test_a_failed_calibration_withdraws_reuse_where_the_device_now_holds_none(self, tmp_path):
        request, monitor, shown, events, _ = make(
            tmp_path,
            ["c", "r", "escape"],
            results=[result(ok=False, note="no eye")],
            record=good_record(),
        )
        assert request.request() is False
        assert "NOT calibrated: no eye" in shown[1][1]
        assert "reuse the previous calibration" not in shown[1][1]
        assert [e[1]["choice"] for e in events] == ["cancelled"]

    def test_an_unknown_outcome_is_accepted_only_explicitly(self, tmp_path):
        request, monitor, shown, events, ledger = make(
            tmp_path,
            ["return", "a"],
            results=[result(ok=None, note="no reply")],
            tracker=HostTracker(),
            cfg=EYELINK,
        )
        assert request.request() is True
        assert events[0][1]["choice"] == "accepted with unknown outcome"
        assert "A           accept" in shown[1][1]
        assert ledger.last() is None  # an unknown outcome is never offered for reuse later


class TestWhatIsSaidAboutThePrevious:
    def test_no_record_says_so_and_offers_no_reuse(self, tmp_path):
        previous = previous_calibration(
            Device(), VIEWPIXX, MONITOR, CalibrationLedger(tmp_path / "x.jsonl"), "s01"
        )
        assert previous.held is True and previous.reusable is False
        text = "\n".join(previous.lines())
        assert "recorded on this rig: none" in text and "neither whose it is" in text

    def test_an_eyelink_cannot_say_and_that_is_said(self, tmp_path):
        ledger = CalibrationLedger(tmp_path / "x.jsonl")
        ledger.append(good_record(backend="eyelink", host_ip="100.1.1.1"))
        previous = previous_calibration(HostTracker(), EYELINK, MONITOR, ledger, "s01")
        assert previous.held is None and previous.reusable is True
        assert "cannot say" in previous.lines()[0]
        assert "cannot check" in previous.reason

    def test_a_record_without_errors_has_no_invented_quality(self, tmp_path):
        ledger = CalibrationLedger(tmp_path / "x.jsonl")
        ledger.append(good_record(mean_error_deg=None, max_error_deg=None))
        text = "\n".join(previous_calibration(Device(), VIEWPIXX, MONITOR, ledger, "s01").lines())
        assert "quality: not reported by this tracker" in text

    @pytest.mark.parametrize(
        ("changes", "subject", "why"),
        [
            ({"distance_cm": 60.0}, "s01", "distance_cm"),
            ({"layout": "HV9"}, "s01", "layout"),
            ({}, "s02", "another subject"),
            ({"ok": None}, "s01", "did not report success"),
        ],
    )
    def test_reuse_needs_the_same_setup_subject_and_a_success(
        self, tmp_path, changes, subject, why
    ):
        ledger = CalibrationLedger(tmp_path / "x.jsonl")
        ledger.append(good_record(**changes))
        previous = previous_calibration(Device(), VIEWPIXX, MONITOR, ledger, subject)
        assert previous.reusable is False and why in previous.reason

    def test_a_device_holding_none_is_never_offered(self, tmp_path):
        ledger = CalibrationLedger(tmp_path / "x.jsonl")
        ledger.append(good_record())
        previous = previous_calibration(Device(holds=False), VIEWPIXX, MONITOR, ledger, "s01")
        assert previous.reusable is False and "holds none" in previous.reason

    def test_an_unreadable_ledger_line_is_skipped(self, tmp_path):
        path = tmp_path / "x.jsonl"
        path.write_text(json.dumps(good_record()) + "\n{cut off", encoding="utf-8")
        assert CalibrationLedger(path).last()["run_dir"] == "old-run"


class TestWhoIsAsked:
    @pytest.mark.parametrize(
        ("backend", "from_rig", "attended", "asked"),
        [
            ("eyelink", True, True, True),
            ("viewpixx", True, True, True),
            ("mouse_sim", True, True, False),
            ("scripted", True, True, False),
            ("viewpixx", False, True, False),  # handed in: simulate's autopilot, a replay
            ("eyelink", True, False, False),  # nobody at the keyboard
        ],
    )
    def test_only_the_rigs_own_real_tracker_with_someone_there(
        self, backend, from_rig, attended, asked
    ):
        cfg = EyeTrackerConfig(backend=backend)
        assert asks_for_calibration(tracker_from_rig=from_rig, cfg=cfg, attended=attended) is asked
        assert asks_for_calibration(tracker_from_rig=True, cfg=None, attended=True) is False


class TestTheRealAdapters:
    """With the SDK fakes: what each real backend lets the request know."""

    def test_a_trackpixx3_holding_a_calibration_reports_it(self, monkeypatch, tmp_path):
        from alhazen.devices.eyetracker.viewpixx import ViewPixxTracker
        from alhazen.testing import FakeClock
        from fake_sdk import install_fake_pypixxlib
        from support import SCREEN

        device = install_fake_pypixxlib(monkeypatch)
        device.holds_calibration = True
        tracker = ViewPixxTracker(VIEWPIXX, None, SCREEN, FakeClock(), background_gaze=False)
        tracker.connect()
        tracker.configure(SCREEN, tracker._clock)
        try:
            ledger = CalibrationLedger(tmp_path / "x.jsonl")
            previous = previous_calibration(tracker, VIEWPIXX, MONITOR, ledger, "s01")
            # Held, but alhazen recorded nothing: asked, and reuse not offered.
            assert previous.held is True and previous.reusable is False
            ledger.append(good_record())
            assert previous_calibration(tracker, VIEWPIXX, MONITOR, ledger, "s01").reusable
            device.holds_calibration = False
            assert not previous_calibration(tracker, VIEWPIXX, MONITOR, ledger, "s01").reusable
        finally:
            tracker.shutdown(None)

    def test_an_eyelink_has_no_state_to_read(self, monkeypatch, tmp_path):
        from alhazen.devices.eyetracker.eyelink import EyeLinkTracker
        from alhazen.testing import FakeClock
        from fake_sdk import install_fake_pylink
        from support import SCREEN

        install_fake_pylink(monkeypatch, FakeClock(start=10.0))
        tracker = EyeLinkTracker(EYELINK, None, SCREEN, FakeClock())
        tracker.connect()
        assert getattr(tracker, "calibration_state", None) is None
        previous = previous_calibration(
            tracker, EYELINK, MONITOR, CalibrationLedger(tmp_path / "x.jsonl"), "s01"
        )
        assert previous.held is None and previous.reusable is False


class TestTheSessionWiring:
    """A built session (simulated rig) with the request wired into its
    runner: cancel starts no trial; a calibration is recorded and trials run."""

    def build(self, tmp_path, presses, results):
        from tests.unit.test_builder import build, read_table

        from alhazen.core.events import EventSchema

        runner = build(tmp_path, EventSchema(()))
        ledger = CalibrationLedger(tmp_path / "ledger.jsonl")
        clock = Clock()
        request = StartupCalibration(
            monitor=Monitor(results),
            tracker=Device(),
            cfg=VIEWPIXX,
            screen_monitor=MONITOR,
            ledger=ledger,
            subject="t01",
            session=1,
            show=lambda t, b: None,
            poll_keys=Keys(presses),
            wait=clock.wait,
            now=clock.now,
        )
        # The builder wires this for a rig's own real tracker with a keyboard;
        # a simulated rig has neither, so the test hands it over the same way.
        runner._startup_calibration = request
        request.emit = runner._emit_session_event
        return runner, read_table

    def test_cancel_runs_no_trial(self, tmp_path):
        runner, read_table = self.build(tmp_path, ["escape"], [])
        runner.run()
        assert read_table(tmp_path, "trials") == []
        events = [row["event"] for row in read_table(tmp_path, "events")]
        assert "CALIBRATION_CHOICE" in events and "TRIAL_START" not in events

    def test_a_calibration_then_the_trials(self, tmp_path):
        runner, read_table = self.build(tmp_path, ["return"], [result()])
        runner.run()
        assert len(read_table(tmp_path, "trials")) == 1


class TestEveryCalibrationIsRecorded:
    """Through the real EyeTrackerMonitor: the hook the builder uses to keep
    the ledger hears every calibration that took, and only those."""

    def monitor(self, outcome):
        from alhazen.display.simulated import SimulatedDisplay
        from alhazen.session.eyetracker import EyeTrackerMonitor
        from alhazen.testing import FakeClock
        from support import SCREEN

        class Tracker:
            def calibrate(self):
                return outcome

        cfg = EyeTrackerConfig(backend="viewpixx", validate_after_calibration=False)
        return EyeTrackerMonitor(
            Tracker(), SimulatedDisplay(60.0), SCREEN, FakeClock(), cfg, poll_keys=lambda: []
        )

    @pytest.mark.parametrize(
        ("outcome", "recorded"),
        [
            (result(), 1),
            (result(ok=False), 0),
            (result(ok=None, aborted=True), 0),
            (result(ok=None), 0),
        ],
    )
    def test_only_a_calibration_that_took(self, outcome, recorded):
        heard = []
        monitor = self.monitor(outcome)
        monitor.on_calibrated = heard.append
        monitor.calibrate()
        assert len(heard) == recorded

    def test_a_ledger_that_cannot_be_written_does_not_undo_the_calibration(self, caplog):
        monitor = self.monitor(result())

        def full_disk(_result):
            raise OSError("No space left on device")

        monitor.on_calibrated = full_disk
        assert monitor.calibrate().ok is True
        assert "could not record this calibration" in caplog.text
