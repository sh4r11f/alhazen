"""What a run folder says about how it was set up (session/identity.py).

Since alhazen 2.0 a run folder holds, beside its snapshot, a compact
session.json and byte copies of the rig and params files the session started
from. These tests pin what each holds, that they are written together with
the snapshot or not at all, and that a session actually writes them and
stamps its experiment's version everywhere the data goes.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pytest
import yaml

from alhazen.config.experiment import Experiment
from alhazen.data.manifest import verify_manifest
from alhazen.data.paths import SessionPaths
from alhazen.errors import ConfigError
from alhazen.session.identity import (
    SESSION_JSON_SCHEMA_VERSION,
    RunIdentity,
    SourceFile,
    session_card,
    source_file,
    write_run_identity,
)
from support import TEST_EXPERIMENT, SessionHarness, make_session_config

EXPERIMENT = Experiment(
    name="amodal-averaging", version="0.1.0", version_source="pyproject.toml", root=None
)

# A rig file as a person writes one: comments, a non-ASCII character and
# Windows line endings, none of which a re-dump of the parsed config keeps.
RIG_BYTES = "# the lab rig — do not edit mid-season\r\ndata_root: data\r\n".encode()
PARAMS_BYTES = b"# pilot\nparadigm:\n  n_per_condition: 8\n"


def paths_for(tmp_path) -> SessionPaths:
    return SessionPaths.create(
        tmp_path, "t01", 1, 1, "test-task", "20260826", experiment_version=EXPERIMENT.version
    )


def identity_with_files(tmp_path, mode="run") -> RunIdentity:
    return RunIdentity(
        experiment=EXPERIMENT,
        mode=mode,
        rig_file=SourceFile(path=tmp_path / "configs" / "rig-lab.yaml", content=RIG_BYTES),
        params_file=SourceFile(path=tmp_path / "configs" / "task.yaml", content=PARAMS_BYTES),
    )


PROVENANCE = {
    "created": "2026-08-26T10:00:00+00:00",
    "experiment_git_sha": "v0.1.0-2-gabc1234-dirty",
    "alhazen_version": "2.0.0",
    "alhazen_git_describe": "not a source checkout",
}


class TestSourceFile:
    def test_a_file_is_read_with_its_absolute_path(self, tmp_path, monkeypatch):
        (tmp_path / "rig.yaml").write_bytes(RIG_BYTES)
        monkeypatch.chdir(tmp_path)

        read = source_file("rig.yaml", "rig")

        assert read == SourceFile(path=(tmp_path / "rig.yaml").resolve(), content=RIG_BYTES)

    @pytest.mark.parametrize("value", [None, "<inline>", "<defaults>"])
    def test_a_layer_that_came_from_no_file_has_no_copy(self, value):
        assert source_file(value, "params") is None

    def test_a_folder_is_not_a_file(self, tmp_path):
        assert source_file(tmp_path, "rig") is None

    def test_a_file_that_cannot_be_read_stops_the_session_naming_it(self, tmp_path, monkeypatch):
        (tmp_path / "rig.yaml").write_bytes(RIG_BYTES)

        def refuse(self):
            raise PermissionError("held by another program")

        monkeypatch.setattr(Path, "read_bytes", refuse)
        with pytest.raises(ConfigError, match=r"cannot read the rig file .*rig\.yaml"):
            source_file(tmp_path / "rig.yaml", "rig")


class TestSessionCard:
    def card(self, tmp_path, identity=None, sources=None):
        cfg = make_session_config(tmp_path)
        if sources is not None:
            cfg = cfg.model_copy(update={"sources": sources})
        paths = paths_for(tmp_path)
        return session_card(cfg, paths, identity or identity_with_files(tmp_path), PROVENANCE)

    def test_it_says_which_experiment_version_subject_and_run(self, tmp_path):
        card = self.card(tmp_path)

        assert card["schema_version"] == SESSION_JSON_SCHEMA_VERSION == 1
        assert card["experiment"] == {
            "name": "amodal-averaging",
            "version": "0.1.0",
            "version_source": "pyproject.toml",
            "git": "v0.1.0-2-gabc1234-dirty",
        }
        assert card["task"] == "test-task"
        assert card["mode"] == "run"
        assert card["subject"]["id"] == "t01"
        assert (card["session"], card["run"], card["seed"]) == (1, 1, 7)
        assert card["date"] == "20260826"
        assert card["created"] == PROVENANCE["created"]
        assert card["alhazen"] == {"version": "2.0.0", "git_describe": "not a source checkout"}

    def test_it_points_at_the_runs_files_relative_to_the_folder(self, tmp_path):
        files = self.card(tmp_path)["files"]

        assert files == {
            "snapshot": "config_snapshot.yaml",
            "manifest": "manifest.yaml",
            "rig": "rig.yaml",
            "params": "params.yaml",
            "trials": "sub-t01_ses-001_run-01_task-test-task_20260826_trials.csv",
            "events": "sub-t01_ses-001_run-01_task-test-task_20260826_events.csv",
            "frames": "sub-t01_ses-001_run-01_task-test-task_20260826_frames.csv",
            "log": "session.log",
        }

    def test_the_rig_is_named_as_the_sessions_sources_name_it(self, tmp_path):
        # rig_name / rig_source are what a rig chosen by name records; a rig
        # given as a path has neither, and the card says null, not a guess.
        sources = {"rig": "x", "task": "y", "rig_name": "lab", "rig_source": "shared"}
        rig = self.card(tmp_path, sources=sources)["rig"]
        assert rig["name"] == "lab" and rig["source"] == "shared"
        assert rig["file"] == str(tmp_path / "configs" / "rig-lab.yaml")

        rig = self.card(tmp_path)["rig"]
        assert rig["name"] is None and rig["source"] is None

    def test_no_file_means_no_copy_and_no_path(self, tmp_path):
        card = self.card(tmp_path, identity=RunIdentity(experiment=EXPERIMENT))
        assert card["rig"]["file"] is None
        assert card["params_file"] is None
        assert card["files"]["rig"] is None and card["files"]["params"] is None
        assert card["mode"] is None


class TestWritingTheRecord:
    def test_all_four_are_written_and_the_copies_are_byte_for_byte(self, tmp_path):
        paths = paths_for(tmp_path)
        cfg = make_session_config(tmp_path)

        write_run_identity(cfg, paths, identity_with_files(tmp_path))

        assert paths.rig_copy_path.read_bytes() == RIG_BYTES
        assert paths.params_copy_path.read_bytes() == PARAMS_BYTES
        card = json.loads(paths.session_json_path.read_text(encoding="utf-8"))
        snapshot = yaml.safe_load(paths.snapshot_path.read_text(encoding="utf-8"))
        # One reading of the provenance, quoted by both.
        assert card["created"] == snapshot["provenance"]["created"]
        assert card["experiment"]["git"] == snapshot["provenance"]["experiment_git_sha"]
        assert snapshot["provenance"]["experiment_version"] == "0.1.0"

    def test_a_session_json_that_cannot_be_written_leaves_nothing_behind(self, tmp_path):
        paths = paths_for(tmp_path)
        paths.session_json_path.mkdir()  # a folder cannot be written as a file

        with pytest.raises(OSError, match="session.json"):
            write_run_identity(make_session_config(tmp_path), paths, identity_with_files(tmp_path))

        # The copies written before it are gone again, and there is no
        # snapshot: the folder is left as the build left it, not a run.
        assert not paths.rig_copy_path.exists()
        assert not paths.params_copy_path.exists()
        assert not paths.snapshot_path.exists()
        # What was there before the call is not this call's to delete.
        assert paths.session_json_path.is_dir()

    def test_a_snapshot_that_cannot_be_written_takes_the_rest_with_it(self, tmp_path):
        paths = paths_for(tmp_path)
        paths.snapshot_path.mkdir()

        with pytest.raises(OSError, match="config_snapshot.yaml"):
            write_run_identity(make_session_config(tmp_path), paths, identity_with_files(tmp_path))

        assert [p for p in paths.run_dir.rglob("*") if p.is_file()] == []


class TestASessionRecordsItsSetup:
    """The runner writes the record before trial 1 and stamps the version on
    every place the data goes: the rows, the manifest, the snapshot, the
    database, the log."""

    def run(self, tmp_path, **kwargs):
        harness = SessionHarness(tmp_path, n_trials=2, **kwargs)
        harness.runner.run()
        return harness

    def test_the_record_is_in_the_run_folder_and_in_its_manifest(self, tmp_path):
        identity = RunIdentity(
            experiment=TEST_EXPERIMENT,
            mode="test",
            rig_file=SourceFile(path=tmp_path / "rig.yaml", content=RIG_BYTES),
            params_file=SourceFile(path=tmp_path / "task.yaml", content=PARAMS_BYTES),
        )
        harness = self.run(tmp_path, identity=identity)
        paths = harness.paths

        assert paths.rig_copy_path.read_bytes() == RIG_BYTES
        assert paths.params_copy_path.read_bytes() == PARAMS_BYTES
        card = json.loads(paths.session_json_path.read_text(encoding="utf-8"))
        assert card["mode"] == "test"
        assert card["experiment"]["version"] == TEST_EXPERIMENT.version
        manifest = yaml.safe_load(paths.manifest_path.read_text(encoding="utf-8"))
        listed = {entry["path"] for entry in manifest["artifacts"]}
        assert {"session.json", "rig.yaml", "params.yaml", "config_snapshot.yaml"} <= listed
        assert verify_manifest(paths.run_dir, paths.manifest_path) == []

    def test_the_version_is_on_every_row_the_manifest_and_the_snapshot(self, tmp_path):
        harness = self.run(tmp_path)
        paths = harness.paths

        with paths.trials_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert [row["experiment_version"] for row in rows] == ["0.1.0", "0.1.0"]
        manifest = yaml.safe_load(paths.manifest_path.read_text(encoding="utf-8"))
        assert manifest["experiment_version"] == "0.1.0"
        provenance = yaml.safe_load(paths.snapshot_path.read_text(encoding="utf-8"))["provenance"]
        assert provenance["experiment_name"] == "test-experiment"
        assert provenance["experiment_version"] == "0.1.0"
        assert provenance["experiment_version_source"] == "given to build_session"
        # And the run folder is the version's.
        assert paths.run_dir.relative_to(tmp_path).parts[0] == "v0.1.0"

    def test_the_database_row_carries_the_version(self, tmp_path):
        from alhazen.session.database import DATABASE_FILENAME, ExperimentDatabase

        database = ExperimentDatabase(tmp_path / DATABASE_FILENAME)
        self.run(tmp_path, database=database)

        found = database.find_run("t01", 1, experiment_version="0.1.0")
        assert found["experiment_version"] == "0.1.0"
        assert found["run_id"].startswith("v0.1.0/sub-t01/")

    def test_the_log_names_the_experiment_and_its_version(self, tmp_path):
        harness = self.run(tmp_path)
        log = harness.paths.log_path.read_text(encoding="utf-8")
        assert "experiment: test-experiment 0.1.0 (version from given to build_session)" in log

    def test_a_runner_with_no_version_to_stamp_is_refused(self, tmp_path):
        from alhazen.session.runner import SessionRunner

        harness = SessionHarness(tmp_path, n_trials=1)
        with pytest.raises(ValueError, match="needs identity="):
            SessionRunner(
                cfg=harness.cfg,
                paths=harness.paths,
                display=harness.display,
                screen=harness.runner._screen,
                clock=harness.clock,
                bus=harness.bus,
                engine=harness.engine,
                source=harness.source,
                build_trial=lambda setup: None,
                recorder=harness.recorder,
                frame_monitor=harness.frame_monitor,
                commands=harness.commands,
                refresh_rate_hz=60.0,
                task_rng=None,  # type: ignore[arg-type]
            )

    def test_a_session_json_that_cannot_be_written_is_not_a_run(self, tmp_path, caplog):
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.paths.session_json_path.mkdir()

        with (
            caplog.at_level(logging.WARNING, logger="alhazen.session.runner"),
            pytest.raises(OSError, match="session.json"),
        ):
            harness.runner.run()

        # The snapshot was never written, so nothing else was either.
        assert [p for p in harness.paths.run_dir.rglob("*") if p.is_file()] == []
        assert "config snapshot was never written" in caplog.text
