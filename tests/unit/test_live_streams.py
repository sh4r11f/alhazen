"""The live monitor's two switchable streams: the Trial view's gaze traces and
the eye camera. Off is a switch at the source: the session stops producing the
stream and the server stops serving it, without touching the stimulus, the
trial logic or what is recorded.

Design: live_monitor/runtime.py (STREAMS), live_monitor/gaze.py,
session/eyetracker.py (camera_switched_off), docs/live_monitor.md.
"""

from __future__ import annotations

import json
import logging
import queue
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from alhazen.config.models import LiveMonitorConfig, RigConfig, with_live_streams
from alhazen.core.trial import InputFrame
from alhazen.live_monitor.gaze import GazeFeed
from alhazen.live_monitor.runtime import (
    EYE_CAMERA,
    GAZE_TRACES,
    LiveMonitorController,
    start_live_streams,
)
from support import SessionHarness


class _Sink:
    def __init__(self) -> None:
        self.batches: list[list] = []

    def __call__(self, batch) -> bool:
        self.batches.append(batch)
        return True

    @property
    def samples(self):
        return [s for batch in self.batches for s in batch]


def _frame(x: float, t: float) -> InputFrame:
    return InputFrame(gaze=(x, 0.0), gaze_t=t)


class TestGazeFeedSwitch:
    def test_off_sends_nothing_and_drops_what_was_waiting(self):
        on = [True]
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=1.0, max_hz=1000.0, flush_s=10.0, enabled=lambda: on[0])
        feed.note(1, 0, 0.0, _frame(0.0, 0.0))  # the first goes at once
        feed.note(1, 1, 0.1, _frame(1.0, 0.1))  # waits for the next flush
        on[0] = False
        for i in range(2, 50):
            feed.note(1, i, i / 10, _frame(float(i), i / 10))
        feed.flush(100.0, force=True)
        assert sink.samples == [(1, 0.0, 0.0, 0.0)], (
            "nothing after the switch, not even the waiting one"
        )

    def test_back_on_mid_trial_starts_after_a_gap(self):
        on = [True]
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=1.0, max_hz=1000.0, flush_s=0.0, enabled=lambda: on[0])
        feed.note(1, 0, 0.0, _frame(0.0, 0.0))
        on[0] = False
        feed.note(1, 1, 0.1, _frame(1.0, 0.1))
        on[0] = True
        feed.note(1, 2, 0.2, _frame(2.0, 0.2))
        # A gap first, so the page draws no line across the time it was off.
        assert sink.samples == [(1, 0.0, 0.0, 0.0), (1, 0.2, None, None), (1, 0.2, 2.0, 0.0)]

    def test_off_costs_the_frame_one_check(self):
        calls = []
        sink = _Sink()

        def switch() -> bool:
            calls.append(1)
            return False

        feed = GazeFeed(sink, px_per_deg=1.0, enabled=switch)
        for i in range(100):
            feed.note(1, i, i / 60, _frame(float(i), i / 60))
        assert len(calls) == 100 and sink.batches == []

    def test_without_a_switch_it_is_always_on(self):
        sink = _Sink()
        GazeFeed(sink, px_per_deg=1.0, flush_s=0.0).note(1, 0, 0.0, _frame(3.0, 0.0))
        assert sink.samples == [(1, 0.0, 3.0, 0.0)]


class TestControllerGating:
    """Not started, so nobody collects: what reaches a queue stays in it."""

    def test_both_on_by_default_and_set_from_the_rig_before_start(self):
        controller = LiveMonitorController(auto_open=False)
        assert controller.streams() == {GAZE_TRACES: True, EYE_CAMERA: True}
        start_live_streams(controller, LiveMonitorConfig(gaze_traces=False, camera=False))
        assert controller.streams() == {GAZE_TRACES: False, EYE_CAMERA: False}
        assert controller.gaze_traces_on() is False and controller.camera_on() is False
        with pytest.raises(ValueError, match="unknown live monitor stream"):
            controller.set_stream("video", False)

    def test_gaze_off_puts_nothing_on_the_channel(self):
        controller = LiveMonitorController(auto_open=False)
        controller.set_stream(GAZE_TRACES, False)
        for i in range(5):
            assert controller.publish_gaze([(1, float(i), 0.0, 0.0)]) is True
        with pytest.raises(queue.Empty):
            controller._gaze.get(timeout=0.2)

    def test_camera_off_puts_no_frame_on_the_channel(self):
        controller = LiveMonitorController(auto_open=False)
        controller.set_stream(EYE_CAMERA, False)
        controller.publish_camera(np.zeros((3, 4), dtype=np.uint8), 1.0)
        with pytest.raises(queue.Empty):
            controller._camera.get(timeout=0.2)
        controller.set_stream(EYE_CAMERA, True)
        controller.publish_camera(np.zeros((3, 4), dtype=np.uint8), 2.0)
        assert controller._camera.get(timeout=2)[2] == 2.0

    def test_every_state_carries_the_switches(self):
        controller = LiveMonitorController(auto_open=False)
        controller.set_stream(EYE_CAMERA, False)
        controller.publish({"revision": 3, "status": "running"})
        _revision, _status, _controls, payload = controller._updates.get(timeout=2)
        assert json.loads(payload)["streams"] == {GAZE_TRACES: True, EYE_CAMERA: False}


class TestServerSwitches:
    def test_the_page_switches_a_stream_and_the_session_sees_it(self):
        controller = LiveMonitorController(auto_open=False)
        controller.set_stream(EYE_CAMERA, False)
        url = controller.start()
        token = url.partition("token=")[2]
        root = url.partition("/?")[0]

        def post(body: object, with_token: bool = True):
            request = urllib.request.Request(
                f"{root}/api/stream",
                data=json.dumps(body).encode(),
                headers={"X-Alhazen-Token": token} if with_token else {},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())

        try:
            # The rig's start, as the page first reads it.
            with urllib.request.urlopen(f"{root}/api/streams?token={token}", timeout=5) as r:
                assert json.loads(r.read()) == {GAZE_TRACES: True, EYE_CAMERA: False}
            status, body = post({"stream": GAZE_TRACES, "on": False})
            assert status == 200 and body == {GAZE_TRACES: False, EYE_CAMERA: False}
            # The session's side of the same byte: no queue, no wait.
            assert controller.gaze_traces_on() is False
            assert controller.publish_gaze([(1, 0.0, 0.0, 0.0)]) is True
            post({"stream": EYE_CAMERA, "on": True})
            assert controller.camera_on() is True

            for bad, code in (
                ({"stream": "video", "on": False}, 400),
                ({"stream": GAZE_TRACES, "on": "no"}, 400),
                ({"stream": GAZE_TRACES}, 400),
            ):
                with pytest.raises(urllib.error.HTTPError) as refused:
                    post(bad)
                assert refused.value.code == code
            with pytest.raises(urllib.error.HTTPError) as forbidden:
                post({"stream": GAZE_TRACES, "on": True}, with_token=False)
            assert forbidden.value.code == 403
            assert controller.gaze_traces_on() is False, "a refused switch changes nothing"
            with pytest.raises(urllib.error.HTTPError) as unread:
                urllib.request.urlopen(f"{root}/api/streams", timeout=2)
            assert unread.value.code == 403
        finally:
            controller.stop()

    def test_switching_the_camera_off_forgets_the_last_frame(self):
        controller = LiveMonitorController(auto_open=False)
        url = controller.start()
        token = url.partition("token=")[2]
        root = url.partition("/?")[0]
        try:
            controller.publish_camera(np.full((3, 4), 7, dtype=np.uint8), 1.0)
            camera = f"{root}/api/camera?token={token}&after=0&wait_ms=3000"
            with urllib.request.urlopen(camera, timeout=5) as response:
                assert response.status == 200
            request = urllib.request.Request(
                f"{root}/api/stream",
                data=json.dumps({"stream": EYE_CAMERA, "on": False}).encode(),
                headers={"X-Alhazen-Token": token},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=5).close()
            # A page opened now is not handed the old picture as current.
            quick = f"{root}/api/camera?token={token}&after=0&wait_ms=50"
            with urllib.request.urlopen(quick, timeout=5) as response:
                assert response.status == 204
        finally:
            controller.stop()


class TestRigAndCommandLine:
    def test_the_rig_keys_default_on(self):
        assert LiveMonitorConfig().gaze_traces is True
        assert LiveMonitorConfig().camera is True
        assert LiveMonitorConfig.model_validate({"camera": False}).camera is False

    def test_a_launch_sets_them_over_the_rig_and_nothing_asked_changes_nothing(self, tmp_path):
        from support import MONITOR

        rig = RigConfig(
            monitor=MONITOR, data_root=tmp_path, live_monitor=LiveMonitorConfig(gaze_traces=False)
        )
        assert with_live_streams(rig) is rig
        chosen = with_live_streams(rig, gaze_traces=True, camera=False)
        assert (chosen.live_monitor.gaze_traces, chosen.live_monitor.camera) == (True, False)
        kept = with_live_streams(rig, camera=False)
        assert kept.live_monitor.gaze_traces is False, "an unasked stream keeps the rig's setting"

    def test_the_flags_parse_and_are_advertised(self):
        import argparse

        from alhazen.cli.capabilities import CAPABILITIES
        from alhazen.cli.main import add_mode_arguments

        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        args = parser.parse_args(["--no-gaze-traces", "--eye-camera"])
        assert (args.gaze_traces, args.eye_camera) == (False, True)
        assert parser.parse_args([]).gaze_traces is None
        assert "live-streams" in CAPABILITIES


class _SwitchingMonitor:
    """A live monitor whose operator switches the gaze traces off as the first
    trial's picture goes out, the camera having started off."""

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:0/"
        self.switches = {GAZE_TRACES: True, EYE_CAMERA: False}
        self.views = 0

    def streams(self) -> dict[str, bool]:
        return dict(self.switches)

    def publish(self, state: dict) -> None:
        pass

    def publish_camera(self, pixels, t, eyes=None) -> None:
        pass

    def publish_trial(self, view: dict) -> None:
        self.views += 1
        if self.views == 1:
            self.switches[GAZE_TRACES] = False

    def publish_gaze(self, samples) -> bool:
        return True

    def poll_commands(self):
        return []

    def poll_settings(self):
        return []

    def save(self, figures_dir: Path, state: dict) -> None:
        pass

    def stop(self) -> None:
        pass


class TestSessionLog:
    def test_the_start_and_every_switch_are_logged(self, tmp_path: Path, caplog):
        monitor = _SwitchingMonitor()
        harness = SessionHarness(
            tmp_path, n_trials=3, live_monitor=monitor, on_pause=lambda m: "resume"
        )
        with caplog.at_level(logging.INFO, logger="alhazen.session.runner"):
            harness.runner.run()
        lines = [r.getMessage() for r in caplog.records if "live monitor" in r.getMessage()]
        assert "live monitor streams: gaze traces on, eye camera off" in lines
        assert lines.count("live monitor: gaze traces switched off from the page") == 1


class _CountingController(LiveMonitorController):
    """The real controller with no child process, counting what the session
    hands it. Its switches are the real shared bytes, set by the builder from
    the rig; the gaze feed reads them every frame."""

    instances: list[_CountingController] = []

    def __init__(self, *, port: int = 0, auto_open: bool = True) -> None:
        super().__init__(port=port, auto_open=auto_open)
        self.gaze_offers = 0
        _CountingController.instances.append(self)

    def start(self, timeout_s: float = 20.0) -> str:
        self.url = "http://127.0.0.1:0/"
        return self.url

    def publish(self, state) -> None:
        pass

    def publish_trial(self, view) -> None:
        pass

    def publish_gaze(self, samples) -> bool:
        self.gaze_offers += 1
        return True


class TestTheBuilderStartsTheSwitchesFromTheRig:
    @pytest.mark.parametrize("gaze_traces", [True, False])
    def test_gaze_off_means_the_session_never_offers_a_sample(
        self, tmp_path, monkeypatch, gaze_traces
    ):
        from alhazen.config.models import DevicesConfig, DisplayConfig, Duration
        from alhazen.core.events import EventSchema
        from alhazen.devices.eyetracker import GazeSample, ScriptedTracker
        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.session import builder as builder_module
        from alhazen.task.plan import TrialPlan
        from alhazen.testing import FakeClock
        from support import COMPLETED, MONITOR, TEST_EXPERIMENT, RunForFrames

        _CountingController.instances.clear()
        monkeypatch.setattr(builder_module, "LiveMonitorController", _CountingController)
        clock = FakeClock()
        samples = [(i / 60, GazeSample(gx=960.0 + i, gy=540.0, t=i / 60)) for i in range(120)]
        rig = RigConfig(
            monitor=MONITOR,
            display=DisplayConfig(backend="simulated"),
            devices=DevicesConfig(),
            data_root=tmp_path,
            live_monitor=LiveMonitorConfig(
                enabled=True, auto_open=False, gaze_traces=gaze_traces, camera=False
            ),
        )
        runner = builder_module.build_session(
            rig=rig,
            subject="t01",
            session=1,
            run=1,
            task_name="test-task",
            task_params=Duration(ms=1),
            event_schema=EventSchema(()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(30, COMPLETED)]),
            make_source=lambda params, rng: SimpleSequence(
                [Condition({"c": "a"})], n_repeats=2, rng=rng
            ),
            tracker=ScriptedTracker(samples, clock),
            clock=clock,
            seed=1,
            iti=Duration(ms=0),
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20260826",
            experiment_version=TEST_EXPERIMENT.version,
        )
        runner.run()
        (controller,) = _CountingController.instances
        assert controller.streams() == {GAZE_TRACES: gaze_traces, EYE_CAMERA: False}
        if gaze_traces:
            assert controller.gaze_offers > 0
        else:
            assert controller.gaze_offers == 0
