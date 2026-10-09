"""Paths, recorder tables, manifest, participants registry."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from alhazen.core.events import Event
from alhazen.data import naming
from alhazen.data import paths as paths_module
from alhazen.data.manifest import add_to_manifest, verify_manifest, write_manifest
from alhazen.data.participants import (
    check_participant,
    ensure_participant,
    participants_path,
)
from alhazen.data.paths import (
    LONGEST_FILE_ENDING,
    RunFolder,
    SessionPaths,
    find_runs,
    windows_path_limit,
)
from alhazen.errors import DataError
from alhazen.session.recorder import DataRecorder, ordered_trial_columns


class TestParseRunDirname:
    """`naming.parse_run_dirname` reads back the run number `run_dirname`
    writes. It is what `next_run` counts with, so a name it misreads is a
    run number handed out twice, and a stray folder it accepts is a gap."""

    @pytest.mark.parametrize("run", [1, 2, 9, 10, 99, 100, 1234])
    def test_it_inverts_run_dirname(self, run):
        assert naming.parse_run_dirname(naming.run_dirname(run, "mib-quest")) == run

    def test_a_run_folder_without_a_task_still_counts(self):
        """Counted, because a folder that looks like a run is one somebody
        will think of as that run: skipping it would hand its number out
        again, into the numbering their notes already use."""
        assert naming.parse_run_dirname("run-07") == 7

    @pytest.mark.parametrize(
        "name",
        ["run-", "run-notes", "run-_task-x", "run-1a_task-x", "run--1", "ses-001", "run", ""],
    )
    def test_a_name_that_is_not_a_run_is_none(self, name):
        assert naming.parse_run_dirname(name) is None


class TestSessionPaths:
    def test_layout_and_padding(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "M1", 3, 2, "mib-quest", "20260826", experiment_version="0.1.0"
        )
        assert paths.run_dir == tmp_path / "v0.1.0" / "sub-M1" / "ses-003" / "run-02_task-mib-quest"
        # The task is in the folder's name only: said again in every file
        # name (as before 2.14), it made paths too long for Windows.
        assert paths.trials_path.name == "sub-M1_ses-003_run-02_20260826_trials.csv"
        assert paths.figures_dir.is_dir()
        assert paths.snapshot_path.parent == paths.run_dir


class TestARunFolderTooDeepForWindows:
    """A path of 260 characters or more cannot be opened on a Windows machine
    without long paths switched on. A run's tables are written when the
    session ends, so the refusal has to come before it starts."""

    @staticmethod
    def create(data_root, limit, monkeypatch):
        # The limit is the machine's (None off Windows, and on a Windows
        # machine with long paths on), so the tests set it rather than
        # depend on where they happen to run.
        monkeypatch.setattr(paths_module, "windows_path_limit", lambda: limit)
        return SessionPaths.create(
            data_root, "M1", 3, 2, "mib-quest", "20260826", experiment_version="0.1.0"
        )

    @staticmethod
    def longest(data_root):
        """The absolute path of the longest file the run above can write."""
        run_dir = data_root / "v0.1.0" / "sub-M1" / "ses-003" / "run-02_task-mib-quest"
        return str(run_dir / f"sub-M1_ses-003_run-02_20260826{LONGEST_FILE_ENDING}")

    def test_a_path_one_short_of_the_limit_is_accepted(self, tmp_path, monkeypatch):
        limit = len(self.longest(tmp_path)) + 1
        paths = self.create(tmp_path, limit, monkeypatch)
        assert paths.run_dir.is_dir()

    def test_a_path_at_the_limit_is_refused_with_the_numbers(self, tmp_path, monkeypatch):
        longest = self.longest(tmp_path)
        with pytest.raises(DataError) as refusal:
            self.create(tmp_path, len(longest), monkeypatch)
        message = str(refusal.value)
        # The path, its length, the limit and how much shorter it must get:
        # what a person needs to fix it without counting characters.
        assert longest in message
        assert f"is {len(longest)} characters" in message
        assert f"refuses {len(longest)} or more" in message
        assert "at least 1 character(s) shorter" in message

    def test_a_refusal_creates_nothing(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        with pytest.raises(DataError):
            self.create(data_root, 10, monkeypatch)
        assert not data_root.exists()

    def test_a_relative_data_root_is_measured_from_the_working_directory(
        self, tmp_path, monkeypatch
    ):
        # The file is opened relative to the working directory, so that is
        # the length Windows sees; the relative path alone would pass.
        monkeypatch.chdir(tmp_path)
        relative = Path("data")
        absolute_length = len(self.longest(tmp_path / "data"))
        assert len(self.longest(relative)) < absolute_length
        with pytest.raises(DataError):
            self.create(relative, absolute_length, monkeypatch)

    def test_no_limit_means_no_refusal(self, tmp_path, monkeypatch):
        deep = tmp_path / ("d" * 100)
        assert self.create(deep, None, monkeypatch).run_dir.is_dir()

    def test_no_file_of_a_run_is_longer_than_the_one_measured(self, tmp_path):
        # The refusal measures one name. Every name SessionPaths gives out,
        # and the live monitor's two files under figures/, must fit in it.
        from alhazen.live_monitor.runtime import SAVED_PAGE, SAVED_STATE

        # The shortest base there can be (a one-letter subject), since the
        # fixed names do not shrink with it.
        base = naming.base_name("a", 1, 1, "20260826")
        paths = SessionPaths(run_dir=tmp_path, base=base)
        measured = len(f"{base}{LONGEST_FILE_ENDING}")
        names = [
            getattr(paths, name).relative_to(tmp_path).as_posix()
            for name in dir(SessionPaths)
            if name.endswith(("_path", "_dir")) and name != "run_dir"
        ]
        names += [f"figures/{SAVED_STATE}", f"figures/{SAVED_PAGE}"]
        assert len(names) > 12
        too_long = [name for name in names if len(name) > measured]
        assert not too_long

    def test_the_limit_is_none_off_windows_and_260_or_none_on_it(self):
        import sys

        limit = windows_path_limit()
        if sys.platform == "win32":
            assert limit in (None, 260)
        else:
            assert limit is None

    def test_refuses_overwriting_recorded_run(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        paths.trials_path.write_text("trial_index\n1\n")
        with pytest.raises(DataError, match="refusing to overwrite"):
            SessionPaths.create(
                tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
            )
        # The next run number is fine.
        SessionPaths.create(tmp_path, "M1", 1, 2, "task", "20260826", experiment_version="0.1.0")

    def test_the_same_run_number_on_a_later_day_is_refused(self, tmp_path):
        # The bug this pins: the trials file's name carries the date and the
        # folder's does not, so tomorrow's run passed the check and wrote
        # into today's folder, over its snapshot and manifest.
        today = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        today.trials_path.write_text("trial_index\n1\n")
        today.snapshot_path.write_text("today's snapshot\n")

        with pytest.raises(DataError, match="refusing to overwrite") as refused:
            SessionPaths.create(
                tmp_path, "M1", 1, 1, "task", "20260827", experiment_version="0.1.0"
            )

        # The message names what is there, and nothing was touched.
        assert "config_snapshot.yaml" in str(refused.value)
        assert today.snapshot_path.read_text() == "today's snapshot\n"

    def test_a_run_that_crashed_before_its_trials_file_is_refused_too(self, tmp_path):
        # A session killed mid-run leaves its snapshot and log, never a
        # trials file: its subject still did the work.
        paths = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        paths.snapshot_path.write_text("snapshot\n")
        paths.log_path.write_text("session start\n")
        with pytest.raises(DataError, match="config_snapshot.yaml, session.log"):
            SessionPaths.create(
                tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
            )

    def test_a_file_in_a_subfolder_counts(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        (paths.figures_dir / "live_monitor.html").write_text("<html>")
        with pytest.raises(DataError, match="figures/live_monitor.html"):
            SessionPaths.create(
                tmp_path, "M1", 1, 1, "task", "20260827", experiment_version="0.1.0"
            )

    def test_a_long_list_is_cut_short(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        for name in "abcde":
            (paths.run_dir / f"{name}.txt").write_text(name)
        with pytest.raises(DataError, match=r"\(a.txt, b.txt, c.txt and 2 more\)"):
            SessionPaths.create(
                tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
            )

    def test_a_folder_left_empty_by_a_failed_build_can_be_used(self, tmp_path):
        # A build that failed before the session began (a tracker that would
        # not connect) left only the empty figures folder behind under alhazen
        # 2.6 and earlier — it now removes it (TestUnusedRunFolders) — and
        # trying again with the same number is not an overwrite of anything.
        SessionPaths.create(tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0")
        again = SessionPaths.create(
            tmp_path, "M1", 1, 1, "task", "20260826", experiment_version="0.1.0"
        )
        assert again.figures_dir.is_dir()


def tree(root):
    """Every file and folder under ``root``, with each file's bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


class TestUnusedRunFolders:
    """A session refused at its start removes the folders `create` made for
    it (`discard_unused`), so a refusal leaves the data root as it was and
    spends no run number — and never removes anything it did not make."""

    def create(self, data_root, subject="M1", session=1, run=1):
        return SessionPaths.create(
            data_root, subject, session, run, "task", "20260826", experiment_version="0.1.0"
        )

    def test_create_notes_every_level_it_made_deepest_first(self, tmp_path):
        data_root = tmp_path / "data"
        paths = self.create(data_root)
        assert paths.made == (
            paths.figures_dir,
            paths.run_dir,
            data_root / "v0.1.0" / "sub-M1" / "ses-001",
            data_root / "v0.1.0" / "sub-M1",
            data_root / "v0.1.0",
            data_root,
        )

    def test_a_data_root_it_made_is_gone_again(self, tmp_path):
        paths = self.create(tmp_path / "data")
        removed = paths.discard_unused()
        assert removed == list(paths.made)
        assert list(tmp_path.iterdir()) == []

    def test_what_was_there_before_is_left_exactly_as_it_was(self, tmp_path):
        # Another subject's run, and this subject's registry row: a refused
        # session takes neither with it, and nothing above its own folders.
        other = self.create(tmp_path, subject="M2")
        (other.run_dir / "config_snapshot.yaml").write_text("snapshot", encoding="utf-8")
        registry = "participant_id\nsub-M2\n"
        (tmp_path / "participants.tsv").write_text(registry, encoding="utf-8")
        before = tree(tmp_path)

        self.create(tmp_path).discard_unused()

        assert tree(tmp_path) == before

    def test_it_stops_at_a_level_that_holds_another_run(self, tmp_path):
        first = self.create(tmp_path, run=1)
        (first.run_dir / "session.log").write_text("ran", encoding="utf-8")
        second = self.create(tmp_path, run=2)

        removed = second.discard_unused()

        assert removed == [second.figures_dir, second.run_dir]
        assert (first.run_dir / "session.log").read_text(encoding="utf-8") == "ran"

    def test_a_run_folder_holding_a_file_is_left_whole_and_said_so(self, tmp_path, caplog):
        import logging

        paths = self.create(tmp_path)
        (paths.run_dir / "notes.txt").write_text("not this session's", encoding="utf-8")
        before = tree(tmp_path)

        with caplog.at_level(logging.WARNING, logger="alhazen.data.paths"):
            assert paths.discard_unused() == []

        assert tree(tmp_path) == before
        assert "notes.txt" in caplog.text and "never deletes a file" in caplog.text

    def test_a_folder_it_did_not_make_is_never_touched(self, tmp_path):
        # An empty run folder an older alhazen left behind: reused, as it
        # always was, and not this session's to remove.
        self.create(tmp_path)
        again = self.create(tmp_path)
        assert again.made == ()
        assert again.discard_unused() == []
        assert again.figures_dir.is_dir()


class TestTheVersionLevel:
    """alhazen 2.0 files every run under its experiment's version, so data
    from two versions of a protocol never share a folder."""

    def test_the_run_sits_under_its_version(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "01", 1, 1, "task", "20260826", experiment_version="0.4.0"
        )
        assert paths.run_dir.relative_to(tmp_path).parts == (
            "v0.4.0",
            "sub-01",
            "ses-001",
            "run-01_task-task",
        )

    def test_the_same_numbers_under_another_version_are_another_run(self, tmp_path):
        # A protocol bumped between a morning and an afternoon session starts
        # its own run numbering; neither run is an overwrite of the other.
        morning = SessionPaths.create(
            tmp_path, "01", 1, 1, "task", "20260826", experiment_version="0.4.0"
        )
        morning.trials_path.write_text("trial_index\n1\n")
        afternoon = SessionPaths.create(
            tmp_path, "01", 1, 1, "task", "20260826", experiment_version="0.5.0"
        )
        assert afternoon.run_dir != morning.run_dir
        assert morning.trials_path.read_text() == "trial_index\n1\n"

    def test_the_record_of_the_setup_sits_in_the_run_folder(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "01", 1, 1, "task", "20260826", experiment_version="0.4.0"
        )
        assert paths.session_json_path == paths.run_dir / "session.json"
        assert paths.rig_copy_path == paths.run_dir / "rig.yaml"
        assert paths.params_copy_path == paths.run_dir / "params.yaml"

    @pytest.mark.parametrize("version", ["0.4.0", "1.0rc1", "2.0+lab", "3"])
    def test_a_version_folder_reads_back_as_its_version(self, version):
        assert naming.parse_version_dirname(naming.version_dirname(version)) == version

    @pytest.mark.parametrize("name", ["sub-01", "participants.tsv", "v", "v/x", "ses-001", ""])
    def test_a_name_that_is_not_a_version_folder_is_none(self, name):
        assert naming.parse_version_dirname(name) is None

    def test_a_run_folder_names_its_task(self):
        assert naming.parse_run_task(naming.run_dirname(2, "mib-quest")) == "mib-quest"
        # Counted as a run (parse_run_dirname), but it names no task.
        assert naming.parse_run_task("run-07") is None


class TestFindRuns:
    """`find_runs` is what replaces a ``data/sub-*`` glob: it must find the
    runs recorded since 2.0, one level down under their version, and still
    the ones recorded before, directly under the root."""

    @staticmethod
    def run_folder(root, *parts):
        folder = root.joinpath(*parts)
        folder.mkdir(parents=True)
        return folder

    def test_both_layouts_are_found_and_told_apart(self, tmp_path):
        new = self.run_folder(tmp_path, "v0.4.0", "sub-01", "ses-002", "run-03_task-mib")
        old = self.run_folder(tmp_path, "sub-01", "ses-001", "run-01_task-mib")

        found = {run.path: run for run in find_runs(tmp_path)}

        assert set(found) == {new, old}
        assert found[new] == RunFolder(
            path=new, experiment_version="0.4.0", subject="01", session=2, run=3, task="mib"
        )
        # Pre-2.0: no version folder above it, so no version.
        assert found[old] == RunFolder(
            path=old, experiment_version=None, subject="01", session=1, run=1, task="mib"
        )

    def test_every_version_is_read(self, tmp_path):
        self.run_folder(tmp_path, "v0.4.0", "sub-01", "ses-001", "run-01_task-t")
        self.run_folder(tmp_path, "v0.5.0", "sub-01", "ses-001", "run-01_task-t")
        assert [run.experiment_version for run in find_runs(tmp_path)] == ["0.4.0", "0.5.0"]

    def test_what_is_not_a_run_is_not_returned(self, tmp_path):
        run = self.run_folder(tmp_path, "v0.4.0", "sub-01", "ses-001", "run-01_task-t")
        # What else lives under a data root, none of it a run.
        (tmp_path / "participants.tsv").write_text("participant_id\n")
        (tmp_path / "experiment.sqlite3").write_bytes(b"")
        (tmp_path / "sub-01").mkdir()
        (tmp_path / "sub-01" / "training_state.yaml").write_text("stage: one\n")
        self.run_folder(tmp_path, "notes", "sub-01", "ses-001", "run-01_task-t")
        self.run_folder(tmp_path, "v0.4.0", "sub-01", "ses-abc", "run-01_task-t")
        self.run_folder(tmp_path, "v0.4.0", "sub-01", "ses-001", "run-notes")
        (tmp_path / "v0.4.0" / "sub-01" / "ses-001" / "run-02_task-t").write_text("a file")

        assert [found.path for found in find_runs(tmp_path)] == [run]

    def test_a_root_that_does_not_exist_holds_no_runs(self, tmp_path):
        assert find_runs(tmp_path / "nowhere") == []

    def test_a_run_the_session_made_is_found(self, tmp_path):
        paths = SessionPaths.create(
            tmp_path, "M1", 3, 2, "mib-quest", "20260826", experiment_version="0.4.0"
        )
        (run,) = find_runs(tmp_path)
        assert (run.path, run.experiment_version, run.subject, run.session, run.run) == (
            paths.run_dir,
            "0.4.0",
            "M1",
            3,
            2,
        )


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

    def test_the_experiment_version_is_recorded_beside_the_hashes(self, tmp_path):
        (tmp_path / "trials.csv").write_text("a\n1\n")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path, experiment_version="0.4.0")

        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["schema_version"] == 2
        assert manifest["experiment_version"] == "0.4.0"
        assert verify_manifest(tmp_path, manifest_path) == []


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

    def test_the_experiment_version_survives_a_later_save(self, tmp_path):
        (tmp_path / "trials.csv").write_text("a\n1\n")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path, experiment_version="0.4.0")
        (tmp_path / "report.yaml").write_text("ok: true\n")

        add_to_manifest(tmp_path, manifest_path, [tmp_path / "report.yaml"])

        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["experiment_version"] == "0.4.0"
        assert manifest["schema_version"] == 2

    def test_a_manifest_from_before_2_0_keeps_its_own_schema(self, tmp_path):
        # Stamping 2 on it would claim an experiment_version it does not have.
        (tmp_path / "trials.csv").write_text("a\n1\n")
        manifest_path = tmp_path / "manifest.yaml"
        write_manifest(tmp_path, manifest_path)
        old = yaml.safe_load(manifest_path.read_text())
        old["schema_version"] = 1
        manifest_path.write_text(yaml.safe_dump(old, sort_keys=False))
        (tmp_path / "report.yaml").write_text("ok: true\n")

        add_to_manifest(tmp_path, manifest_path, [tmp_path / "report.yaml"])

        manifest = yaml.safe_load(manifest_path.read_text())
        assert manifest["schema_version"] == 1
        assert "experiment_version" not in manifest
        assert verify_manifest(tmp_path, manifest_path) == []


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

    def test_a_failed_write_leaves_the_registry_whole(self, tmp_path, monkeypatch):
        # The registry is a record, and adding a subject rewrites all of it.
        # It was rewritten in place, so a crash or a full disk part-way
        # through left a truncated file: every subject registered before
        # was gone from the only copy.
        ensure_participant(tmp_path, "s01", {"species": "macaque"})
        before = participants_path(tmp_path).read_bytes()

        def disk_full(self):
            raise OSError(28, "No space left on device")

        # The first thing the rewrite writes: past this point the old code
        # had already emptied the file.
        monkeypatch.setattr(csv.DictWriter, "writeheader", disk_full)
        with pytest.raises(OSError, match="No space left"):
            ensure_participant(tmp_path, "s02")

        assert participants_path(tmp_path).read_bytes() == before
        # And no half-written temporary file is left beside it.
        assert [p.name for p in tmp_path.iterdir()] == ["participants.tsv"]

    def test_the_file_keeps_its_line_endings(self, tmp_path):
        # The atomic rewrite must write the same bytes the in-place one did:
        # csv's own CRLF, not a CRLF that a text-mode file doubles on Windows.
        ensure_participant(tmp_path, "s01")
        ensure_participant(tmp_path, "s02")
        assert participants_path(tmp_path).read_bytes() == (
            b"participant_id\r\nsub-s01\r\nsub-s02\r\n"
        )

    def test_a_row_wider_than_its_header_is_refused_naming_the_row(self, tmp_path):
        # A hand edit that left an extra cell (a stray tab) has no column to
        # go under. csv read it under a None key and then refused to write it
        # back with a ValueError that named neither the file nor the row.
        path = participants_path(tmp_path)
        path.write_text("participant_id\tspecies\nsub-s01\tmacaque\textra\n", encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(DataError) as error:
            ensure_participant(tmp_path, "s02")

        message = str(error.value)
        assert str(path) in message
        assert "line 2" in message and "sub-s01" in message
        assert "'extra'" in message
        # Refused before anything was written: the file is as the human left it.
        assert path.read_bytes() == before


class TestInitialsInTheRegistry:
    """alhazen 2.0: a subject's initials are recorded beside its id, and are
    a check on the subject number — a later session with the same id and
    other initials is refused, before anything is written."""

    def rows(self, tmp_path):
        with participants_path(tmp_path).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    def test_the_first_session_records_them(self, tmp_path):
        ensure_participant(tmp_path, "01", initials="HD")
        assert self.rows(tmp_path) == [{"participant_id": "sub-01", "initials": "HD"}]

    def test_the_same_initials_again_change_nothing(self, tmp_path):
        ensure_participant(tmp_path, "01", initials="HD")
        before = participants_path(tmp_path).read_bytes()

        ensure_participant(tmp_path, "01", initials="HD")
        check_participant(tmp_path, "01", "HD")

        assert participants_path(tmp_path).read_bytes() == before

    def test_other_initials_for_the_same_subject_are_refused_naming_both(self, tmp_path):
        ensure_participant(tmp_path, "01", initials="HD")
        before = participants_path(tmp_path).read_bytes()

        for attempt in (
            lambda: check_participant(tmp_path, "01", "XY"),
            lambda: ensure_participant(tmp_path, "01", initials="XY"),
        ):
            with pytest.raises(DataError) as refused:
                attempt()
            assert str(refused.value).startswith(
                "sub-01 is recorded as HD; this session says XY — check the subject number"
            )
            assert str(participants_path(tmp_path)) in str(refused.value)

        assert participants_path(tmp_path).read_bytes() == before

    def test_a_hand_edited_cell_is_read_as_recorded(self, tmp_path):
        # Lowercase or padded by a person editing the file is the same person.
        participants_path(tmp_path).write_text(
            "participant_id\tinitials\nsub-01\t hd \n", encoding="utf-8"
        )
        check_participant(tmp_path, "01", "HD")
        ensure_participant(tmp_path, "01", initials="HD")
        with pytest.raises(DataError, match="recorded as HD"):
            check_participant(tmp_path, "01", "XY")

    def test_a_subject_from_before_2_0_has_them_filled_in(self, tmp_path):
        # A registry written before initials existed: no column at all.
        participants_path(tmp_path).write_text(
            "participant_id\tspecies\nsub-01\tmacaque\nsub-02\tmacaque\n", encoding="utf-8"
        )

        check_participant(tmp_path, "01", "HD")  # not refused
        ensure_participant(tmp_path, "01", initials="HD")

        assert self.rows(tmp_path) == [
            {"participant_id": "sub-01", "species": "macaque", "initials": "HD"},
            {"participant_id": "sub-02", "species": "macaque", "initials": ""},
        ]

    def test_a_blank_cell_is_filled_in_too(self, tmp_path):
        ensure_participant(tmp_path, "01")  # a session that gave none
        ensure_participant(tmp_path, "02", initials="AB")
        ensure_participant(tmp_path, "01", initials="HD")

        assert self.rows(tmp_path) == [
            {"participant_id": "sub-01", "initials": "HD"},
            {"participant_id": "sub-02", "initials": "AB"},
        ]

    def test_a_session_that_gives_none_checks_and_changes_nothing(self, tmp_path):
        ensure_participant(tmp_path, "01", initials="HD")
        before = participants_path(tmp_path).read_bytes()

        check_participant(tmp_path, "01", None)
        ensure_participant(tmp_path, "01")

        assert participants_path(tmp_path).read_bytes() == before

    def test_nothing_to_check_against_is_not_a_refusal(self, tmp_path):
        check_participant(tmp_path, "01", "HD")  # no registry yet
        ensure_participant(tmp_path, "02", initials="AB")
        check_participant(tmp_path, "01", "HD")  # a subject not registered yet
        assert not [row for row in self.rows(tmp_path) if row["participant_id"] == "sub-01"]
