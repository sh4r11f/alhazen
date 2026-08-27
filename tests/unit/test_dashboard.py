"""The dashboard's declarative plots, isolated server, and IPC contract."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alhazen import DashboardPanel, DashboardSpec, RewardPulses
from alhazen.config.models import DashboardConfig
from alhazen.core.commands import Command
from alhazen.dashboard.runtime import DashboardCommand, DashboardController, dashboard_state
from alhazen.devices.reward import SimulatedReward
from alhazen.errors import SessionError
from alhazen.testing import ScriptedCommands
from support import SessionHarness


def _state(revision: int, status: str) -> dict:
    return dashboard_state(
        revision=revision,
        status=status,
        identity={"task_name": "test", "subject": "s1", "session": 1, "run": 1},
        trials=[{"trial_index": 1, "outcome": "CORRECT", "success": True}],
        events=[],
        spec=DashboardSpec(),
    )


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
            deadline = time.monotonic() + 2
            commands = []
            while time.monotonic() < deadline and not commands:
                commands = controller.poll_commands()
                time.sleep(0.01)
            assert [command.name for command in commands] == ["manual_reward"]
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
