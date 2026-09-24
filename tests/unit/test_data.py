"""Paths, recorder tables, manifest, participants registry."""

from __future__ import annotations

import csv
import json

import pytest
import yaml

from alhazen.core.events import Event
from alhazen.data.manifest import add_to_manifest, verify_manifest, write_manifest
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

    def test_the_same_run_number_on_a_later_day_is_refused(self, tmp_path):
        # The bug this pins: the trials file's name carries the date and the
        # folder's does not, so tomorrow's run passed the check and wrote
        # into today's folder, over its snapshot and manifest.
        today = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        today.trials_path.write_text("trial_index\n1\n")
        today.snapshot_path.write_text("today's snapshot\n")

        with pytest.raises(DataError, match="refusing to overwrite") as refused:
            SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260827")

        # The message names what is there, and nothing was touched.
        assert "config_snapshot.yaml" in str(refused.value)
        assert today.snapshot_path.read_text() == "today's snapshot\n"

    def test_a_run_that_crashed_before_its_trials_file_is_refused_too(self, tmp_path):
        # A session killed mid-run leaves its snapshot and log, never a
        # trials file: its subject still did the work.
        paths = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        paths.snapshot_path.write_text("snapshot\n")
        paths.log_path.write_text("session start\n")
        with pytest.raises(DataError, match="config_snapshot.yaml, session.log"):
            SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")

    def test_a_file_in_a_subfolder_counts(self, tmp_path):
        paths = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        (paths.figures_dir / "dashboard.html").write_text("<html>")
        with pytest.raises(DataError, match="figures/dashboard.html"):
            SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260827")

    def test_a_long_list_is_cut_short(self, tmp_path):
        paths = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        for name in "abcde":
            (paths.run_dir / f"{name}.txt").write_text(name)
        with pytest.raises(DataError, match=r"\(a.txt, b.txt, c.txt and 2 more\)"):
            SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")

    def test_a_folder_left_empty_by_a_failed_build_can_be_used(self, tmp_path):
        # A build that failed before the session began (a tracker that would
        # not connect) leaves only the empty figures folder behind; trying
        # again with the same number is not an overwrite of anything.
        SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        again = SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826")
        assert again.figures_dir.is_dir()


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


class TestAddToManifest:
    """What a report or a saved alignment does to a finished run: record its
    own file and nothing else. It used to re-hash the whole directory, which
    recorded a file damaged since the session under its damaged hash — the
    first report said "hash mismatch", and every check after it "verified"."""

    def finished_run(self, tmp_path):
        (tmp_path / "trials.csv").write_text("a\n1\n")
        (tmp_path / "figures").mkdir()
        (tmp_path / "figures" / "fig.txt").write_text("fig")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path)
        return manifest_path

    def test_a_damaged_file_stays_detectable(self, tmp_path):
        manifest_path = self.finished_run(tmp_path)
        (tmp_path / "trials.csv").write_text("a\n2\n")  # changed after the session
        report = tmp_path / "report.yaml"
        report.write_text("ok: false\n")

        add_to_manifest(tmp_path, manifest_path, [report])

        # The damage is still reported; the new file is recorded.
        assert verify_manifest(tmp_path, manifest_path) == ["hash mismatch: trials.csv"]

    def test_every_other_entry_is_left_exactly_as_it_was(self, tmp_path):
        manifest_path = self.finished_run(tmp_path)
        before = yaml.safe_load(manifest_path.read_text())["artifacts"]
        (tmp_path / "report.yaml").write_text("ok: true\n")

        add_to_manifest(tmp_path, manifest_path, [tmp_path / "report.yaml"])

        after = yaml.safe_load(manifest_path.read_text())["artifacts"]
        assert after[: len(before)] == before
        assert [entry["path"] for entry in after[len(before) :]] == ["report.yaml"]

    def test_a_file_saved_again_replaces_its_own_entry(self, tmp_path):
        manifest_path = self.finished_run(tmp_path)
        report = tmp_path / "report.yaml"
        report.write_text("first\n")
        add_to_manifest(tmp_path, manifest_path, [report])
        report.write_text("second, longer\n")
        add_to_manifest(tmp_path, manifest_path, [report])

        paths = [entry["path"] for entry in yaml.safe_load(manifest_path.read_text())["artifacts"]]
        assert paths.count("report.yaml") == 1
        assert verify_manifest(tmp_path, manifest_path) == []

    def test_a_file_nobody_recorded_stays_unlisted(self, tmp_path):
        manifest_path = self.finished_run(tmp_path)
        (tmp_path / "copied-in-by-hand.csv").write_text("x\n")
        (tmp_path / "report.yaml").write_text("ok: false\n")

        add_to_manifest(tmp_path, manifest_path, [tmp_path / "report.yaml"])

        assert verify_manifest(tmp_path, manifest_path) == ["unlisted file: copied-in-by-hand.csv"]

    def test_a_file_in_a_subfolder_is_recorded_with_forward_slashes(self, tmp_path):
        manifest_path = self.finished_run(tmp_path)
        figure = tmp_path / "figures" / "later.png"
        figure.write_bytes(b"png")

        add_to_manifest(tmp_path, manifest_path, [figure])

        paths = [entry["path"] for entry in yaml.safe_load(manifest_path.read_text())["artifacts"]]
        assert "figures/later.png" in paths
        assert verify_manifest(tmp_path, manifest_path) == []

    def test_a_run_without_a_manifest_is_not_given_one(self, tmp_path, caplog):
        # Its session never finished teardown. A manifest made now would call
        # whatever is in the folder a complete run.
        (tmp_path / "trials.csv").write_text("a\n1\n")
        (tmp_path / "report.yaml").write_text("ok: false\n")

        with caplog.at_level("WARNING"):
            add_to_manifest(tmp_path, tmp_path / "manifest.yaml", [tmp_path / "report.yaml"])

        assert not (tmp_path / "manifest.yaml").exists()
        assert "has no manifest" in caplog.text
        assert "report.yaml" in caplog.text


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
