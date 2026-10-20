"""This machine's rig: one flat rig per dashboard workspace (cli/machine_rig.py),
a session's own copy of it, and the event lines carried between sessions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from alhazen.cli import machine_rig as mr
from alhazen.cli import workspace as workspace_module
from alhazen.cli.main import _read_rig_origin, main
from alhazen.cli.workspace import Launch, Workspace
from alhazen.cli.workspace_data import data_roots

RIG = Path(__file__).parents[2] / "examples/minimal_fixation/rig-sim.yaml"
SYNC = (
    "\ndevices:\n  eyetracker:\n    backend: mouse_sim\n"
    "  sync:\n    backend: simulated\n    pulse_ms: 2.0\n"
)


def lab_text(events: bool = False) -> str:
    """The example simulated rig with a sync device and a tracker; with
    ``events``, two event lines and a photodiode, as an experiment's rig."""
    text = RIG.read_text(encoding="utf-8") + SYNC
    if events:
        text += "    event_lines:\n      FIX_ON: Dev1/port0/line0\n      BOGUS: Dev1/port0/line1\n"
    return text


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workspace_module,
        "probe_interpreter",
        lambda python, path: {
            # Old enough that a one-task project is launched without --task
            # (no schema read); the capability is what is under test.
            "alhazen_version": "1.9.0",
            "python_version": "stub",
            "shared_rigs": [],
            "capabilities": ["rig-origin"],
        },
    )
    root = tmp_path / "exp"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_text(RIG.read_text(encoding="utf-8"))
    (root / "configs/rig-lab.yaml").write_text(lab_text(events=True))
    (root / "configs/task.yaml").write_text("speed: 3\n")
    (root / "run.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]), flush=True)\n")
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    # Every task here declares these events (as its schema would say).
    monkeypatch.setattr(space, "_declared_events", lambda project, task: ["FIX_ON", "TRIAL_START"])
    yield space
    space.close()


def set_up(space: Workspace, rig: str = "configs/rig-sim.yaml", name: str = "booth") -> dict:
    pid = space.projects[0]["id"]
    return space.setup_machine({"name": name, "template": f"{pid}:{rig}"})


def finish(space, run):
    space.worker.join(timeout=20)
    assert not space.worker.is_alive()
    return space.detail(run["id"])


class TestTheMachineFile:
    def test_a_shared_rig_is_copied_byte_for_byte(self, tmp_path):
        machine = mr.MachineRig(tmp_path)
        status = machine.setup(
            "lab", mr.Path(RIG), shared=None, template={"kind": "shared", "label": "x"}
        )
        assert status["configured"] and status["name"] == "lab"
        assert Path(status["file"]).name == "rig-lab.yaml"
        assert Path(status["file"]).read_text() == RIG.read_text()
        assert status["template"]["sha256"]
        with pytest.raises(mr.MachineRigError, match="already has a rig"):
            machine.setup("lab", RIG, shared=None, template={})

    def test_event_names_are_left_out_and_returned(self, tmp_path):
        source = tmp_path / "rig-lab.yaml"
        source.write_text(lab_text(events=True))
        status = mr.MachineRig(tmp_path / "state").setup(
            "lab", source, shared=None, template={"label": "exp/lab"}
        )
        assert status["left_out"] == {
            "event_lines": {"FIX_ON": "Dev1/port0/line0", "BOGUS": "Dev1/port0/line1"}
        }
        assert "event_lines" not in status["values"]["devices"]["sync"]
        assert "Event names were left out" in status["text"]

    def test_an_extending_rig_is_written_out_whole(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "rig-base.yaml").write_text(RIG.read_text())
        source = tmp_path / "configs" / "rig-lab.yaml"
        source.parent.mkdir()
        source.write_text("extends: base\nmonitor:\n  distance_cm: 70.0\n")
        status = mr.MachineRig(tmp_path / "state").setup(
            "lab", source, shared={"base": shared / "rig-base.yaml"}, template={"label": "e/lab"}
        )
        assert "extends" not in status["values"]
        assert status["values"]["monitor"]["distance_cm"] == 70.0
        assert status["values"]["monitor"]["width_px"] == 1920
        assert status["template"]["extends"] == "base"

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("extends: lab\n", "extends nothing"),
            (lab_text(events=True), "names events"),
            ("monitor: 3\n", "invalid config"),
            ("- a\n", "YAML mapping"),
        ],
    )
    def test_the_editor_refuses_what_a_machine_rig_cannot_be(self, tmp_path, text, match):
        machine = mr.MachineRig(tmp_path)
        machine.setup("lab", RIG, shared=None, template={})
        with pytest.raises(mr.MachineRigError, match=match):
            machine.save_text(text, None, by="test")

    def test_a_save_keeps_the_previous_file_and_checks_it_was_not_changed(self, tmp_path):
        machine = mr.MachineRig(tmp_path)
        status = machine.setup("lab", RIG, shared=None, template={})
        edited = status["text"].replace("distance_cm: 60.0", "distance_cm: 61.0")
        with pytest.raises(mr.MachineRigError, match="changed since"):
            machine.save_text(edited, "0" * 64, by="test")
        saved = machine.save_text(edited, status["sha256"], by="test")
        assert saved["values"]["monitor"]["distance_cm"] == 61.0
        history = list((tmp_path / "machine-rig" / "history").iterdir())
        assert [p.read_text() for p in history] == [RIG.read_text()]
        assert saved["saves"][-1]["changes"] == ["monitor.distance_cm"]

    def test_saving_settings_drops_event_names(self, tmp_path):
        machine = mr.MachineRig(tmp_path)
        source = tmp_path / "rig-x.yaml"
        source.write_text(lab_text())
        status = machine.setup("lab", source, shared=None, template={})
        values = yaml.safe_load(lab_text(events=True))
        values["monitor"]["distance_cm"] = 65.0
        saved = machine.save_values(values, status["sha256"], by="run page")
        assert saved["saved"] == ["monitor.distance_cm"]
        assert "event_lines" not in saved["values"]["devices"]["sync"]

    def test_two_rig_files_are_refused_rather_than_one_picked(self, tmp_path):
        machine = mr.MachineRig(tmp_path)
        machine.setup("lab", RIG, shared=None, template={})
        (tmp_path / "machine-rig" / "rig-other.yaml").write_text(RIG.read_text())
        assert machine.status()["configured"] is False
        assert "a machine has one rig" in machine.status()["error"]

    def test_changes_are_listed_setting_by_setting(self):
        changes = mr.rig_changes(
            {"a": {"b": 1, "c": [1]}, "d": 2}, {"a": {"b": 2, "c": [1]}, "e": 3}
        )
        assert changes == [
            {"path": "a.b", "machine": 1, "session": 2},
            {"path": "d", "machine": 2, "absent": "session"},
            {"path": "e", "session": 3, "absent": "machine"},
        ]


class TestSessions:
    def test_a_launch_needs_this_machines_rig(self, workspace):
        pid = workspace.projects[0]["id"]
        with pytest.raises(ValueError, match="no rig yet"):
            workspace.start(Launch(project=pid, mode="simulate"))

    def test_a_session_runs_its_own_copy_and_records_what_changed(self, workspace):
        set_up(workspace, "configs/rig-lab.yaml", "lab")
        machine_file = workspace.machine.file()
        # A reward calibration kept beside the machine's file is read beside
        # the session's copy too.
        (machine_file.parent / "rig-lab.reward.yaml").write_text("widths: {}\n")
        pid = workspace.projects[0]["id"]
        values = workspace.session_rig(pid, None, "simulate")["values"]
        values["monitor"]["distance_cm"] = 75.0
        run = finish(
            workspace, workspace.start(Launch(project=pid, mode="simulate", rig_values=values))
        )
        folder = Path(run["directory"])
        assert run["rig"] == "machine" and run["rig_source"] == "machine"
        assert run["rig_name"] == "lab"
        assert run["rig_changes"][0] == {
            "path": "monitor.distance_cm",
            "machine": 60.0,
            "session": 75.0,
        }
        argv = json.loads(run["log"].splitlines()[0])
        assert argv[argv.index("--rig") + 1] == str(folder / "rig-lab.yaml")
        assert argv[argv.index("--rig-origin") + 1] == str(folder / "rig-origin.json")
        assert (folder / "rig-lab.reward.yaml").is_file()
        assert (folder / "rig.yaml").read_bytes() == (folder / "rig-lab.yaml").read_bytes()
        assert "distance_cm: 75.0" in (folder / "rig-lab.yaml").read_text()
        origin = json.loads((folder / "rig-origin.json").read_text())
        assert origin["kind"] == "machine" and origin["machine"] == "lab"
        assert origin["sha256"] == mr.sha256_text(machine_file.read_text())
        # The machine's own file is untouched.
        assert "distance_cm: 60.0" in machine_file.read_text()

    def test_an_unchanged_rig_is_the_machines_file_byte_for_byte(self, workspace):
        set_up(workspace)
        pid = workspace.projects[0]["id"]
        run = finish(workspace, workspace.start(Launch(project=pid, mode="simulate")))
        assert (Path(run["directory"]) / "rig-booth.yaml").read_text() == RIG.read_text()
        assert run["rig_changes"] == []

    def test_no_rig_origin_for_an_alhazen_that_does_not_take_it(self, workspace, monkeypatch):
        set_up(workspace)
        project = workspace.projects[0]
        monkeypatch.setitem(project, "capabilities", [])
        run = finish(workspace, workspace.start(Launch(project=project["id"], mode="simulate")))
        assert "--rig-origin" not in run["command"]
        assert not (Path(run["directory"]) / "rig-origin.json").exists()
        # The workspace's own records still say where the rig came from.
        launch = json.loads((Path(run["directory"]) / "launch.json").read_text())
        assert launch["rig_origin"]["machine"] == "booth"
        assert run["machine_rig"]["file"] == str(workspace.machine.file())

    def test_a_session_cannot_change_whether_real_data_is_collected(self, workspace):
        set_up(workspace)
        pid = workspace.projects[0]["id"]
        values = workspace.session_rig(pid, None, "simulate")["values"]
        values["real_data"] = False
        with pytest.raises(ValueError, match="real data may be collected is this machine"):
            workspace.start(Launch(project=pid, mode="simulate", rig_values=values))
        assert not list((workspace.directory / "runs").iterdir())

    def test_run_mode_on_a_development_machine_is_refused(self, workspace):
        status = set_up(workspace)
        text = status["text"] + "real_data: false\n"
        workspace.save_machine({"text": text, "sha256": status["sha256"]})
        pid = workspace.projects[0]["id"]
        with pytest.raises(ValueError, match="this machine's rig \\(booth\\) is a development"):
            workspace.start(Launch(project=pid, mode="run", subject="s01", initials="AB"))

    def test_measure_runs_the_machines_file_and_refuses_changes(self, workspace):
        set_up(workspace)
        pid = workspace.projects[0]["id"]
        values = workspace.session_rig(pid, None, "measure")["values"]
        values["monitor"]["distance_cm"] = 99.0
        with pytest.raises(ValueError, match="Measure rig measures this machine's rig"):
            workspace.start(Launch(project=pid, mode="measure", rig_values=values))
        run = finish(workspace, workspace.start(Launch(project=pid, mode="measure")))
        argv = json.loads(run["log"].splitlines()[0])
        assert argv[argv.index("--rig") + 1] == str(workspace.machine.file())
        assert run["rig_carried"] is None

    def test_an_undeclared_event_is_refused_before_anything_is_written(self, workspace):
        set_up(workspace, "configs/rig-lab.yaml", "lab")
        pid = workspace.projects[0]["id"]
        values = workspace.session_rig(pid, None, "simulate")["values"]
        values["devices"]["sync"]["event_lines"] = {"STIM_ON": "Dev1/port0/line0"}
        with pytest.raises(ValueError, match="never declares: STIM_ON"):
            workspace.start(Launch(project=pid, mode="simulate", rig_values=values))
        assert not list((workspace.directory / "runs").iterdir())

    def test_a_named_rig_still_launches_as_before(self, workspace):
        pid = workspace.projects[0]["id"]
        run = finish(
            workspace,
            workspace.start(Launch(project=pid, mode="simulate", rig="configs/rig-sim.yaml")),
        )
        assert run["rig"] == "configs/rig-sim.yaml" and run["rig_source"] == "experiment"
        assert "rig_changes" not in run


class TestEventLinesCarryOver:
    def test_first_from_the_experiments_rig_of_this_machines_name(self, workspace):
        status = set_up(workspace, "configs/rig-lab.yaml", "lab")
        assert "event_lines" not in status["values"]["devices"]["sync"]
        pid = workspace.projects[0]["id"]
        start = workspace.session_rig(pid, None, "simulate")
        # BOGUS is not an event of the task, so it is left out, and said.
        assert start["values"]["devices"]["sync"]["event_lines"] == {"FIX_ON": "Dev1/port0/line0"}
        assert start["events_source"]["kind"] == "experiment-rig"
        assert start["dropped"] == ["BOGUS: not an event of this task"]

    def test_then_from_the_last_session_of_the_same_task(self, workspace):
        set_up(workspace, "configs/rig-lab.yaml", "lab")
        pid = workspace.projects[0]["id"]
        values = workspace.session_rig(pid, None, "simulate")["values"]
        values["devices"]["sync"]["event_lines"] = {"TRIAL_START": "Dev1/port0/line3"}
        run = finish(
            workspace,
            workspace.start(
                Launch(project=pid, mode="test", subject="s1", initials="AB", rig_values=values)
            ),
        )
        assert run["rig_carried"] == {
            "devices.sync.event_lines": {"TRIAL_START": "Dev1/port0/line3"}
        }
        assert run["rig_events_source"]["edited"] is True
        start = workspace.session_rig(pid, None, "simulate")
        assert start["values"]["devices"]["sync"]["event_lines"] == {
            "TRIAL_START": "Dev1/port0/line3"
        }
        assert start["events_source"] == {
            **start["events_source"],
            "kind": "last-session",
            "run": run["id"],
        }

    def test_with_nothing_to_carry_no_line_is_assigned(self, workspace):
        set_up(workspace)
        pid = workspace.projects[0]["id"]
        (Path(workspace.projects[0]["path"]) / "configs/rig-lab.yaml").unlink()
        start = workspace.session_rig(pid, None, "simulate")
        assert start["events_source"]["kind"] == "none"
        assert "no event goes out on a line" in start["events_source"]["label"]


class TestPages:
    def test_templates_and_the_suggestion_before_set_up(self, workspace):
        pid = workspace.projects[0]["id"]
        finish(
            workspace,
            workspace.start(Launch(project=pid, mode="simulate", rig="configs/rig-sim.yaml")),
        )
        status = workspace.machine_status()
        assert status["configured"] is False
        ids = {t["id"] for t in status["templates"]}
        assert f"{pid}:configs/rig-lab.yaml" in ids and "alhazen/lab" in ids
        lab = next(t for t in status["templates"] if t["id"] == f"{pid}:configs/rig-lab.yaml")
        assert lab["events"] == ["BOGUS", "FIX_ON"] and lab["summary"].startswith("1920 × 1080")
        assert status["suggested"] == f"{pid}:configs/rig-sim.yaml"

    def test_the_data_page_lists_the_machines_data_root(self, workspace):
        set_up(workspace)
        described = workspace.describe(workspace.projects[0]["id"])
        assert described["machine_rig"]["name"] == "booth"
        existing, missing, _problems = data_roots(described)
        real = next(r for r in existing + missing if r.kind == "real")
        assert "this machine (booth)" in real.rigs


class TestCommandLine:
    def test_rig_init_and_show(self, tmp_path, capsys):
        state = tmp_path / "state"
        assert (
            main(["rig", "init", "--state-dir", str(state), "--name", "m1", "--from", str(RIG)])
            == 0
        )
        assert "this machine's rig is m1" in capsys.readouterr().out
        assert main(["rig", "show", "--state-dir", str(state)]) == 0
        assert "distance_cm: 60.0" in capsys.readouterr().out
        assert (
            main(["rig", "init", "--state-dir", str(state), "--name", "m1", "--from", str(RIG)])
            == 1
        )

    def test_rig_origin_file_is_read_as_one_json_object(self, tmp_path):
        path = tmp_path / "o.json"
        path.write_text('{"kind": "machine"}')
        assert _read_rig_origin(str(path)) == {"kind": "machine"}
        path.write_text("[1]")
        with pytest.raises(ValueError, match="JSON object"):
            _read_rig_origin(str(path))
        assert _read_rig_origin(None) is None


class TestASimulatedSession:
    """A real session, start to finish: a scaffolded experiment registered with
    its real interpreter probe, this machine's rig set up from its lab rig,
    and a headless simulate launch with one setting changed for the session."""

    def test_session_json_records_where_the_rig_came_from(self, tmp_path):
        assert main(["new", "demo_exp", "--into", str(tmp_path)]) == 0
        root = tmp_path / "demo_exp"
        space = Workspace(tmp_path / "state")
        try:
            project = space.add(str(root), sys.executable)
            assert "rig-origin" in project["capabilities"]
            space.setup_machine(
                {"name": "lab", "template": f"{project['id']}:configs/rig-lab.yaml"}
            )
            start = space.session_rig(project["id"], None, "simulate")
            assert "FIX_ON" in start["events"]
            values = start["values"]
            values["monitor"]["distance_cm"] = 61.5
            run = space.start(
                Launch(
                    project=project["id"],
                    mode="simulate",
                    headless=True,
                    seed=1,
                    rig_values=values,
                )
            )
            space.worker.join(timeout=180)
            run = space.detail(run["id"])
            assert run["status"] == "completed", run["log"][-2000:]
        finally:
            space.close()
        cards = list((root / "data-rehearsal").rglob("session.json"))
        assert len(cards) == 1
        card = json.loads(cards[0].read_text())
        origin = card["rig"]["origin"]
        assert origin["kind"] == "machine" and origin["machine"] == "lab"
        assert origin["changes"] == [
            {"path": "monitor.distance_cm", "machine": 57.0, "session": 61.5}
        ]
        assert origin["workspace_run"] == run["id"]
        assert "distance_cm: 61.5" in (cards[0].parent / "rig-source.yaml").read_text()
