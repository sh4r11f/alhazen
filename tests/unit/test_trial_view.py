"""The live monitor's Trial view: the trial's schematic in dva, the gaze feed,
their channels to the page, and the session wiring.

Design: task/schematic.py, display/schematic.py, live_monitor/gaze.py,
docs/live_monitor.md ("Trial view").
"""

from __future__ import annotations

import json
import math
import queue
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alhazen.core.trial import CircleRegion, InputFrame
from alhazen.display.schematic import (
    MAX_POINTS,
    Circle,
    Dots,
    Label,
    Line,
    Polygon,
    Rect,
    check_shapes,
)
from alhazen.live_monitor.gaze import GazeFeed
from alhazen.live_monitor.runtime import LiveMonitorController, _GazeState, _TrialViewState
from alhazen.task.plan import TrialPlan
from alhazen.task.schematic import region_shapes, region_title, trial_view
from support import SCREEN, SessionHarness

# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class TestShapes:
    def test_each_shape_says_what_it_is_in_plain_json(self):
        shapes = [
            Circle((1, 2), 0.5, name="fixation"),
            Rect((0, 0), (4, 2), angle_deg=30, role="annotation"),
            Line((0, 0), (3, 4)),
            Polygon([(0, 0), (1, 0), (0, 1)], approximate=True),
            Dots([(1, 1), (2, 2)], 0.1),
            Label((0, -5), "occluder"),
        ]
        payloads = [shape.payload() for shape in shapes]
        assert payloads[0] == {
            "kind": "circle",
            "role": "stimulus",
            "name": "fixation",
            "center": [1.0, 2.0],
            "radius": 0.5,
        }
        assert payloads[1]["angle_deg"] == 30.0 and payloads[1]["role"] == "annotation"
        assert payloads[3]["approximate"] is True
        # Only an approximate shape says so: an exact one carries no flag.
        assert "approximate" not in payloads[0]
        # A label is the operator's annotation unless it says otherwise.
        assert payloads[5]["role"] == "annotation"
        json.dumps(payloads, allow_nan=False)

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (lambda: Circle((0, math.nan), 1), "circle center y must be finite"),
            (lambda: Circle((0, 0), 0), "circle radius must be greater than 0"),
            (lambda: Circle((0, 0), -1), "circle radius must be greater than 0"),
            (lambda: Circle((0, 0, 0), 1), r"circle center must be an \(x, y\) pair"),
            (lambda: Circle(("a", 0), 1), "circle center x must be a number"),
            (lambda: Circle((True, 0), 1), "circle center x must be a number"),
            (lambda: Rect((0, 0), (1, 0)), "rect height must be greater than 0"),
            (lambda: Polygon([(0, 0), (1, 1)]), "at least 3 point"),
            (lambda: Dots([], 0.1), "at least 1 point"),
            (lambda: Label((0, 0), " "), "label text must be a non-empty string"),
            (lambda: Label((0, 0), "x" * 81), "at most 80"),
            (lambda: Circle((0, 0), 1, role="target"), "role must be one of"),
            (lambda: Circle((0, 0), 1, name=""), "circle name must be a non-empty string"),
            (lambda: Line((0, 0), (math.inf, 0)), "line end x must be finite"),
        ],
    )
    def test_a_shape_refuses_what_it_cannot_draw(self, build, message):
        with pytest.raises(ValueError, match=message):
            build()

    def test_a_schematic_is_a_list_of_shapes_within_the_point_budget(self):
        with pytest.raises(ValueError, match="holds Circle, Rect"):
            check_shapes([Circle((0, 0), 1), {"kind": "circle"}])
        with pytest.raises(ValueError, match="sequence of shapes"):
            check_shapes(Circle((0, 0), 1))
        half = [(float(i), 0.0) for i in range(MAX_POINTS // 2 + 1)]
        with pytest.raises(ValueError, match=f"at most {MAX_POINTS}"):
            check_shapes([Dots(half, 0.1), Dots(half, 0.1)])
        assert len(check_shapes([Dots(half, 0.1)])) == 1

    def test_a_task_schematic_is_checked_at_build_trial(self):
        with pytest.raises(ValueError, match="TrialPlan.schematic"):
            TrialPlan(phases=[], schematic=[Circle((0, 0), 1), "fixation"])  # type: ignore[list-item]
        plan = TrialPlan(phases=[], schematic=[Circle((0, 0), 1)])
        assert isinstance(plan.schematic, tuple)


# ---------------------------------------------------------------------------
# The default schematic
# ---------------------------------------------------------------------------


class _Describes:
    """A stimulus with a ``schematic(screen)`` method: the generic hook the
    first Trial view read. Nothing reads it now (a task's own picture is the
    only source of stimulus shapes), which these tests pin."""

    def __init__(self, shapes):
        self._shapes = shapes
        self.asked = 0

    def update(self, dt: float) -> None:
        pass

    def draw(self) -> None:
        pass

    def schematic(self, screen):
        self.asked += 1
        return self._shapes


class TestDefaultSchematic:
    def test_regions_convert_from_centered_px_to_dva_with_the_session_screen(self):
        # SCREEN: 40 px per degree.
        shapes = region_shapes(
            {
                "fixation": CircleRegion((0.0, 0.0), 80.0),
                "target": CircleRegion((320.0, -40.0), 120.0),
            },
            SCREEN,
        )
        assert [s.payload() for s in shapes] == [
            {
                "kind": "circle",
                "role": "region",
                "name": "Fixation window",
                "center": [0.0, 0.0],
                "radius": 2.0,
            },
            {
                "kind": "circle",
                "role": "region",
                "name": "Target window",
                "center": [8.0, -1.0],
                "radius": 3.0,
            },
        ]

    def test_a_region_is_named_after_its_key_as_a_window(self):
        assert region_title("fixation") == "Fixation window"
        assert region_title("target_far") == "Target far window"
        # A key that already says window is not given a second one.
        assert region_title("fixation_window") == "Fixation window"
        assert region_title("x" * 200) == ("X" + "x" * 199)[:80]

    def test_a_region_that_cannot_be_drawn_is_a_bug_in_a_tasks_own_picture(self):
        with pytest.raises(ValueError, match="region 'bad' cannot be drawn"):
            region_shapes({"bad": CircleRegion((math.nan, 0.0), 10.0)}, SCREEN)

    def test_the_regions_only_picture_names_a_region_it_cannot_draw_and_goes_on(self, caplog):
        view = trial_view(
            schematic=None,
            regions={
                "bad": CircleRegion((math.nan, 0.0), 10.0),
                "ok": CircleRegion((0.0, 0.0), 40.0),
            },
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )
        assert [s["name"] for s in view["shapes"]] == ["Ok window"]
        assert view["undrawn"] == ["bad (window: circle center x must be finite, got nan)"]
        assert "region 'bad' cannot be drawn" in caplog.text

    def test_without_a_task_picture_only_the_regions_are_drawn(self):
        view = trial_view(
            schematic=None,
            regions={"fixation": CircleRegion((0.0, 0.0), 80.0)},
            screen=SCREEN,
            trial_index=3,
            attempt=2,
            condition={"side": "left", "contrast": 0.5, "odd": (1, 2)},
            t=12.5,
        )
        assert view["screen"] == {"width_dva": 48.0, "height_dva": 27.0}
        assert view["source"] == "regions"
        assert [(s["role"], s["name"]) for s in view["shapes"]] == [("region", "Fixation window")]
        # Nothing is said about the stimuli: no list of what was not drawn.
        assert view["undrawn"] == []
        assert view["condition"] == {"side": "left", "contrast": 0.5, "odd": "(1, 2)"}
        assert (view["trial_index"], view["attempt"], view["t"]) == (3, 2, 12.5)
        json.dumps(view, allow_nan=False)

    def test_no_task_picture_and_no_regions_is_an_empty_picture(self):
        view = trial_view(
            schematic=None,
            regions={},
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )
        assert (view["source"], view["shapes"], view["undrawn"]) == ("regions", [], [])

    def test_a_stimulus_describing_itself_is_not_drawn(self, tmp_path: Path):
        """The generic stimulus-outline fallback is gone: a stimulus's own
        ``schematic()`` is never asked, in the payload or in a session."""
        stimulus = _Describes([Circle((0, 0), 0.15)])
        monitor = _RecordingMonitor()

        def build(setup):
            from support import COMPLETED, RunForFrames

            return TrialPlan(
                phases=[RunForFrames(2, COMPLETED, emit_on_enter="FIX_ON")],
                stimuli={"fixation": stimulus},
                regions={"fixation": CircleRegion((0.0, 0.0), 80.0)},
            )

        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            build_trial=build,
            live_monitor=monitor,
            on_pause=lambda m: "resume",
        )
        harness.runner.run()
        (view,) = monitor.trial_views
        assert view["source"] == "regions"
        assert all(s["role"] == "region" for s in view["shapes"])
        assert stimulus.asked == 0

    def test_a_task_picture_is_drawn_as_given_with_the_regions_added(self):
        own = (Rect((0, 0), (10, 4), name="occluder"), Dots([(1, 1), (2, 2)], 0.1, name="dots"))
        view = trial_view(
            schematic=own,
            regions={"fixation": CircleRegion((0.0, 0.0), 80.0)},
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )
        assert view["source"] == "task"
        # The task's shapes exactly, in its order, then the windows.
        assert view["shapes"][:2] == [shape.payload() for shape in own]
        windows = [(s["role"], s["name"]) for s in view["shapes"][2:]]
        assert windows == [("region", "Fixation window")]
        assert view["undrawn"] == []

    def test_a_task_picture_that_draws_its_windows_gets_exactly_what_it_drew(self):
        own = (
            Rect((0, 0), (10, 4), name="occluder"),
            Circle((5, 0), 1.0, name="target", role="region"),
        )
        view = trial_view(
            schematic=own,
            regions={
                "fixation": CircleRegion((0.0, 0.0), 80.0),
                "target": CircleRegion((200.0, 0.0), 40.0),
            },
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )
        assert view["shapes"] == [shape.payload() for shape in own]
        assert view["undrawn"] == []

    def test_region_shapes_in_a_task_picture_match_what_alhazen_would_add(self):
        regions = {"fixation": CircleRegion((40.0, -80.0), 80.0)}
        added = trial_view(
            schematic=(Rect((0, 0), (1, 1)),),
            regions=regions,
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )["shapes"][1:]
        own = trial_view(
            schematic=(Rect((0, 0), (1, 1)), *region_shapes(regions, SCREEN)),
            regions=regions,
            screen=SCREEN,
            trial_index=1,
            attempt=1,
            condition={},
            t=0.0,
        )["shapes"][1:]
        assert added == own


# ---------------------------------------------------------------------------
# The gaze feed
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.batches: list[list] = []
        self.offers = 0

    def __call__(self, batch):
        self.offers += 1
        if self.accept:
            self.batches.append(batch)
        return self.accept

    @property
    def samples(self):
        return [s for batch in self.batches for s in batch]


def _frame(x, y, t):
    return InputFrame(gaze=(x, y), gaze_t=t)


class TestGazeFeed:
    def test_gaze_is_converted_to_dva_and_decimated_to_at_most_max_hz(self):
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=40.0, max_hz=60.0, flush_s=0.0)
        # A 120 Hz display, a new sample on every frame, for one second.
        for i in range(120):
            t = i / 120
            feed.note(1, i, t, _frame(40.0 * i, -80.0, t))
        samples = sink.samples
        assert 55 <= len(samples) <= 61
        assert samples[0] == (1, 0.0, 0.0, -2.0)
        assert all(b[1] - a[1] >= 0.9 / 60 for a, b in zip(samples, samples[1:], strict=False))

    def test_a_repeated_sample_is_not_sent_twice(self):
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=40.0, flush_s=0.0)
        for i in range(10):
            # The tracker delivered one sample; every frame repeats it.
            feed.note(1, i, i / 60, _frame(40.0, 40.0, 0.0))
        assert sink.samples == [(1, 0.0, 1.0, 1.0)]

    def test_a_gap_is_sent_once_per_run_of_missing_samples(self):
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=40.0, flush_s=0.0)
        frames = [
            _frame(0, 0, 0.0),
            InputFrame(),
            InputFrame(),
            InputFrame(gaze=(math.nan, 0.0), gaze_t=0.06),  # unusable: a gap too
            _frame(40, 0, 0.1),
        ]
        for i, inputs in enumerate(frames):
            feed.note(1, i, i * 0.025, inputs)
        assert sink.samples == [(1, 0.0, 0.0, 0.0), (1, 0.025, None, None), (1, 0.1, 1.0, 0.0)]

    def test_a_new_trial_starts_a_new_trail_and_flushes_the_old_one_first(self):
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=40.0, flush_s=10.0)  # time never flushes
        feed.note(1, 0, 0.0, _frame(0, 0, 0.0))
        feed.note(1, 1, 0.5, _frame(40, 0, 0.5))
        assert sink.offers == 1  # the session's first sample goes at once
        feed.note(2, 0, 1.0, _frame(80, 0, 1.0))
        # Trial 1's end went out at the change, before trial 2's first sample,
        # which waits for the next flush like any other.
        assert sink.batches[1] == [(1, 0.5, 1.0, 0.0)]
        feed.flush(20.0)
        assert sink.batches[-1] == [(2, 1.0, 2.0, 0.0)]

    def test_batches_leave_at_most_every_flush_s(self):
        sink = _Sink()
        feed = GazeFeed(sink, px_per_deg=40.0, max_hz=1000.0, flush_s=0.1)
        for i in range(100):
            t = i / 100
            feed.note(1, i, t, _frame(0, i, t))
        assert sink.offers <= 11
        # What has not left yet leaves with the next flush; nothing is lost.
        feed.flush(1.0, force=True)
        assert [s[1] for s in sink.samples] == [i / 100 for i in range(100)]

    def test_a_full_slot_keeps_the_samples_and_drops_the_oldest_beyond_capacity(self):
        sink = _Sink(accept=False)
        feed = GazeFeed(sink, px_per_deg=1.0, max_hz=1000.0, flush_s=0.0, capacity=5)
        for i in range(8):
            feed.note(1, i, float(i), _frame(float(i), 0.0, float(i)))
        assert sink.batches == [] and sink.offers == 8
        assert feed.dropped == 3
        sink.accept = True
        feed.note(1, 8, 8.0, _frame(8.0, 0.0, 8.0))
        # The newest five survive, oldest first; nothing older is resent.
        assert [s[2] for s in sink.samples] == [4.0, 5.0, 6.0, 7.0, 8.0]

    def test_refuses_a_meaningless_configuration(self):
        with pytest.raises(ValueError, match="px_per_deg"):
            GazeFeed(_Sink(), px_per_deg=0.0)
        with pytest.raises(ValueError, match="capacity"):
            GazeFeed(_Sink(), px_per_deg=1.0, capacity=0)


# ---------------------------------------------------------------------------
# The channels
# ---------------------------------------------------------------------------


class TestChannels:
    def test_publish_gaze_never_blocks_and_says_when_the_slot_is_full(self):
        controller = LiveMonitorController(auto_open=False)  # not started: nobody collects
        start = time.perf_counter()
        assert controller.publish_gaze([(1, 0.0, 0.0, 0.0)]) is True
        assert controller.publish_gaze([(1, 0.1, 0.0, 0.0)]) is False
        assert time.perf_counter() - start < 0.5

    def test_publish_trial_never_blocks_and_the_newest_picture_wins(self, caplog):
        controller = LiveMonitorController(auto_open=False)  # not started: nobody collects
        start = time.perf_counter()
        for index in range(1, 20):  # more than the channel holds
            controller.publish_trial({"trial_index": index})
        assert time.perf_counter() - start < 0.5
        # A full channel is said once, not once per trial.
        assert caplog.text.count("picture not sent") == 1
        # The child keeps the last one it takes off the channel.
        view = _TrialViewState()
        while True:
            try:
                view.set(controller._trial.get(timeout=1))
            except queue.Empty:
                break
        _seq, payload = view.wait_after(0, 0)
        assert json.loads(payload)["trial_index"] == 8

    def test_the_server_keeps_recent_gaze_numbered_and_counts_what_it_dropped(self):
        gaze = _GazeState(kept=4)
        gaze.add([(1, 0.0, 0.0, 0.0), (1, 0.1, 1.0, 0.0)])
        first = gaze.wait_after(0, 0)
        assert first == {"seq": 2, "missed": 0, "samples": [[1, 0.0, 0.0, 0.0], [1, 0.1, 1.0, 0.0]]}
        gaze.add([(1, t / 10, 0.0, 0.0) for t in range(2, 8)])
        later = gaze.wait_after(2, 0)
        assert later["seq"] == 8 and later["missed"] == 2 and len(later["samples"]) == 4
        assert gaze.wait_after(8, 0.01) is None

    def test_the_server_answers_with_the_newest_picture_only(self):
        view = _TrialViewState()
        assert view.wait_after(0, 0.01) is None
        view.set('{"trial_index": 1}')
        view.set('{"trial_index": 2}')
        assert view.wait_after(0, 0) == (2, '{"trial_index": 2}')
        assert view.wait_after(2, 0.01) is None

    def test_both_endpoints_end_to_end(self):
        controller = LiveMonitorController(auto_open=False)
        url = controller.start()
        token = url.partition("token=")[2]
        root = url.partition("/?")[0]

        def get(path: str):
            with urllib.request.urlopen(f"{root}{path}", timeout=5) as response:
                return response.status, dict(response.headers), response.read()

        try:
            controller.publish_trial({"trial_index": 7, "shapes": []})
            status, headers, body = get(f"/api/trial?token={token}&after=0&wait_ms=3000")
            assert status == 200 and json.loads(body)["trial_index"] == 7
            seq = int(headers["X-Trial-Seq"])
            status, _h, body = get(f"/api/trial?token={token}&after={seq}&wait_ms=50")
            assert (status, body) == (204, b"")

            assert controller.publish_gaze([(7, 1.0, 0.5, -0.5), (7, 1.02, None, None)])
            status, _h, body = get(f"/api/gaze?token={token}&after=0&wait_ms=3000")
            assert json.loads(body) == {
                "seq": 2,
                "missed": 0,
                "samples": [[7, 1.0, 0.5, -0.5], [7, 1.02, None, None]],
            }
            status, _h, body = get(f"/api/gaze?token={token}&after=2&wait_ms=50")
            assert (status, body) == (204, b"")

            for path in ("/api/trial?after=0", "/api/gaze?after=0"):
                with pytest.raises(urllib.error.HTTPError) as forbidden:
                    urllib.request.urlopen(f"{root}{path}", timeout=2)
                assert forbidden.value.code == 403
            with pytest.raises(urllib.error.HTTPError) as malformed:
                urllib.request.urlopen(f"{root}/api/gaze?token={token}&after=x", timeout=2)
            assert malformed.value.code == 400
        finally:
            controller.stop()


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class _RecordingMonitor:
    """The live monitor's interface, recording what the runner sends."""

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:0/"
        self.trial_views: list[dict] = []
        self.states: list[dict] = []

    def publish(self, state: dict) -> None:
        self.states.append(state)

    def publish_camera(self, pixels, t, eyes=None) -> None:
        pass

    def publish_trial(self, view: dict) -> None:
        self.trial_views.append(view)

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


class TestSessionPublishesEachTrial:
    def test_every_trial_publishes_its_picture_before_it_runs(self, tmp_path: Path):
        monitor = _RecordingMonitor()
        harness = SessionHarness(
            tmp_path, n_trials=3, live_monitor=monitor, on_pause=lambda m: "resume"
        )
        harness.runner.run()
        assert [v["trial_index"] for v in monitor.trial_views] == [1, 2, 3]
        view = monitor.trial_views[0]
        assert view["screen"]["width_dva"] == pytest.approx(SCREEN.width_px / SCREEN.px_per_deg)
        # This rig has no tracker: no gaze, and the camera box says why.
        assert view["gaze"] is False
        assert view["camera"] == {
            "live": False,
            "has_frames": False,
            "reason": "This rig has no eye tracker.",
        }
        json.dumps(view, allow_nan=False)

    def test_a_tracker_gives_its_own_reason_for_no_camera_during_trials(self, tmp_path: Path):
        from alhazen.devices.eyetracker import GazeSample
        from alhazen.devices.eyetracker.scripted import ScriptedTracker
        from support import FakeClock

        clock = FakeClock()
        tracker = ScriptedTracker([(0.0, GazeSample(gx=960.0, gy=540.0, t=0.0))], clock)
        monitor = _RecordingMonitor()
        harness = SessionHarness(
            tmp_path,
            n_trials=1,
            tracker=tracker,
            clock=clock,
            live_monitor=monitor,
            on_pause=lambda m: "resume",
        )
        harness.runner.run()
        (view,) = monitor.trial_views
        assert view["gaze"] is True
        assert view["camera"]["reason"] == "The scripted test tracker has no camera."
        assert view["camera"]["live"] is False


class TestTrackersSayWhyThereIsNoLiveCamera:
    def test_every_backend_states_its_reason(self):
        from alhazen.devices.automated import AutomatedGazeTracker
        from alhazen.devices.eyetracker.eyelink import EyeLinkTracker
        from alhazen.devices.eyetracker.mouse_sim import MouseSimTracker
        from alhazen.devices.eyetracker.scripted import ScriptedTracker
        from alhazen.devices.eyetracker.viewpixx import ViewPixxTracker

        for cls in (
            EyeLinkTracker,
            ViewPixxTracker,
            MouseSimTracker,
            ScriptedTracker,
            AutomatedGazeTracker,
        ):
            reason = cls.camera_while_recording
            assert isinstance(reason, str) and reason.endswith("."), cls
        assert "camera setup" in EyeLinkTracker.camera_while_recording
        assert "paused or calibrating" in ViewPixxTracker.camera_while_recording


class TestBuilderWiring:
    def test_the_frame_record_comes_first_then_the_gaze(self):
        from alhazen.session.builder import _also_feeding

        order: list[str] = []
        sink = _Sink()
        feed = GazeFeed(lambda b: order.append("gaze") or sink(b), px_per_deg=40.0, flush_s=0.0)
        hook = _also_feeding(lambda *a: order.append("record"), feed)
        hook(1, 0, 0.0, _frame(40.0, 0.0, 0.0))
        assert order == ["record", "gaze"]
        assert sink.samples == [(1, 0.0, 1.0, 0.0)]
