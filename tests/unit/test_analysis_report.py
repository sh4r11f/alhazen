"""The report's own arithmetic, against tables written by hand.

The integration test builds a run where every trial completes, which is
exactly the shape that cannot see a completed-rate that counts every row.
"""

from __future__ import annotations

import pytest

from alhazen.analysis.io.session import load_run
from alhazen.analysis.report import build_report
from alhazen.data.manifest import write_manifest

BASE = "sub-t01_ses-001_run-01_task-demo_20260826"


def write_run(tmp_path, trials_csv: str, frames_csv: str | None = None):
    run = tmp_path / "sub-t01" / "ses-001" / "run-01_task-demo"
    run.mkdir(parents=True)
    (run / f"{BASE}_trials.csv").write_text(trials_csv)
    (run / f"{BASE}_events.csv").write_text("trial_index,event,t,payload_json\n")
    (run / f"{BASE}_frames.csv").write_text(
        frames_csv or "trial_index,t,interval_s,dropped\n1,0.0,0.0167,False\n"
    )
    (run / "config_snapshot.yaml").write_text("config: {info: {subject: t01}}\n")
    write_manifest(run, run / "manifest.yaml")
    return run


class TestCompletedRate:
    """`completed_rate` counted every row whose outcome was not PAUSED or
    ABORTED. Incomplete outcomes have the experiment's own names — a broken
    fixation is an incomplete trial and DOES write a row — so a shaping
    session's engagement rate read as 100% no matter how the animal did."""

    def test_an_incomplete_outcome_is_not_counted_as_completed(self, tmp_path):
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,completed,success\n"
            "1,1,CORRECT,True,True\n"
            "2,1,BROKE_FIX,False,\n"
            "3,1,CORRECT,True,True\n"
            "4,1,BROKE_FIX,False,\n",
        )

        report = build_report(run_dir)

        assert report.trials["n_rows"] == 4
        assert report.trials["completed"] == 2
        assert report.trials["completed_rate"] == pytest.approx(0.5)

    def test_the_outcome_breakdown_still_counts_every_row(self, tmp_path):
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,completed,success\n"
            "1,1,CORRECT,True,True\n"
            "2,1,BROKE_FIX,False,\n",
        )

        report = build_report(run_dir)

        assert report.trials["outcomes"] == {"CORRECT": 1, "BROKE_FIX": 1}

    def test_old_data_without_the_column_falls_back(self, tmp_path):
        # Runs recorded before the column existed still have to report; the
        # fallback is the old heuristic, and it is documented as a heuristic.
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,success\n1,1,CORRECT,True\n2,1,ABORTED,\n",
        )

        report = build_report(run_dir)

        assert report.trials["completed"] == 1

    def test_an_empty_table_reports_zero_rather_than_dividing(self, tmp_path):
        run_dir = write_run(tmp_path, "trial_index,attempt,outcome,completed,success\n")

        report = build_report(run_dir)

        assert report.trials == {
            "n_rows": 0,
            "completed": 0,
            "completed_rate": 0.0,
            "outcomes": {},
        }


class TestFrames:
    def test_dropped_frames_are_reported_per_trial(self, tmp_path):
        """A session's total drop count says nothing about whether they were
        spread evenly or all landed in one trial's stimulus."""
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,completed,success\n1,1,CORRECT,True,True\n"
            "2,1,CORRECT,True,True\n",
            frames_csv=(
                "trial_index,t,interval_s,dropped\n"
                "1,0.000,0.0167,False\n"
                "2,0.017,0.0500,True\n"
                "2,0.067,0.0500,True\n"
            ),
        )

        report = build_report(run_dir)

        assert report.frames["n_dropped"] == 2
        assert report.frames["dropped_by_trial"] == {2: 2}

    def test_the_rows_own_count_is_used_when_it_is_larger(self, tmp_path):
        """The trials table and the frame log count the same thing from two
        places; reporting the larger means neither can hide a drop."""
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,completed,success,n_dropped_frames\n"
            "1,1,CORRECT,True,True,3\n",
            frames_csv="trial_index,t,interval_s,dropped\n1,0.0,0.0167,False\n",
        )

        report = build_report(run_dir)

        assert report.frames["dropped_by_trial"] == {1: 3}

    def test_a_clean_session_lists_no_trials(self, tmp_path):
        run_dir = write_run(
            tmp_path, "trial_index,attempt,outcome,completed,success\n1,1,CORRECT,True,True\n"
        )

        assert build_report(run_dir).frames["dropped_by_trial"] == {}


class TestTypedRowsReachTheReport:
    def test_a_false_success_is_false_not_a_truthy_string(self, tmp_path):
        run_dir = write_run(
            tmp_path,
            "trial_index,attempt,outcome,completed,success\n1,1,WRONG,True,False\n",
        )

        run = load_run(run_dir)

        # Read as a string this was `"False"`, which is truthy — so `not
        # row["success"]` silently meant the opposite of what it reads as.
        assert not run.trials["success"].iloc[0]
