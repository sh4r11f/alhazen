"""Paths, recorder tables, manifest, participants registry."""

from __future__ import annotations

import csv
import json

import pytest
import yaml

from alhazen.core.events import Event
from alhazen.data.manifest import verify_manifest, write_manifest
from alhazen.data.participants import ensure_participant, participants_path
from alhazen.data.paths import SessionPaths
from alhazen.errors import DataError
from alhazen.session.recorder import DataRecorder, ordered_trial_columns


class TestSessionPaths:
    def test_layout_and_padding(self, tmp_path):
        paths = SessionPaths.create(tmp_path, "M1", 3, 2, "mib-quest", "20260826")
        assert paths.run_dir == tmp_path / "sub-M1" / "ses-003" / "run-02_task-mib-quest"
        assert paths.trials_path.name == "sub-M1_ses-003_run-02_task-mib-quest_20260826_trials.csv"
        assert paths.figures_dir.is_dir()
        assert paths.snapshot_path.parent == paths.run_dir

    def test_refuses_overwriting_recorded_run(self, tmp_path):
        paths = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        paths.trials_path.write_text("trial_index\n1\n")
        with pytest.raises(DataError, match="refusing to overwrite"):
            SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        # The next run number is fine.
        SessionPaths.create(tmp_path, "M1", 1, 2, "task", "20260826")


class TestRecorder:
    def test_column_ordering(self):
        rows = [
            {
                "trial_index": 1,
                "attempt": 1,
                "outcome": "CORRECT",
                "coherence": 0.5,
                "direction": 90,
                "t_trial_start": 0.0,
                "t_stim_on": 0.5,
            }
        ]
        assert ordered_trial_columns(rows) == [
            "trial_index",
            "attempt",
            "outcome",
            "coherence",
            "direction",
            "t_stim_on",
            "t_trial_start",
        ]

    def test_column_present_only_if_populated(self):
        rows = [
            {"trial_index": 1, "outcome": "CORRECT", "abort_reason": None},
            {"trial_index": 2, "outcome": "ABORTED", "abort_reason": "skipped_by_user"},
        ]
        assert "abort_reason" in ordered_trial_columns(rows)
        assert "abort_reason" not in ordered_trial_columns(rows[:1])

    def test_write_tables(self, tmp_path):
        recorder = DataRecorder(tmp_path / "trials.csv", tmp_path / "events.csv")
        recorder.on_event(Event(name="TRIAL_START", t=0.1, trial_index=1, payload={"a": 1}))
        recorder.add_trial({"trial_index": 1, "outcome": "COMPLETED", "t_trial_start": 0.1})
        recorder.write()

        with (tmp_path / "trials.csv").open() as f:
            trials = list(csv.DictReader(f))
        assert trials[0]["outcome"] == "COMPLETED"

        with (tmp_path / "events.csv").open() as f:
            events = list(csv.DictReader(f))
        assert events[0]["event"] == "TRIAL_START"
        assert json.loads(events[0]["payload_json"]) == {"a": 1}


class TestManifest:
    def test_write_and_verify(self, tmp_path):
        (tmp_path / "trials.csv").write_text("a,b\n1,2\n")
        (tmp_path / "figures").mkdir()
        (tmp_path / "figures" / "fig.txt").write_text("fig")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path)

        manifest = yaml.safe_load(manifest_path.read_text())
        assert {a["path"] for a in manifest["artifacts"]} == {"trials.csv", "figures/fig.txt"}
        assert verify_manifest(tmp_path, manifest_path) == []

    def test_verify_reports_tamper_missing_and_unlisted(self, tmp_path):
        (tmp_path / "trials.csv").write_text("a\n1\n")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path)

        (tmp_path / "trials.csv").write_text("a\n2\n")
        (tmp_path / "extra.csv").write_text("x\n")
        problems = verify_manifest(tmp_path, manifest_path)
        assert "hash mismatch: trials.csv" in problems
        assert "unlisted file: extra.csv" in problems

        (tmp_path / "trials.csv").unlink()
        assert "missing: trials.csv" in verify_manifest(tmp_path, manifest_path)


class TestParticipants:
    def test_create_and_idempotent(self, tmp_path):
        ensure_participant(tmp_path, "s01")
        ensure_participant(tmp_path, "s01")
        content = participants_path(tmp_path).read_text().strip().splitlines()
        assert content == ["participant_id", "sub-s01"]

    def test_new_metadata_widens_columns(self, tmp_path):
        ensure_participant(tmp_path, "s01")
        ensure_participant(tmp_path, "s02", {"species": "macaque"})
        rows = participants_path(tmp_path).read_text().strip().splitlines()
        assert rows[0] == "participant_id\tspecies"
        assert rows[1].startswith("sub-s01")
        assert rows[2] == "sub-s02\tmacaque"
