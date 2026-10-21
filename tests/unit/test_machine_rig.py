"""This machine's rig: one flat rig per dashboard workspace (cli/machine_rig.py),
a session's own copy of it, and the event lines carried between sessions."""

from __future__ import annotations

import json
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
import yaml

from alhazen.cli import machine_rig as mr
from alhazen.cli import workspace as workspace_module
from alhazen.cli.dashboard import DashboardServer, with_rig_config
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
    """Add a rig config from one of the experiment's rigs; the config as the page sees it."""
    pid = space.projects[0]["id"]
    answer = space.setup_machine({"name": name, "template": f"{pid}:{rig}"})
    return next(c for c in answer["configs"] if c["id"] == answer["created"])


def finish(space, run):
    space.worker.join(timeout=20)
    assert not space.worker.is_alive()
    return space.detail(run["id"])


def legacy_state(tmp_path: Path, name: str = "booth") -> Path:
    """A state folder as alhazen 3.5.0 left it: one machine rig at the top of machine-rig/,
    its gamma beside it, its saves and one earlier version."""
    folder = tmp_path / "state" / "machine-rig"
    (folder / "history").mkdir(parents=True)
    (folder / f"rig-{name}.yaml").write_text(RIG.read_text())
    (folder / f"rig-{name}_gamma.yaml").write_text("gamma: 2.2\n")
    (folder / "history" / f"rig-{name}.20261019T000000Z.yaml").write_text("old\n")
    (folder / "machine.json").write_text(
        json.dumps(
            {
                "name": name,
                "created": "2026-10-19T00:00:00+00:00",
                "saves": [{"by": "setup", "at": "2026-10-19T00:00:00+00:00"}],
            }
        )
    )
    return tmp_path / "state"


class TestTheConfigFiles:
    def test_a_shared_rig_is_copied_byte_for_byte_as_the_first_config(self, tmp_path):
        rigs = mr.MachineRigs(tmp_path)
        status = rigs.setup("lab", RIG, shared=None, template={"kind": "shared", "label": "x"})
        assert status["name"] == "lab" and status["default"] is True
        assert Path(status["file"]) == tmp_path / "machine-rig/configs/lab/rig-lab.yaml"
        assert Path(status["file"]).read_text() == RIG.read_text()
        assert status["origin"]["template"]["sha256"]
        with pytest.raises(mr.MachineRigError, match="already has a rig config named 'lab'"):
            rigs.setup("lab", RIG, shared=None, template={})

    def test_event_names_are_left_out_and_returned(self, tmp_path):
        source = tmp_path / "rig-lab.yaml"
        source.write_text(lab_text(events=True))
        status = mr.MachineRigs(tmp_path / "state").setup(
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
        status = mr.MachineRigs(tmp_path / "state").setup(
            "lab", source, shared={"base": shared / "rig-base.yaml"}, template={"label": "e/lab"}
        )
        assert "extends" not in status["values"]
        assert status["values"]["monitor"]["distance_cm"] == 70.0
        assert status["values"]["monitor"]["width_px"] == 1920
        assert status["origin"]["template"]["extends"] == "base"

    @pytest.mark.parametrize(
        ("text", "match"),
        [
            ("extends: lab\n", "extends nothing"),
            (lab_text(events=True), "names events"),
            ("monitor: 3\n", "invalid config"),
            ("- a\n", "YAML mapping"),
        ],
    )
    def test_the_editor_refuses_what_a_rig_config_cannot_be(self, tmp_path, text, match):
        rigs = mr.MachineRigs(tmp_path)
        rigs.setup("lab", RIG, shared=None, template={})
        with pytest.raises(mr.MachineRigError, match=match):
            rigs.save_text("lab", text, None, by="test")

    def test_a_save_keeps_the_previous_file_and_checks_it_was_not_changed(self, tmp_path):
        rigs = mr.MachineRigs(tmp_path)
        status = rigs.setup("lab", RIG, shared=None, template={})
        edited = status["text"].replace("distance_cm: 60.0", "distance_cm: 61.0")
        with pytest.raises(mr.MachineRigError, match="changed since"):
            rigs.save_text(status["id"], edited, "0" * 64, by="test")
        saved = rigs.save_text(status["id"], edited, status["sha256"], by="test")
        assert saved["saved"] == ["monitor.distance_cm"]
        assert rigs.require().values["monitor"]["distance_cm"] == 61.0
        history = list((tmp_path / "machine-rig/configs/lab/history").iterdir())
        assert [p.read_text() for p in history] == [RIG.read_text()]
        assert rigs.history("lab")["saves"][0]["changes"] == ["monitor.distance_cm"]
        assert rigs.version("lab", history[0].name) == RIG.read_text()
        with pytest.raises(mr.MachineRigError, match="history"):
            rigs.version("lab", "../configs.json")

    def test_saving_settings_drops_event_names(self, tmp_path):
        rigs = mr.MachineRigs(tmp_path)
        source = tmp_path / "rig-x.yaml"
        source.write_text(lab_text())
        status = rigs.setup("lab", source, shared=None, template={})
        values = yaml.safe_load(lab_text(events=True))
        values["monitor"]["distance_cm"] = 65.0
        saved = rigs.save_values(None, values, status["sha256"], by="run page")
        assert saved["saved"] == ["monitor.distance_cm"]
        assert "event_lines" not in rigs.require().values["devices"]["sync"]

    def test_several_configs_one_default_rename_and_delete(self, tmp_path):
        rigs = mr.MachineRigs(tmp_path)
        lab = rigs.setup("lab", RIG, shared=None, template={})
        source = tmp_path / "rig-x.yaml"
        source.write_text(lab_text())
        eye = rigs.setup("eyelink", source, shared=None, template={})
        assert rigs.default_id() == lab["id"] and eye["default"] is False
        assert [c.name for c in rigs.configs()] == ["lab", "eyelink"]
        gamma = tmp_path / "machine-rig/configs/eyelink/rig-eyelink_gamma.yaml"
        gamma.write_text("gamma: 2.0\n")
        rigs.rename(eye["id"], "tracker", eye["sha256"], by="test")
        moved = tmp_path / "machine-rig/configs/tracker"
        assert (moved / "rig-tracker.yaml").is_file() and (
            moved / "rig-tracker_gamma.yaml"
        ).is_file()
        assert rigs.require("tracker").id == eye["id"]
        with pytest.raises(mr.MachineRigError, match="already has a rig config named"):
            rigs.rename(eye["id"], "lab", None, by="test")
        with pytest.raises(mr.MachineRigError, match="default"):
            rigs.delete(lab["id"], None, by="test")
        rigs.set_default(eye["id"])
        removed = rigs.delete(lab["id"], lab["sha256"], by="test")
        assert Path(removed["trash"], "configs", "rig-lab.yaml").is_file()
        assert [c.name for c in rigs.configs()] == ["tracker"]
        with pytest.raises(mr.MachineRigError, match="no rig config 'lab'"):
            rigs.require("lab")


class TestMigrationFrom350:
    def test_the_350_rig_becomes_the_first_config_where_it_is(self, tmp_path):
        state = legacy_state(tmp_path)
        before = {p: p.read_bytes() for p in (state / "machine-rig").rglob("*") if p.is_file()}
        rigs = mr.MachineRigs(state)
        status = rigs.status()
        (config,) = status["configs"]
        assert config["name"] == "booth" and config["legacy"] and config["default"]
        assert status["migrated"]["config"] == config["id"]
        assert config["file"] == str(state / "machine-rig/rig-booth.yaml")
        assert config["gamma"]["file"].endswith("rig-booth_gamma.yaml")
        # Nothing 3.5.0 wrote was moved or rewritten.
        after = {p: p.read_bytes() for p in (state / "machine-rig").rglob("*") if p.is_file()}
        assert {p: b for p, b in after.items() if p.name != "configs.json"} == before
        # The id is the machine's from now on.
        assert mr.MachineRigs(state).machine_id() == rigs.machine_id()

    def test_a_350_dashboard_still_sees_its_rig_and_its_edits_are_picked_up(self, tmp_path):
        state = legacy_state(tmp_path)
        rigs = mr.MachineRigs(state)
        source = tmp_path / "rig-x.yaml"
        source.write_text(lab_text())
        rigs.setup("eyelink", source, shared=None, template={})
        # 3.5.0 lists rig files at the top of the folder only.
        assert [p.name for p in mr._rig_files(state / "machine-rig")] == ["rig-booth.yaml"]
        top = state / "machine-rig/rig-booth.yaml"
        top.write_text(top.read_text().replace("distance_cm: 60.0", "distance_cm: 59.0"))
        assert rigs.require("booth").values["monitor"]["distance_cm"] == 59.0

    def test_a_rig_a_350_dashboard_sets_up_after_the_first_was_deleted_is_adopted(self, tmp_path):
        state = legacy_state(tmp_path)
        rigs = mr.MachineRigs(state)
        source = tmp_path / "rig-x.yaml"
        source.write_text(lab_text())
        other = rigs.setup("eyelink", source, shared=None, template={})
        rigs.set_default(other["id"])
        rigs.delete("booth", None, by="test")
        assert not (state / "machine-rig/rig-booth.yaml").exists()
        (state / "machine-rig/rig-desk.yaml").write_text(RIG.read_text())
        names = {c["name"]: c for c in rigs.status()["configs"]}
        assert names["desk"]["legacy"] and names["desk"]["origin"]["kind"] == "older-dashboard"

    def test_two_rig_files_at_the_top_are_refused_rather_than_one_picked(self, tmp_path):
        state = legacy_state(tmp_path)
        (state / "machine-rig/rig-other.yaml").write_text(RIG.read_text())
        status = mr.MachineRigs(state).status()
        assert status["configs"] == [] and "holds 2 rig files" in status["error"]

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
        with pytest.raises(ValueError, match="no rig config yet"):
            workspace.start(Launch(project=pid, mode="simulate"))

    def test_a_session_runs_its_own_copy_and_records_what_changed(self, workspace):
        set_up(workspace, "configs/rig-lab.yaml", "lab")
        machine_file = workspace.machine.require().path
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
        assert run["machine_rig"]["file"] == str(workspace.machine.require().path)

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
        assert argv[argv.index("--rig") + 1] == str(workspace.machine.require().path)
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


class TestChoosingAConfig:
    def test_a_session_runs_on_the_chosen_config_and_carries_its_own_event_lines(self, workspace):
        booth = set_up(workspace)
        lab = set_up(workspace, "configs/rig-lab.yaml", "lab")
        pid = workspace.projects[0]["id"]
        assert workspace.describe(pid)["machine_rigs"]["default"] == booth["id"]
        start = workspace.session_rig(pid, None, "simulate", lab["id"])
        assert start["config"] == {"id": lab["id"], "name": "lab"}
        assert [c["name"] for c in start["configs"]] == ["booth", "lab"]
        values = start["values"]
        values["devices"]["sync"]["event_lines"] = {"TRIAL_START": "Dev1/port0/line5"}
        run = finish(
            workspace,
            workspace.start(
                Launch(
                    project=pid,
                    mode="test",
                    subject="s1",
                    initials="AB",
                    rig_config="lab",
                    rig_values=values,
                )
            ),
        )
        assert run["rig_name"] == "lab" and run["rig_config"] == lab["id"]
        assert Path(run["directory"], "rig-lab.yaml").is_file()
        # The lines go with the config they were chosen on.
        again = workspace.session_rig(pid, None, "simulate", "lab")
        assert again["events_source"]["kind"] == "last-session"
        assert workspace.session_rig(pid, None, "simulate")["config"]["name"] == "booth"
        with pytest.raises(ValueError, match="no rig config 'nope'"):
            workspace.start(Launch(project=pid, mode="simulate", rig_config="nope"))
        with pytest.raises(ValueError, match="either"):
            workspace.start(
                Launch(project=pid, mode="simulate", rig_config="lab", rig="configs/rig-sim.yaml")
            )


class TestPages:
    def test_templates_and_the_suggestion_before_set_up(self, workspace):
        pid = workspace.projects[0]["id"]
        finish(
            workspace,
            workspace.start(Launch(project=pid, mode="simulate", rig="configs/rig-sim.yaml")),
        )
        assert workspace.machine_status()["configs"] == []
        status = workspace.machine_template_list()
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
        assert "added this machine's rig config m1" in capsys.readouterr().out
        assert main(["rig", "show", "--state-dir", str(state)]) == 0
        assert "distance_cm: 60.0" in capsys.readouterr().out
        assert (
            main(["rig", "init", "--state-dir", str(state), "--name", "m1", "--from", str(RIG)])
            == 1
        )
        assert (
            main(
                [
                    "rig",
                    "init",
                    "--state-dir",
                    str(state),
                    "--name",
                    "m2",
                    "--from",
                    str(RIG),
                    "--default",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert main(["rig", "list", "--state-dir", str(state)]) == 0
        listed = capsys.readouterr().out.splitlines()
        assert listed[0].startswith("* m2") and listed[1].startswith("  m1")
        assert main(["rig", "show", "--state-dir", str(state), "--config", "m1"]) == 0

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


class TestTheDashboardRoutes:
    def test_the_pages_api_adds_edits_renames_defaults_and_deletes(self, workspace):
        server = DashboardServer(workspace)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def call(path, body=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            headers = {"X-Alhazen-Token": server.token}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(
                "POST" if body is not None else "GET",
                path,
                json.dumps(body) if body is not None else None,
                headers,
            )
            response = conn.getresponse()
            data = json.loads(response.read() or b"null")
            conn.close()
            return response.status, data

        try:
            pid = workspace.projects[0]["id"]
            status, rigs = call("/api/machine-rigs")
            assert (
                status == 200 and rigs["configs"] == [] and rigs["machine"]["id"].startswith("mc_")
            )
            assert rigs["sync"]["state"] == "not-connected"
            status, templates = call("/api/machine-rigs/templates")
            assert f"{pid}:configs/rig-lab.yaml" in {t["id"] for t in templates["templates"]}
            status, made = call(
                "/api/machine-rigs/create",
                {"name": "booth", "template": f"{pid}:configs/rig-sim.yaml"},
            )
            assert status == 201 and made["default"] == made["created"]
            status, made = call("/api/machine-rigs/create", {"name": "spare", "copy_of": "booth"})
            spare = next(c for c in made["configs"] if c["name"] == "spare")
            assert spare["origin"]["kind"] == "copy" and spare["sync"]["state"] == "not-synced"
            status, checked = call("/api/machine-rigs/check", {"id": spare["id"], "text": "- a\n"})
            assert status == 400
            status, saved = call(
                "/api/machine-rigs/save",
                {
                    "id": spare["id"],
                    "sha256": spare["sha256"],
                    "text": spare["text"].replace("distance_cm: 60.0", "distance_cm: 64.0"),
                },
            )
            assert status == 200 and saved["saved"] == ["monitor.distance_cm"]
            status, stale = call(
                "/api/machine-rigs/save",
                {"id": spare["id"], "sha256": spare["sha256"], "text": spare["text"]},
            )
            assert status == 400 and "changed since" in stale["error"]
            status, renamed = call("/api/machine-rigs/rename", {"id": spare["id"], "name": "desk"})
            assert "desk" in [c["name"] for c in renamed["configs"]]
            status, defaulted = call("/api/machine-rigs/default", {"id": spare["id"]})
            assert defaulted["default"] == spare["id"]
            status, chosen = call(f"/api/session-rig?project={pid}&mode=simulate&config=booth")
            assert chosen["config"]["name"] == "booth"
            booth = next(c for c in defaulted["configs"] if c["name"] == "booth")
            status, history = call(f"/api/machine-rigs/history?id={spare['id']}")
            assert [s.get("by") for s in history["saves"]][:2] == ["general page", "general page"]
            status, deleted = call("/api/machine-rigs/delete", {"id": booth["id"]})
            assert [c["name"] for c in deleted["configs"]] == ["desk"]
            status, synced = call("/api/machine-rigs/sync", {})
            assert status == 200 and synced["sync"]["state"] == "not-connected"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_the_page_can_be_opened_with_a_config_chosen(self):
        url = "http://127.0.0.1:5/#token=abc"
        assert with_rig_config(url, "eye link") == "http://127.0.0.1:5/?rig=eye%20link#token=abc"
        assert with_rig_config(url, None) == url
