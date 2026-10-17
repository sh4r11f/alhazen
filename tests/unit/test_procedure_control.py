"""The live monitor's hold on a running eye-tracker procedure
(session/procedure_control.py), through the session's EyeTrackerMonitor.

2026-10-17 (Sharif): during a calibration the page offered no buttons and its
Trial view showed the last trial. Now Resume/Back/Pause reach the walk, the
Trial view shows the procedure's targets with the one on screen highlighted,
the camera as live and, during a validation or drift correction, the gaze.
"""

from __future__ import annotations

from typing import Any

from alhazen.live_monitor.runtime import LiveMonitorCommand
from alhazen.session.operator_commands import Capabilities, OperatorCommands
from alhazen.session.procedure_control import ProcedureControl
from support import FRAME_S, SCREEN
from test_session_eyetracker import FollowingTracker, HookedTracker, session  # noqa: F401


class PageEvery:
    """A page that clicks ``name`` once a second of walk time (every 60
    frames), accepted under "calibrating"; ``limit`` clicks in all."""

    def __init__(self, name: str, limit: int = 99) -> None:
        self.name = name
        self.limit = limit
        self.polls = 0
        self.sent = 0
        self.acks: list[list[dict]] = []

    def poll_commands(self) -> list[LiveMonitorCommand]:
        self.polls += 1
        if self.polls % round(1.0 / FRAME_S) == 0 and self.sent < self.limit:
            self.sent += 1
            return [LiveMonitorCommand(f"c{self.sent}", self.name, status="calibrating")]
        return []

    def publish_commands(self, records: list[dict]) -> None:
        self.acks.append(records)


def wire(s: Any, page: Any) -> dict[str, Any]:
    operator = OperatorCommands(page)
    events: list = []
    operator.bind(
        emit=lambda name, payload: events.append((name, payload)),
        capabilities=Capabilities(tracker=True),
        clock=s.clock,
    )
    views: list[dict] = []
    gaze: list[list] = []
    controls: list[dict] = []
    s.monitor.publisher = lambda status, message: controls.append(operator.controls(status))
    s.monitor.control = ProcedureControl(
        operator=operator,
        screen=SCREEN,
        clock=s.clock,
        publish_view=views.append,
        camera=lambda: {"live": True, "has_frames": True, "reason": "live"},
        trial_index=lambda: 7,
        gaze_sink=lambda batch: gaze.append(batch) or True,
    )
    return {
        "operator": operator,
        "events": events,
        "views": views,
        "gaze": gaze,
        "controls": controls,
    }


class CommandableTracker(HookedTracker):
    """A tracker whose own walk reads the page (set_command_source)."""

    def __init__(self, clock) -> None:
        super().__init__(clock)
        self.sources: list = []

    def set_command_source(self, source) -> None:
        self.sources.append(source)


class TestAValidationThePageDrives:
    def test_resume_accepts_each_target_and_the_trial_view_follows(self, session) -> None:
        s = session()
        page = PageEvery("resume")
        w = wire(s, page)
        result = s.monitor.validate(advance="manual")
        assert len(result.measured) == 5 and not result.aborted
        applied = [p for name, p in w["events"] if name == "OPERATOR_COMMAND"]
        assert [p["result"] for p in applied] == ["applied"] * 5
        assert all(p["name"] == "resume" for p in applied)
        # While it ran the page could press Pause, Resume and Back; after, nothing.
        assert w["controls"][0]["available"] == ["pause", "resume", "back"]
        assert w["controls"][0]["keys"]["resume"] == "space"
        assert w["operator"].controls("calibrating")["available"] == []
        views = w["views"]
        first, last = views[0], views[-1]
        assert first["source"] == "procedure" and first["procedure"]["kind"] == "validation"
        assert first["trial_index"] == 7 and first["gaze"] is True
        assert len(first["shapes"]) == 5 and first["camera"]["live"] is True
        steps = [v["procedure"]["step"] for v in views if v["procedure"]["step"]]
        assert steps[:1] == [1] and max(steps) == 5
        on_screen = [s_["name"] for s_ in views[-2]["shapes"] if s_["role"] == "stimulus"]
        assert on_screen == ["Target on screen"]
        assert last["procedure"]["ended"] is True
        assert last["procedure"]["line"].startswith("validation")
        # The gaze the walk measured reached the Trial view, as trial 7's.
        samples = [sample for batch in w["gaze"] for sample in batch]
        assert samples and {sample[0] for sample in samples} == {7}

    def test_pause_on_the_page_stops_the_walk(self, session) -> None:
        s = session()
        w = wire(s, PageEvery("pause", limit=1))
        result = s.monitor.validate(advance="manual")
        assert result.aborted
        assert [p["name"] for n, p in w["events"] if n == "OPERATOR_COMMAND"] == ["pause"]


class TestACalibration:
    def test_a_walk_that_reads_the_page_gets_the_source_and_its_targets(self, session) -> None:
        s = session(tracker_cls=CommandableTracker, validate_after_calibration=False)
        w = wire(s, PageEvery("resume", limit=0))
        s.monitor.calibrate()
        assert len(s.tracker.sources) == 2
        assert s.tracker.sources[0] is not None and s.tracker.sources[-1] is None
        view = w["views"][0]
        assert view["procedure"]["reads_page"] is True and len(view["shapes"]) == 5
        assert view["gaze"] is False  # no model to read gaze through yet
        # The hook's "target 1 of 5" highlights the first target.
        stepped = [v for v in w["views"] if v["procedure"]["step"] == 1]
        assert stepped and stepped[0]["shapes"][0]["role"] == "stimulus"

    def test_a_calibration_that_runs_elsewhere_offers_nothing_and_says_why(self, session) -> None:
        s = session(tracker_cls=HookedTracker, validate_after_calibration=False)
        w = wire(s, PageEvery("resume", limit=0))
        s.monitor.calibrate()
        first = w["controls"][0]
        assert first["available"] == []
        assert "own setup screen" in first["refused"]["resume"]
        assert w["views"][0]["shapes"] == [] and not w["views"][0]["procedure"]["targets_known"]
