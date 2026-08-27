"""SessionRunner: the outer-loop contract against fakes."""

from __future__ import annotations

import csv

import pytest
import yaml

from alhazen.core.commands import Command
from alhazen.testing import ScriptedCommands
from support import SessionHarness


def read_trials(harness):
    with harness.paths.trials_path.open() as f:
        return list(csv.DictReader(f))


class TestHappyPath:
    def test_full_session_writes_everything(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=2)
        harness.runner.run()

        rows = read_trials(harness)
        assert len(rows) == 2
        assert [r["outcome"] for r in rows] == ["COMPLETED", "COMPLETED"]
        assert [r["trial_index"] for r in rows] == ["1", "2"]
        assert rows[0]["condition"] == "a"

        assert harness.paths.snapshot_path.exists()
        snap = yaml.safe_load(harness.paths.snapshot_path.read_text())
        assert snap["config"]["info"]["seed"] == 7

        assert harness.paths.events_path.exists()
        assert harness.paths.frames_path.exists()
        assert harness.paths.manifest_path.exists()
        assert harness.paths.log_path.exists()
        assert (tmp_path / "participants.tsv").read_text().splitlines()[1] == "sub-t01"

        assert harness.display.closed

    def test_score_hook_applied(self, tmp_path):
        def score(record):
            record["my_metric"] = 42.0
            return record

        harness = SessionHarness(tmp_path, n_trials=1, score=score)
        harness.runner.run()
        assert read_trials(harness)[0]["my_metric"] == "42.0"

    def test_manifest_covers_run_dir(self, tmp_path):
        from alhazen.data.manifest import verify_manifest

        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner.run()
        assert verify_manifest(harness.paths.run_dir, harness.paths.manifest_path) == []


class TestParadigmSummary:
    def test_a_scheduler_with_a_summary_writes_it(self, tmp_path):
        import numpy as np

        from alhazen.paradigms.constant import ConstantStimuli

        source = ConstantStimuli(
            {"side": ["left", "right"]}, n_per_condition=1, rng=np.random.default_rng(0)
        )
        harness = SessionHarness(tmp_path, source=source)
        harness.runner.run()

        assert harness.paths.paradigm_path.exists()
        with harness.paths.paradigm_path.open() as f:
            rows = list(csv.DictReader(f))
        # The table that says whether the session ended balanced.
        assert sorted(r["side"] for r in rows) == ["left", "right"]
        assert all(r["n_completed"] == "1" for r in rows)

    def test_a_scheduler_with_nothing_to_say_writes_no_file(self, tmp_path):
        # An absent file means "this paradigm had no summary", not "something
        # failed to write" — so SimpleSequence leaves none.
        harness = SessionHarness(tmp_path, n_trials=1)
        harness.runner.run()
        assert not harness.paths.paradigm_path.exists()


class TestPauseSemantics:
    def test_paused_trial_requeues_but_writes_no_row(self, tmp_path):
        # PAUSE lands on the first frame of trial 1; unattended runs
        # auto-resume, the condition is re-served, and the session still
        # collects its 2 completed measurements.
        commands = ScriptedCommands([[Command.PAUSE]])
        harness = SessionHarness(tmp_path, n_trials=2, commands=commands)
        harness.runner.run()

        rows = read_trials(harness)
        assert [r["outcome"] for r in rows] == ["COMPLETED", "COMPLETED"]
        # The paused attempt consumed trial_index 1 and attempt 1.
        assert [r["trial_index"] for r in rows] == ["2", "3"]
        assert [r["attempt"] for r in rows] == ["2", "3"]
        assert "PAUSED" in harness.collector.names()
        assert "RESUMED" in harness.collector.names()

    def test_quit_ends_session_but_saves_data(self, tmp_path):
        commands = ScriptedCommands([[Command.QUIT]])
        harness = SessionHarness(tmp_path, n_trials=5, commands=commands)
        harness.runner.run()
        assert read_trials(harness) == []
        assert harness.paths.trials_path.exists()
        assert harness.paths.manifest_path.exists()

    def test_skip_writes_aborted_row_and_requeues(self, tmp_path):
        commands = ScriptedCommands([[Command.SKIP_TRIAL]])
        harness = SessionHarness(tmp_path, n_trials=1, commands=commands)
        harness.runner.run()
        rows = read_trials(harness)
        assert [r["outcome"] for r in rows] == ["ABORTED", "COMPLETED"]
        assert rows[0]["abort_reason"] == "skipped_by_user"


class TestTeardownResilience:
    def test_all_steps_attempted_and_first_error_reraised(self, tmp_path):
        harness = SessionHarness(tmp_path, n_trials=1)

        def broken_write():
            raise OSError("disk full")

        harness.recorder.write = broken_write  # type: ignore[method-assign]
        with pytest.raises(OSError, match="disk full"):
            harness.runner.run()
        # Every later step still ran.
        assert harness.paths.frames_path.exists()
        assert harness.paths.manifest_path.exists()
        assert harness.display.closed

    def test_teardown_error_never_masks_session_error(self, tmp_path):
        def broken_build(setup):
            raise RuntimeError("task bug")

        harness = SessionHarness(tmp_path, n_trials=1, build_trial=broken_build)

        def broken_write():
            raise OSError("disk full")

        harness.recorder.write = broken_write  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="task bug"):
            harness.runner.run()
