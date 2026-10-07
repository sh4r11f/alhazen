"""The Run page's subject and experimenter: people-registry records resolved
by the server, sent to run.py, and kept in an immutable launch snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.unit import test_workspace as base

from alhazen.cli import workspace as workspace_module
from alhazen.cli.workspace import Workspace

# The launcher fixtures, by module so pytest does not collect its classes here.
request_for = base.request_for
finish = base.finish


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    yield from base.workspace.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def people(workspace):
    """A subject and an assigned experimenter of the fixture's experiment, on
    an alhazen that records the experimenter."""
    project = workspace.projects[0]
    project["capabilities"] = ["experimenter"]
    registry = workspace.people
    subject = registry.add_subject(project["id"], {"code": "007", "initials": "HD"})
    who = registry.add_experimenter({"name": "Zoë Lee", "initials": "ZL"})
    registry.assign(project["id"], who["id"])
    return {"project": project, "subject": subject, "experimenter": who}


def argv_of(run: dict) -> list[str]:
    return json.loads(run["log"].splitlines()[0])


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


class TestRegistryLaunch:
    def test_the_records_reach_the_session_and_the_snapshot(self, workspace, people):
        run = workspace.start(
            request_for(
                workspace,
                mode="test",
                subject_record=people["subject"]["id"],
                experimenter=people["experimenter"]["id"],
            )
        )
        detail = finish(workspace, run)
        argv = argv_of(detail)
        assert flag(argv, "--sub") == "007" and flag(argv, "--initials") == "HD"
        assert flag(argv, "--experimenter") == "Zoë Lee"
        assert flag(argv, "--experimenter-id") == people["experimenter"]["id"]
        assert detail["subject"] == "007" and detail["initials"] == "HD"
        assert detail["experimenter_recorded_in"] == "session.json"
        identity = detail["identity"]
        assert identity["source"] == "registry"
        assert identity["subject"]["record_id"] == people["subject"]["id"]
        assert identity["experimenter"]["name"] == "Zoë Lee"
        launch = json.loads((Path(run["directory"]) / "launch.json").read_text(encoding="utf-8"))
        assert launch["identity"] == identity and launch["command"] == run["command"]

    def test_later_edits_never_rewrite_a_past_launch(self, workspace, people, tmp_path):
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace,
                    mode="test",
                    subject_record=people["subject"]["id"],
                    experimenter=people["experimenter"]["id"],
                )
            ),
        )
        launch_file = Path(run["directory"]) / "launch.json"
        before = launch_file.read_bytes()
        workspace.people.update_experimenter(people["experimenter"]["id"], 1, {"name": "Z. Lee"})
        workspace.people.update_subject(
            people["project"]["id"], people["subject"]["id"], 1, {"notes": "edited"}, used=True
        )
        workspace.close()
        reopened = Workspace(workspace.directory)
        try:
            again = reopened.detail(run["id"])
            assert again["identity"]["experimenter"]["name"] == "Zoë Lee"
            assert launch_file.read_bytes() == before
            assert reopened.used_subjects(people["project"]["id"]) == {people["subject"]["id"]}
        finally:
            reopened.close()

    def test_an_older_alhazen_is_not_sent_the_flag_and_the_run_says_so(self, workspace, people):
        people["project"]["capabilities"] = []
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace,
                    mode="test",
                    subject_record=people["subject"]["id"],
                    experimenter=people["experimenter"]["id"],
                )
            ),
        )
        assert "--experimenter" not in argv_of(run)
        assert run["experimenter_recorded_in"] == "workspace"
        assert run["identity"]["experimenter"]["record_id"] == people["experimenter"]["id"]
        people["project"].pop("capabilities")  # registered before the probe asked
        run = finish(
            workspace,
            workspace.start(
                request_for(
                    workspace,
                    mode="test",
                    subject_record=people["subject"]["id"],
                    experimenter=people["experimenter"]["id"],
                )
            ),
        )
        assert "--experimenter" not in argv_of(run)
        assert run["experimenter_recorded_in"] == "workspace"

    def test_a_typed_launch_still_works_and_records_no_experimenter(self, workspace):
        run = finish(
            workspace,
            workspace.start(request_for(workspace, mode="test", subject="s01", initials="hd")),
        )
        assert run["identity"] == {
            "subject": {"record_id": None, "id": "s01", "initials": "HD"},
            "experimenter": None,
            "source": "typed",
        }
        assert run["experimenter_recorded_in"] is None
        assert (Path(run["directory"]) / "launch.json").is_file()

    def test_simulate_needs_no_subject_record(self, workspace, people):
        run = finish(
            workspace,
            workspace.start(
                request_for(workspace, mode="simulate", experimenter=people["experimenter"]["id"])
            ),
        )
        assert run["identity"]["subject"] is None
        assert flag(argv_of(run), "--experimenter") == "Zoë Lee"


class TestMeasureRig:
    CATALOG = [
        {
            "key": "tracker.calibration",
            "group": "Eye tracker",
            "title": "Calibration",
            "order": 1,
            "requires": [],
            "subject": "required",
        },
        {
            "key": "monitor.refresh",
            "group": "Monitor",
            "title": "Refresh",
            "order": 2,
            "requires": [],
            "subject": "none",
        },
    ]

    def test_a_registered_subject_reaches_a_measurement_of_the_subject(self, workspace, people):
        people["project"]["measurements"] = self.CATALOG
        request = request_for(
            workspace,
            mode="measure",
            measurements=["tracker.calibration"],
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )
        resolved, identity = workspace._identity(people["project"], request)
        command = workspace._command(resolved, workspace.directory / "job")
        assert command[command.index("--sub") + 1] == "007"
        assert "--initials" not in command and "--experimenter" not in command
        assert identity["experimenter"]["record_id"] == people["experimenter"]["id"]

    def test_a_machine_measurement_sends_no_subject(self, workspace, people):
        people["project"]["measurements"] = self.CATALOG
        request = request_for(
            workspace,
            mode="measure",
            measurements=["monitor.refresh"],
            subject_record=people["subject"]["id"],
        )
        resolved, _ = workspace._identity(people["project"], request)
        assert "--sub" not in workspace._command(resolved, workspace.directory / "job")


class TestRefusals:
    def refused(self, workspace, words, **overrides):
        before = set(workspace.runs)
        folders = sorted((workspace.directory / "runs").iterdir())
        with pytest.raises(ValueError, match=words):
            workspace.start(request_for(workspace, **overrides))
        assert set(workspace.runs) == before  # nothing filed
        assert sorted((workspace.directory / "runs").iterdir()) == folders

    def test_a_subject_of_another_experiment(self, workspace, people):
        other = workspace.people.add_subject("f" * 16, {"code": "1", "initials": "AB"})
        self.refused(
            workspace,
            "another experiment",
            mode="test",
            subject_record=other["id"],
            experimenter=people["experimenter"]["id"],
        )

    def test_an_archived_subject_or_experimenter(self, workspace, people):
        registry = workspace.people
        pid = people["project"]["id"]
        registry.set_subject_status(pid, people["subject"]["id"], 1, "archived")
        self.refused(
            workspace,
            "archived",
            mode="test",
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )
        registry.set_subject_status(pid, people["subject"]["id"], 2, "active")
        registry.set_experimenter_status(people["experimenter"]["id"], 1, "archived")
        self.refused(
            workspace,
            "archived",
            mode="test",
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )

    def test_unknown_or_unassigned(self, workspace, people):
        self.refused(
            workspace,
            "Unknown subject",
            mode="test",
            subject_record="s_nope",
            experimenter=people["experimenter"]["id"],
        )
        stranger = workspace.people.add_experimenter({"name": "Not here"})
        self.refused(
            workspace,
            "not an experimenter of this experiment",
            mode="test",
            subject_record=people["subject"]["id"],
            experimenter=stranger["id"],
        )

    def test_run_and_test_need_the_experimenter(self, workspace, people):
        self.refused(
            workspace,
            "Choose the experimenter",
            mode="test",
            subject_record=people["subject"]["id"],
        )

    def test_typed_values_may_not_contradict_the_record(self, workspace, people):
        self.refused(
            workspace,
            "not the selected subject",
            mode="test",
            subject="008",
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )
        self.refused(
            workspace,
            "are not sub-007",
            mode="test",
            initials="XY",
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )

    def test_a_subject_with_no_initials_cannot_run(self, workspace, people):
        bare = workspace.people.add_subject(people["project"]["id"], {"code": "9"})
        self.refused(
            workspace,
            "no initials recorded",
            mode="test",
            subject_record=bare["id"],
            experimenter=people["experimenter"]["id"],
        )

    def test_modes_that_name_nobody(self, workspace, people):
        self.refused(
            workspace, "takes a subject", mode="movie", subject_record=people["subject"]["id"]
        )

    def test_the_data_folders_registry_disagrees(self, workspace, people):
        """participants.tsv records sub-007 as XY in the folder a test session
        writes to: refused before anything is filed, in the session's words."""
        root = Path(people["project"]["path"])
        rig = workspace._launch_rig(people["project"], "configs/rig-sim.yaml")[0]
        values = workspace_module.rig_mapping(rig.path).values
        data_root = Path(values["data_root"]).expanduser()
        if not data_root.is_absolute():
            data_root = root / data_root
        folder = workspace_module.rehearsal_root(data_root)
        folder.mkdir(parents=True)
        (folder / "participants.tsv").write_text("participant_id\tinitials\nsub-007\tXY\n")
        self.refused(
            workspace,
            "recorded as XY",
            mode="test",
            subject_record=people["subject"]["id"],
            experimenter=people["experimenter"]["id"],
        )

    def test_one_run_at_a_time_still(self, workspace, people, monkeypatch):
        (Path(people["project"]["path"]) / "run.py").write_text("import time\ntime.sleep(30)\n")
        run = workspace.start(request_for(workspace, mode="simulate"))
        try:
            with pytest.raises(ValueError, match="Another run is active"):
                workspace.start(request_for(workspace, mode="simulate"))
        finally:
            workspace.stop(run["id"])
            workspace.worker.join(timeout=40)


class TestRegistration:
    def test_registering_again_is_refused_by_register_and_keeps_notes(self, workspace, tmp_path):
        project = workspace.projects[0]
        workspace.update_meta(project["id"], {"description": "Saccades", "notes": "line 1\nline 2"})
        workspace.set_archived(project["id"], True)
        with pytest.raises(ValueError, match="already registered"):
            workspace.register(project["path"], sys.executable)
        again = workspace.add(project["path"], sys.executable)  # Project settings → save
        assert again["meta"] == {"description": "Saccades", "notes": "line 1\nline 2"}
        assert again["archived"] is True
        assert again["registered"]

    @pytest.mark.parametrize(
        "fields", [{"title": "x"}, {"notes": 3}, {"notes": "bad\x00"}, {"notes": "x" * 4001}]
    )
    def test_meta_is_checked(self, workspace, fields):
        with pytest.raises(ValueError):
            workspace.update_meta(workspace.projects[0]["id"], fields)

    def test_unregistering_never_deletes_files_or_records(self, workspace, people):
        project = people["project"]
        root = Path(project["path"])
        files = sorted(p for p in root.rglob("*"))
        workspace.remove(project["id"])
        assert sorted(p for p in root.rglob("*")) == files
        assert workspace.people.subjects(project["id"])  # still in the registry
