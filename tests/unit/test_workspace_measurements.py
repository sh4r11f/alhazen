"""The workspace's side of Measure rig's checklist: what the probe records,
what a launch may ask for, the command it sends and the queue it shows.

The page only offers checkboxes; the server is what refuses a selection the
child would refuse — an empty one, an unknown key, a dependant without its
prerequisite, a subject measurement with no subject — before a run record
exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.unit.test_workspace import REAL_PROBE, request_for, workspace  # noqa: F401

from alhazen.cli import workspace as workspace_module
from alhazen.cli.workspace import (
    _measurement_offer,
    check_measurements,
    measurement_status,
)

CATALOG = [
    {
        "key": "monitor.refresh",
        "group": "Monitor",
        "title": "Refresh rate & frame timing",
        "order": 10,
        "requires": [],
        "subject": "none",
    },
    {
        "key": "input.keys",
        "group": "Keyboard & mouse",
        "title": "Key timing & response time",
        "order": 40,
        "requires": [],
        "subject": "optional",
    },
    {
        "key": "tracker.calibration",
        "group": "Eye tracker",
        "title": "Calibration",
        "order": 80,
        "requires": [],
        "subject": "required",
    },
    {
        "key": "tracker.accuracy",
        "group": "Eye tracker",
        "title": "Accuracy, precision & gain",
        "order": 85,
        "requires": ["tracker.calibration"],
        "subject": "required",
    },
]


@pytest.fixture
def offered(workspace, monkeypatch):  # noqa: F811
    project = workspace.project(workspace.projects[0]["id"])
    monkeypatch.setitem(project, "measurements", [dict(entry) for entry in CATALOG])
    return project


class TestTheSelectionIsCheckedOnTheServer:
    def test_measure_rig_with_a_catalog_needs_at_least_one(self, offered):
        with pytest.raises(ValueError, match="at least one measurement"):
            check_measurements(offered, "measure", [])
        with pytest.raises(ValueError, match="at least one measurement"):
            check_measurements(offered, "measure", None)

    def test_unknown_repeated_and_orphaned_keys_are_refused(self, offered):
        with pytest.raises(ValueError, match="not a measurement"):
            check_measurements(offered, "measure", ["monitor.refreshh"])
        with pytest.raises(ValueError, match="twice"):
            check_measurements(offered, "measure", ["monitor.refresh", "monitor.refresh"])
        with pytest.raises(ValueError, match="needs Calibration"):
            check_measurements(offered, "measure", ["tracker.accuracy"])
        check_measurements(offered, "measure", ["tracker.accuracy", "tracker.calibration"])

    def test_other_modes_take_none(self, offered):
        with pytest.raises(ValueError, match="Only Measure rig"):
            check_measurements(offered, "simulate", ["monitor.refresh"])
        check_measurements(offered, "simulate", None)

    def test_an_older_alhazen_keeps_the_fixed_list(self, workspace):  # noqa: F811
        project = workspace.project(workspace.projects[0]["id"])
        assert project.get("measurements") is None
        check_measurements(project, "measure", None)
        with pytest.raises(ValueError, match="fixed list"):
            check_measurements(project, "measure", ["monitor.refresh"])


class TestTheCommand:
    def test_it_sends_the_keys_and_where_to_keep_the_queue(self, workspace, offered):  # noqa: F811
        request = request_for(
            workspace, mode="measure", measurements=["input.keys", "monitor.refresh"]
        )
        run_dir = workspace.directory / "job"
        command = workspace._command(request, run_dir)
        assert command.count("--measure") == 2
        assert command[command.index("--measure-status") + 1] == str(
            run_dir / "measure-status.json"
        )
        # Nothing of a subject for measurements of the machine, even when the
        # form still holds one.
        assert "--sub" not in command

    def test_a_subject_measurement_needs_and_passes_a_subject(self, workspace, offered):  # noqa: F811
        request = request_for(workspace, mode="measure", measurements=["tracker.calibration"])
        with pytest.raises(ValueError, match="give a subject ID"):
            workspace._command(request, workspace.directory / "job")
        request = request_for(
            workspace, mode="measure", subject="s07", measurements=["tracker.calibration"]
        )
        command = workspace._command(request, workspace.directory / "job")
        assert command[command.index("--sub") + 1] == "s07"
        assert "--initials" not in command

    def test_the_flags_cannot_be_typed_in_the_extra_arguments(self, workspace, offered):  # noqa: F811
        request = request_for(
            workspace,
            mode="measure",
            measurements=["monitor.refresh"],
            extra_args="--measure tracker.calibration",
        )
        with pytest.raises(ValueError, match="--measure is set from the dashboard"):
            workspace._command(request, workspace.directory / "job")

    def test_a_launch_records_its_selection_and_shows_its_queue(self, workspace, offered):  # noqa: F811
        run = workspace.start(
            request_for(workspace, mode="measure", measurements=["monitor.refresh"])
        )
        assert run["measurements"] == ["monitor.refresh"]
        workspace.worker.join(timeout=20)
        directory = Path(run["directory"])
        assert workspace.detail(run["id"])["measurement"] is None
        (directory / "measure-status.json").write_text(
            json.dumps(
                {
                    "current": "monitor.refresh",
                    "jobs": [{"key": "monitor.refresh", "state": "running"}],
                }
            )
        )
        assert workspace.detail(run["id"])["measurement"]["current"] == "monitor.refresh"
        (directory / "measure-status.json").write_text("{not json")
        assert measurement_status(directory) is None

    def test_a_second_launch_while_one_runs_is_refused(self, workspace, offered, monkeypatch):  # noqa: F811
        workspace.active = "someone-else"
        try:
            with pytest.raises(ValueError, match="Another run is active"):
                workspace.start(
                    request_for(workspace, mode="measure", measurements=["monitor.refresh"])
                )
        finally:
            workspace.active = None


class TestTheProbe:
    def test_the_offer_is_checked(self):
        assert _measurement_offer(None) == {"measurements": None, "measurements_error": None}
        broken = _measurement_offer({"error": "ImportError: kde"})
        assert broken == {"measurements": [], "measurements_error": "ImportError: kde"}
        with pytest.raises(ValueError, match="unexpected measurement key"):
            _measurement_offer({"jobs": [dict(CATALOG[0], key="../../x")]})
        with pytest.raises(ValueError, match="requires one that is not listed"):
            _measurement_offer({"jobs": [CATALOG[3]]})

    def test_the_real_probe_lists_this_alhazens_measurements(self, tmp_path):
        root = tmp_path / "experiment"
        root.mkdir()
        report = REAL_PROBE(sys.executable, str(root))
        keys = [entry["key"] for entry in report["measurements"]]
        assert keys[0] == "monitor.refresh" and "tracker.accuracy" in keys
        accuracy = next(e for e in report["measurements"] if e["key"] == "tracker.accuracy")
        assert accuracy["subject"] == "required"
        assert report["measurements_error"] is None


def test_the_module_is_wired_into_the_probe():
    assert "measurements" in workspace_module.INTERPRETER_PROBE
