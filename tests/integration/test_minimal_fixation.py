"""Acceptance: the minimal_fixation example runs a full simulated session on
a bare machine and produces the documented files — through the real builder
(in-process) and through the example script itself (subprocess)."""

from __future__ import annotations

import csv
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import yaml

from alhazen.data.manifest import verify_manifest
from alhazen.session.database import DeviceSample, ExperimentDatabase
from support import load_example_task

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "minimal_fixation"


def test_builder_session_end_to_end(tmp_path):
    task_module = load_example_task(EXAMPLE_DIR)
    from alhazen import Duration, RigConfig, build_session
    from alhazen.config.models import DisplayConfig, MonitorConfig
    from alhazen.paradigms.config import SchedulerConfig

    rig = RigConfig(
        monitor=MonitorConfig(
            width_px=1920,
            height_px=1080,
            width_cm=60.0,
            distance_cm=60.0,
            refresh_rate_hz=60.0,
            fullscreen=False,
        ),
        display=DisplayConfig(backend="simulated"),
        data_root=tmp_path,
    )
    params = task_module.FixationParams(
        fixation_duration=Duration(ms=40),
        iti=Duration(ms=5),
        paradigm=SchedulerConfig(n_per_condition=3),
    )
    runner = build_session(
        rig=rig,
        subject="demo",
        session=1,
        run=1,
        task=task_module.MinimalFixationTask(params),
        seed=1234,
        iti=params.iti,
        simulated_frame_period_s=0.0,
    )
    runner.run()

    run_dir = tmp_path / "sub-demo" / "ses-001" / "run-01_task-minimal-fixation"
    assert run_dir.is_dir()

    trials_path = next(run_dir.glob("*_trials.csv"))
    with trials_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3
    assert all(r["outcome"] == "COMPLETED" for r in rows)
    assert all(r["success"] == "True" for r in rows)
    assert all("t_fix_on" in r and float(r["t_fix_on"]) > 0 for r in rows)

    events_path = next(run_dir.glob("*_events.csv"))
    with events_path.open() as f:
        names = [row["event"] for row in csv.DictReader(f)]
    assert names.count("TRIAL_START") == 3
    assert names.count("FIX_ON") == 3
    assert names.count("TRIAL_END") == 3

    snap = yaml.safe_load((run_dir / "config_snapshot.yaml").read_text())
    assert snap["config"]["info"]["seed"] == 1234
    assert snap["provenance"]["alhazen_version"]

    assert next(run_dir.glob("*_frames.csv")).exists()
    assert verify_manifest(run_dir, run_dir / "manifest.yaml") == []
    assert "sub-demo" in (tmp_path / "participants.tsv").read_text()

    database_path = tmp_path / "experiment.sqlite3"
    assert database_path.exists()
    with sqlite3.connect(database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(names)
        assert db.execute("SELECT COUNT(*) FROM frames").fetchone()[0] > 0
        assert db.execute("SELECT COUNT(*) FROM frame_inputs").fetchone()[0] > 0
        artifact_names = {row[0] for row in db.execute("SELECT path FROM artifacts")}
        assert "config_snapshot.yaml" in artifact_names
        assert "manifest.yaml" in artifact_names

    experiment_db = ExperimentDatabase(database_path)
    # Read back rather than reconstructed: the run_id carries the run's date,
    # which is what lets the same subject/session/run run again tomorrow.
    with experiment_db.connect() as db:
        (run_id,) = db.execute("SELECT run_id FROM runs").fetchone()
    assert run_id.startswith("sub-demo/ses-001/run-01/task-minimal-fixation/")
    first_frame = experiment_db.frame_snapshot(
        subject="demo", session=1, run=1, trial_index=1, frame_index=0
    )
    experiment_db.ingest_device_samples(
        run_id,
        device="test-ephys",
        stream="ap",
        sample_rate_hz=30_000,
        samples=[
            DeviceSample(
                channel="17",
                sample_index=42,
                t_device=0.0014,
                t_session=first_frame["t_session"],
                value=-127,
            )
        ],
    )
    snapshot = experiment_db.frame_snapshot(
        subject="demo", session=1, run=1, trial_index=1, frame_index=0
    )
    assert snapshot["devices"][0]["channel"] == "17"
    assert snapshot["devices"][0]["value_real"] == -127

    experiment_db.ingest_dense_stream(
        run_id,
        device="test-ephys",
        stream="dense-ap",
        values=np.array([[1, 2], [3, 4], [5, 6]], dtype=np.int16),
        channels=["0", "1"],
        sample_rate_hz=1000,
        t_device_start=10.0,
        t_session_start=first_frame["t_session"],
    )
    snapshot = experiment_db.frame_snapshot(
        subject="demo", session=1, run=1, trial_index=1, frame_index=0
    )
    dense = [row for row in snapshot["devices"] if row["stream"] == "dense-ap"]
    assert [(row["channel"], row["value_real"]) for row in dense] == [("0", 1), ("1", 2)]


def test_example_script_runs_as_documented(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "run.py"), "--data-root", str(tmp_path), "--seed", "1"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "session complete" in proc.stdout
    run_dir = tmp_path / "sub-demo" / "ses-001" / "run-01_task-minimal-fixation"
    assert next(run_dir.glob("*_trials.csv")).exists()
    # A second invocation picks the next run number instead of refusing.
    proc2 = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "run.py"), "--data-root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc2.returncode == 0, proc2.stderr
    assert (tmp_path / "sub-demo" / "ses-001" / "run-02_task-minimal-fixation").is_dir()
