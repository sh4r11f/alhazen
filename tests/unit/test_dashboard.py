"""The dashboard's declarative plots, isolated server, and IPC contract."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from alhazen import DashboardPanel, DashboardSpec, RewardPulses
from alhazen.config.models import DashboardConfig
from alhazen.core.commands import Command
from alhazen.dashboard.runtime import DashboardCommand, DashboardController, dashboard_state
from alhazen.devices.eyetracker import GazeSample
from alhazen.devices.eyetracker.protocol import CameraFrame
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import SessionError
from alhazen.testing import FakeClock, ScriptedCommands
from support import SCREEN, SessionHarness


def _state(revision: int, status: str) -> dict:
    return dashboard_state(
        revision=revision,
        status=status,
        identity={"task_name": "test", "subject": "s1", "session": 1, "run": 1},
        trials=[{"trial_index": 1, "outcome": "CORRECT", "success": True}],
        events=[],
        spec=DashboardSpec(),
    )


class TestExtraPanels:
    """Precomputed payloads appended to the state — a live analysis's panels
    or the eye tracker's — drawn like every other panel."""

    def make(self, extra):
        return dashboard_state(
            revision=1,
            status="running",
            identity={"task_name": "t", "subject": "s1", "session": 1, "run": 1},
            trials=[],
            events=[],
            spec=DashboardSpec(include_defaults=False),
            extra_panels=extra,
        )

    def test_extra_panels_land_after_the_spec_panels(self):
        payload = {"form": "heatmap", "maps": [], "x_edges": [], "y_edges": []}
        state = self.make([{"title": "Receptive fields", "data": payload}])
        assert state["panels"][-1]["title"] == "Receptive fields"
        assert state["panels"][-1]["data"] == payload
        # Unfiled panels take the default section, so the sidebar can group
        # them; a filed one keeps its own.
        assert state["panels"][-1]["section"] == "Live analysis"
        filed = self.make([{"title": "RF", "section": "RF map", "data": payload}])
        assert filed["panels"][-1]["section"] == "RF map"

    def test_a_malformed_extra_panel_is_refused_loudly(self):
        with pytest.raises(SessionError, match="missing"):
            self.make([{"title": "no data key"}])

    def test_the_state_stays_serialisable(self):
        state = self.make(
            [
                {
                    "title": "RF",
                    "data": {
                        "form": "heatmap",
                        "maps": [{"name": "population", "matrix": [[1.0, None]]}],
                        "x_edges": [0, 1, 2],
                        "y_edges": [0, 1],
                    },
                }
            ]
        )
        json.dumps(state, allow_nan=False)


class TestSpecification:
    def test_defaults_and_custom_panels_resolve_in_order(self):
        custom = DashboardPanel(
            kind="grouped_mean", title="Bias", value="bias_dva", group="coherence"
        )
        spec = DashboardSpec(panels=(custom,))
        resolved = spec.resolved_panels()
        assert resolved[-1] == custom
        assert {p.kind for p in resolved} >= {"outcomes", "rewards", "scatter"}

    @pytest.mark.parametrize(
        ("kind", "message"),
        [("histogram", "require value"), ("scatter", "require x and y")],
    )
    def test_required_fields_are_validated(self, kind, message):
        with pytest.raises(ValueError, match=message):
            DashboardPanel(kind=kind, title="Broken")

    def test_port_validation(self):
        assert DashboardConfig(port=0).port == 0
        with pytest.raises(ValueError, match="port"):
            DashboardConfig(port=80)


class TestRuntime:
    def test_server_is_paused_only_authenticated_and_deduplicated(self):
        controller = DashboardController(auto_open=False)
        url = controller.start()
        token = url.partition("token=")[2]
        root = url.partition("/?")[0]
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                html = response.read().decode()
            assert "const STATIC_STATE = null;" in html
            controller.publish(_state(1, "running"))
            self._wait_for_revision(root, token, 1)
            with pytest.raises(urllib.error.HTTPError) as error:
                self._post(root, token, "manual_reward", "same")
            assert error.value.code == 409

            controller.publish(_state(2, "paused"))
            self._wait_for_revision(root, token, 2)
            assert self._post(root, token, "manual_reward", "same") == 202
            assert self._post(root, token, "manual_reward", "same") == 202
            # The eye-tracker procedures are buttons too, so their names must
            # pass the server's allow-list; a name it does not know is refused
            # before it can reach the session.
            assert self._post(root, token, "validate", "v1") == 202
            assert self._post(root, token, "drift_correct", "d1") == 202
            with pytest.raises(urllib.error.HTTPError) as unknown:
                self._post(root, token, "recalibrate", "x1")
            assert unknown.value.code == 400
            deadline = time.monotonic() + 2
            commands: list[DashboardCommand] = []
            while time.monotonic() < deadline and len(commands) < 3:
                commands += controller.poll_commands()
                time.sleep(0.01)
            assert [command.name for command in commands] == [
                "manual_reward",
                "validate",
                "drift_correct",
            ]
        finally:
            controller.stop()
        assert not controller.alive()

    def test_save_is_self_contained(self, tmp_path: Path):
        controller = DashboardController(auto_open=False)
        state = _state(3, "complete")
        controller.save(tmp_path, state)
        saved = json.loads((tmp_path / "dashboard_state.json").read_text())
        assert saved["status"] == "complete"
        html = (tmp_path / "dashboard.html").read_text()
        assert "__STATIC_STATE__" not in html
        assert '"status": "complete"' in html

    @staticmethod
    def _wait_for_revision(root: str, token: str, revision: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"{root}/api/state?token={token}", timeout=2) as response:
                state = json.load(response)
            if state.get("revision") == revision:
                return
            time.sleep(0.02)
        raise AssertionError(f"dashboard never reached revision {revision}")

    @staticmethod
    def _post(root: str, token: str, name: str, request_id: str) -> int:
        request = urllib.request.Request(
            f"{root}/api/command",
            data=json.dumps({"name": name, "request_id": request_id}).encode(),
            headers={"Content-Type": "application/json", "X-Alhazen-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status


class FakeDashboard:
    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.states: list[dict] = []
        self.stopped = False
        self.url = "http://127.0.0.1:0/"

    def publish(self, state: dict) -> None:
        self.states.append(state)

    def poll_commands(self) -> list[DashboardCommand]:
        names = self.batches.pop(0) if self.batches else []
        return [DashboardCommand(str(i), name) for i, name in enumerate(names)]

    def save(self, figures_dir: Path, state: dict) -> None:
        (figures_dir / "dashboard.html").write_text(state["status"])
        (figures_dir / "dashboard_state.json").write_text(json.dumps(state))

    def stop(self) -> None:
        self.stopped = True


class TestRunnerIntegration:
    def test_keyboard_pause_enables_browser_reward_then_resume(self, tmp_path: Path):
        reward = SimulatedReward()
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=1, commands=commands, reward=reward)
        # The first batch is drained and discarded on entering the pause, so
        # the commands that matter start in the second.
        dashboard = FakeDashboard([[], ["manual_reward"], ["resume"]])
        pulses = RewardPulses(n_pulses=1, pulse_ms=25, inter_pulse_ms=0)
        harness.runner._dashboard = dashboard
        harness.runner._manual_reward = lambda: reward.deliver(pulses)
        harness.runner._manual_reward_payload = {"pulses": pulses.model_dump(mode="json")}

        harness.runner.run()

        assert reward.deliveries == [pulses]
        assert [event.name for event in harness.collector.events].count("REWARD") == 1
        assert any(state["status"] == "paused" for state in dashboard.states)
        assert dashboard.states[-1]["status"] == "complete"
        assert dashboard.stopped
        assert (harness.paths.figures_dir / "dashboard.html").exists()

    def test_the_browser_runs_the_eye_tracker_procedures(self, tmp_path: Path):
        """Validate and Drift correct are buttons: each runs its procedure,
        publishes its progress while it runs, and reports its result in the
        notice and in the Eye tracker panels once it is done."""
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2 + 20.0, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        # The empty batch after each procedure is what the runner's drain
        # finds: nothing was clicked while the walk ran. The next click comes
        # once the buttons are back.
        dashboard = FakeDashboard([[], ["validate"], [], ["drift_correct"], [], ["resume"]])
        harness.runner._dashboard = dashboard

        harness.runner.run()

        statuses = [state["status"] for state in dashboard.states]
        # The walk published its progress under its own status, so the page
        # showed "validating: target 2 of 5" rather than a frozen "paused".
        assert "calibrating" in statuses
        progress = [s["message"] for s in dashboard.states if s["status"] == "calibrating"]
        assert any(m.startswith("validating: target") for m in progress)
        assert any(m.startswith("drift correcting: target") for m in progress)
        # Each result went out as the notice of a "paused" state.
        paused = [s["message"] for s in dashboard.states if s["status"] == "paused"]
        assert any(m.startswith("validation FAILED") for m in paused), paused
        assert any(m.startswith("drift correction applied: offset 0.50°") for m in paused), paused
        # ...and as the Eye tracker section's panels, drawn like any other.
        final = dashboard.states[-1]
        titles = {p["title"] for p in final["panels"] if p["section"] == "Eye tracker"}
        assert titles == {"Calibration", "Validation", "Drift correction"}
        by_title = {p["title"]: p["data"] for p in final["panels"]}
        assert by_title["Validation"]["form"] == "scatter"
        assert by_title["Drift correction"]["value"] == "0.50"
        assert dashboard.states[-1]["status"] == "complete"

    def test_a_calibrate_click_reports_the_verdict_in_the_notice(self, tmp_path: Path):
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        dashboard = FakeDashboard([[], ["calibrate"], [], ["resume"]])
        harness.runner._dashboard = dashboard

        harness.runner.run()

        paused = [s["message"] for s in dashboard.states if s["status"] == "paused"]
        # The scripted tracker reports no calibration result, and the notice
        # says exactly that rather than "Paused".
        assert any("result unknown" in m for m in paused), paused

    def test_the_in_trial_calibrate_key_reports_its_verdict_too(self, tmp_path: Path):
        """The C key mid-trial calibrates before the pause begins; the pause
        notice the browser then shows is the verdict, not the generic
        "Paused"."""
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.CALIBRATE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        dashboard = FakeDashboard([[], ["resume"]])
        harness.runner._dashboard = dashboard

        harness.runner.run()

        first_pause = next(s["message"] for s in dashboard.states if s["status"] == "paused")
        assert "result unknown" in first_pause, first_pause
        assert [e.name for e in harness.collector.events].count("CALIBRATION") == 1

    def test_a_click_on_a_rig_without_a_tracker_says_so(self, tmp_path: Path, caplog):
        """The server accepts Validate whether or not a tracker is wired, so
        the runner has to answer the click rather than crash or stay mute."""
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=1, commands=commands)
        dashboard = FakeDashboard([[], ["validate"], [], ["resume"]])
        harness.runner._dashboard = dashboard

        with caplog.at_level(logging.WARNING, logger="alhazen.session.runner"):
            harness.runner.run()

        paused = [s["message"] for s in dashboard.states if s["status"] == "paused"]
        assert "No eye tracker is wired." in paused, paused
        assert "validate requested while paused, but no eye tracker is wired" in caplog.text
        assert dashboard.states[-1]["status"] == "complete"


class CameraScriptedTracker(ScriptedTracker):
    """A scripted tracker with a camera, like the viewpixx: every read is a
    fresh frame stamped with the time it was taken."""

    def __init__(self, samples, clock: FakeClock) -> None:
        super().__init__(samples, clock)
        self.reads = 0

    def camera_frame(self) -> CameraFrame:
        self.reads += 1
        return CameraFrame(np.full((3, 4), self.reads, dtype=np.uint8), t=self._clock.now())


class TestCameraThroughThePause:
    """A tracker with a camera has its image refreshed while the session is
    paused, about once a second, so the Eye tracker tab shows the eye as it
    is now — and the saved copy never carries the picture."""

    def _run(self, tmp_path: Path, batches: list[list[str]]):
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2, gy=SCREEN.height_px / 2, t=0.0)
        tracker = CameraScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        dashboard = FakeDashboard(batches)
        harness.runner._dashboard = dashboard
        harness.runner.run()
        return harness, tracker, dashboard

    @staticmethod
    def _camera(state: dict) -> dict:
        return next(p["data"] for p in state["panels"] if p["title"] == "Camera")

    def test_the_image_is_read_again_about_once_a_second(self, tmp_path: Path):
        # The pause loop waits 10 ms of simulated time per poll: 250 empty
        # polls are 2.5 s of pause, long enough for two refreshes at 1 Hz.
        harness, tracker, dashboard = self._run(tmp_path, [[]] * 250 + [["resume"]])

        paused = [s for s in dashboard.states if s["status"] == "paused"]
        assert len(paused) == 3, [s["message"] for s in paused]
        read_at = [self._camera(s)["stats"][0]["value"] for s in paused]
        assert len(set(read_at)) == 3, read_at  # three different frames
        # The refresh republishes the standing notice, not a new one.
        assert {s["message"] for s in paused} == {"Paused — browser controls are enabled."}
        # About a second apart in the session's own clock.
        times = [float(v.removesuffix(" s")) for v in read_at]
        assert times[1] - times[0] == pytest.approx(1.0, abs=0.02)
        assert times[2] - times[1] == pytest.approx(1.0, abs=0.02)
        assert all(self._camera(s)["pixels"] for s in paused)
        assert dashboard.states[-1]["status"] == "complete"

    def test_a_rig_without_a_camera_is_not_republished(self, tmp_path: Path):
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        dashboard = FakeDashboard([[]] * 250 + [["resume"]])
        harness.runner._dashboard = dashboard
        harness.runner.run()
        assert [s["status"] for s in dashboard.states].count("paused") == 1

    def test_the_saved_copy_leaves_the_pixels_out(self, tmp_path: Path):
        harness, tracker, dashboard = self._run(tmp_path, [[], ["resume"]])

        saved = json.loads((harness.paths.figures_dir / "dashboard_state.json").read_text())
        camera = self._camera(saved)
        assert camera["form"] == "image" and camera["pixels"] == ""
        assert camera["note"] == "image left out of the saved copy"
        # The live pause page did carry the picture.
        live = next(s for s in dashboard.states if s["status"] == "paused")
        assert self._camera(live)["pixels"]


class TestStaleCommandsAreDiscarded:
    """A command accepted in the milliseconds between the browser seeing
    "paused" and the runner resuming used to sit in the queue and fire at the
    NEXT pause — a reward delivered, or a session quit, minutes after the
    click that asked for it."""

    def test_a_command_queued_before_the_pause_never_fires(self, tmp_path: Path):
        reward = SimulatedReward()
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=1, commands=commands, reward=reward)
        # A manual reward left over from before this pause, then a resume.
        dashboard = FakeDashboard([["manual_reward"], ["resume"]])
        harness.runner._dashboard = dashboard
        harness.runner._manual_reward = lambda: reward.deliver(RewardPulses(n_pulses=1))

        harness.runner.run()

        assert reward.deliveries == []
        assert dashboard.states[-1]["status"] == "complete"

    def test_a_click_queued_while_a_procedure_ran_never_fires(self, tmp_path: Path):
        """The browser keeps accepting clicks for ~0.2 s after a procedure
        starts, until it learns of the "calibrating" status. A double-click
        on Calibrate is the common case: without the drain, the second click
        ran a whole second calibration after the first, with nobody at the
        rig expecting it."""
        clock = FakeClock()
        gaze = GazeSample(gx=SCREEN.width_px / 2, gy=SCREEN.height_px / 2, t=0.0)
        tracker = ScriptedTracker([(0.0, gaze)], clock)
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(
            tmp_path, n_trials=1, commands=commands, tracker=tracker, clock=clock
        )
        # Second batch: a double-click, two Calibrates in one poll. Third: a
        # Validate the server accepted before it saw "calibrating". Then a
        # Resume clicked after the buttons came back, which must still work.
        dashboard = FakeDashboard([[], ["calibrate", "calibrate"], ["validate"], ["resume"]])
        harness.runner._dashboard = dashboard

        harness.runner.run()

        names = [event.name for event in harness.collector.events]
        assert names.count("CALIBRATION") == 1, names
        assert "VALIDATION" not in names, names
        assert "RESUMED" in names, names
        assert dashboard.states[-1]["status"] == "complete"

    def test_a_stale_quit_does_not_end_the_next_pause(self, tmp_path: Path):
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands)
        dashboard = FakeDashboard([["quit"], ["resume"]])
        harness.runner._dashboard = dashboard

        harness.runner.run()

        # The session resumed and ran its remaining trials rather than being
        # quit by a click nobody made during this pause.
        assert [row["trial_index"] for row in harness.recorder.trials] == [2, 3]


class TestSections:
    """Panels are read in groups, and the sidebar shows one group at a time —
    a dashboard that shows everything at once is a page to scroll rather than
    a thing to watch."""

    def test_each_kind_files_itself(self):
        by_kind = {
            panel.kind: panel.resolved_section for panel in DashboardSpec().resolved_panels()
        }
        assert by_kind == {
            "performance": "Session",
            "rewards": "Session",
            "outcomes": "Session",
            "histogram": "Behaviour",
            "responses": "Behaviour",
            "scatter": "Gaze",
            "vectors": "Gaze",
        }

    def test_a_task_can_file_its_own_panels_together(self):
        panel = DashboardPanel(
            kind="histogram", title="Pupil", value="pupil_mm", section="Pupillometry"
        )
        assert panel.resolved_section == "Pupillometry"

    def test_condition_panels_are_filed_under_conditions(self):
        resolved = DashboardSpec().resolved_panels(["coherence"])
        automatic = [p for p in resolved if "by coherence" in p.title]
        assert automatic and all(p.resolved_section == "Conditions" for p in automatic)

    def test_the_section_travels_with_every_panel(self):
        # The page groups by this, so it has to be on the wire even though the
        # panel model stores it as None until it is resolved.
        state = dashboard_state(
            revision=1,
            status="running",
            identity={},
            trials=[],
            events=[],
            spec=DashboardSpec(),
        )
        assert all(panel["section"] for panel in state["panels"])


class TestPage:
    """The page is assembled from three source files at import time and must
    end up self-contained: the server answers no request for an asset, and the
    copy saved to figures/ opens from a filesystem with no server at all."""

    def page(self) -> str:
        from alhazen.dashboard.runtime import page_html

        return page_html("null")

    def test_the_stylesheet_and_renderer_are_inlined(self):
        html = self.page()
        assert "__STYLE__" not in html and "__SCRIPT__" not in html
        assert "--series-1:" in html  # the stylesheet
        assert "function drawLineChart(" in html  # the renderer

    def test_every_allowed_command_has_a_button(self):
        # A command the server would accept but the page has no button for is
        # a control nobody can reach; a button for a command the server
        # refuses is a control that silently does nothing. The two lists are
        # kept equal so neither can happen.
        from alhazen.dashboard.runtime import _ALLOWED_COMMANDS

        html = self.page()
        buttons = set(re.findall(r'data-command="([a-z_]+)"', html))
        assert buttons == _ALLOWED_COMMANDS

    def test_the_eye_tracker_panels_can_be_drawn(self):
        # The monitor (session/eyetracker.py) sends a camera picture as an
        # ``image`` form and its verdicts as ``stat`` tiles with a status;
        # a form the renderer lacks draws as "Nothing to draw", so both are
        # asserted against the asset.
        html = self.page()
        assert "image: drawImage," in html
        assert "putImageData" in html
        assert "tile.dataset.status" in html
        # The status a procedure publishes under has its own colour, and the
        # notice shows the procedure's progress instead of the pause hint.
        assert '#status[data-state="calibrating"]' in html
        assert "state.status === 'calibrating'" in html

    def test_nothing_is_fetched_from_the_network(self):
        # A rig has no internet and a saved figure outlives any CDN.
        html = self.page()
        assert "https://" not in html
        assert "<script src" not in html and "<link" not in html

    def test_the_grid_rebuild_preserves_the_readers_scroll_position(self):
        """Every completed trial rebuilds the panel grid, and a browser laying
        out the momentarily-empty document clamps scroll to zero. At one trial
        every few seconds that makes any panel below the fold unreadable.

        Asserted against the asset rather than in a browser, because there is
        no JS test harness here and a missing three-line guard is worth
        catching cheaply. It checks the ORDER too: restoring before the panels
        are painted would clamp against a height that is about to grow.
        """
        from alhazen.dashboard import runtime

        script = (runtime._ASSETS / "dashboard.js").read_text()
        render = script[script.index("function render()") :]
        render = render[: render.index("\n}")]

        assert "scrollingElement" in render, "render() does not preserve scroll position"
        assert render.index("scrollTop") < render.index("replaceChildren"), (
            "scroll position must be captured before the grid is rebuilt"
        )
        assert render.rindex("scrollTop") > render.rindex("paintPanel"), (
            "scroll position must be restored after the panels are painted"
        )

    def test_missing_assets_fail_loudly_rather_than_serving_a_blank_page(self, monkeypatch):
        from alhazen.dashboard import runtime

        runtime._page_template.cache_clear()
        monkeypatch.setattr(runtime, "_ASSETS", Path("/nonexistent/assets"))
        try:
            with pytest.raises(SessionError, match="dashboard assets are missing"):
                runtime.page_html("null")
        finally:
            runtime._page_template.cache_clear()


class TestPanelData:
    """Every panel travels with the data it draws, computed over the whole
    session — the page renders, it does not analyse."""

    def state(self, *, max_rows=None, panels=None):
        trials = [
            {
                "trial_index": i,
                "outcome": "CORRECT" if i % 5 else "FIX_BREAK",
                "completed": i % 5 != 0,
                "success": i % 5 != 0,
                "rt_ms": 300 + i,
            }
            for i in range(1, 51)
        ]
        events = [
            {
                "trial_index": i,
                "event": "REWARD",
                "t": float(i),
                "payload_json": json.dumps(
                    {"manual": False, "pulses": {"n_pulses": 2, "pulse_ms": 100}}
                ),
            }
            for i in range(1, 51)
            if i % 5
        ]
        spec = DashboardSpec(panels=tuple(panels or ()), include_defaults=panels is None)
        return dashboard_state(
            revision=1,
            status="running",
            identity={},
            trials=trials,
            events=events,
            spec=spec,
            max_rows=max_rows,
        )

    def panel(self, state, kind):
        return next(panel for panel in state["panels"] if panel["kind"] == kind)

    def test_each_panel_carries_its_drawn_data(self):
        state = self.state()
        assert self.panel(state, "performance")["data"]["form"] == "line"
        assert self.panel(state, "outcomes")["data"]["form"] == "bars"

    def test_the_truncated_echo_never_truncates_the_plots(self):
        # max_rows bounds what a between-trial update costs to serialise. If
        # the panels were computed from that window instead of the session, a
        # long run's cumulative reward curve would start partway up.
        state = self.state(max_rows=5)

        assert len(state["trials"]) == 5 and state["n_trials"] == 50
        assert len(state["events"]) == 5 and state["n_events"] == 40
        assert self.panel(state, "outcomes")["data"]["total"] == 50
        reward = self.panel(state, "rewards")["data"]
        assert reward["series"][0]["points"][0] == [0.0, 0.0]
        assert reward["series"][0]["points"][-1][1] == pytest.approx(40 * 0.2)

    def test_condition_fields_colour_and_group_the_defaults(self):
        # The runner learns the factors from the conditions it served, so a
        # task gets condition-aware monitoring without declaring anything.
        state = dashboard_state(
            revision=1,
            status="running",
            identity={},
            trials=[
                {
                    "trial_index": i,
                    "side": "left" if i % 2 else "right",
                    "completed": True,
                    "success": i % 3 != 0,
                    "endpoint_x_dva": 8.0,
                    "endpoint_y_dva": 0.0,
                }
                for i in range(1, 11)
            ],
            events=[],
            spec=DashboardSpec(),
            condition_fields=["side"],
        )
        titles = [panel["title"] for panel in state["panels"]]

        assert "Accuracy by side" in titles and "Landing error by side" in titles
        landings = next(p for p in state["panels"] if p["kind"] == "scatter")
        assert landings["color_by"] == "side"
        assert [s["name"] for s in landings["data"]["series"]] == ["left", "right"]

    def test_without_conditions_nothing_is_added_or_coloured(self):
        state = self.state()
        assert all("by " not in panel["title"] for panel in state["panels"])
        assert next(p for p in state["panels"] if p["kind"] == "scatter")["color_by"] is None

    def test_a_task_panel_is_resolved_and_computed_like_any_other(self):
        panel = DashboardPanel(kind="stat", title="Median RT", value="rt_ms", agg="median")
        state = self.state(panels=[panel])
        assert self.panel(state, "stat")["data"]["value"] == "326"  # median of 301..350


class TestTheChildDoesNotLeak:
    """The dashboard is a child PROCESS. Only `display.open()` was guarded, so
    anything that failed after `controller.start()` — a tracker that will not
    connect, a measured refresh rate that disagrees with the config, an event
    name the rig maps but the task never declares — left a server running with
    nothing driving it, and the next session's port already taken."""

    def rig(self, tmp_path, **devices):
        from alhazen.config.models import DashboardConfig, DevicesConfig, DisplayConfig, RigConfig
        from support import MONITOR

        return RigConfig(
            monitor=MONITOR,
            display=DisplayConfig(backend="simulated"),
            dashboard=DashboardConfig(enabled=True, auto_open=False),
            devices=DevicesConfig(**devices),
            data_root=tmp_path,
        )

    def build(self, tmp_path, monkeypatch, rig, **kwargs):
        from alhazen.config.models import Duration
        from alhazen.core.events import EventSchema
        from alhazen.paradigms.base import Condition, SimpleSequence
        from alhazen.session import builder as builder_module
        from alhazen.task.plan import TrialPlan
        from support import COMPLETED, RunForFrames

        started: list[object] = []
        stopped: list[object] = []

        class SpyController:
            def __init__(self, port=0, auto_open=True):
                self.url = "http://127.0.0.1:0/"

            def start(self):
                started.append(self)

            def stop(self):
                stopped.append(self)

            def publish(self, state):
                pass

        monkeypatch.setattr(builder_module, "DashboardController", SpyController)

        def go():
            return builder_module.build_session(
                rig=rig,
                subject="t01",
                session=1,
                run=1,
                task_name="test-task",
                task_params=Duration(ms=1),  # any model; nothing reads it here
                event_schema=EventSchema(("FIX_ON",)),
                build_trial=lambda setup: TrialPlan(phases=[RunForFrames(0, COMPLETED)]),
                make_source=lambda params, rng: SimpleSequence(
                    [Condition({})], n_repeats=1, rng=rng
                ),
                simulated_frame_period_s=0.0,
                date_yyyymmdd="20260826",
                **kwargs,
            )

        return go, started, stopped

    def test_a_successful_build_leaves_the_child_running(self, tmp_path, monkeypatch):
        go, started, stopped = self.build(tmp_path, monkeypatch, self.rig(tmp_path))

        runner = go()

        assert runner is not None
        assert len(started) == 1 and stopped == []

    def test_a_failure_after_start_stops_the_child(self, tmp_path, monkeypatch):
        from alhazen.config.models import SyncHwConfig
        from alhazen.errors import ConfigError

        # A sync line naming an event the task never declares: a validation
        # failure that happens well after the dashboard has been started.
        rig = self.rig(
            tmp_path, sync=SyncHwConfig(backend="simulated", event_lines={"NOPE": "Dev1/line0"})
        )
        go, started, stopped = self.build(tmp_path, monkeypatch, rig)

        with pytest.raises(ConfigError, match="NOPE"):
            go()

        assert len(started) == 1 and len(stopped) == 1

    def test_a_tracker_that_will_not_connect_stops_the_child(self, tmp_path, monkeypatch):
        from alhazen.errors import TrackerError

        class DeadTracker:
            def connect(self):
                raise TrackerError("no link to the Host PC")

        go, started, stopped = self.build(
            tmp_path, monkeypatch, self.rig(tmp_path), tracker=DeadTracker()
        )

        with pytest.raises(TrackerError):
            go()

        assert len(started) == 1 and len(stopped) == 1
