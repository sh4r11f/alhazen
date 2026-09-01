"""Acceptance: an RF-mapping session end to end, with nothing installed.

The whole live pipeline through the real wiring — ``build_mode_session`` in
simulate mode, the real builder, the real runner — on a simulated display,
with the task's own fixating autopilot in the chair and a simulated spike
source with ground-truth receptive fields on the rig. What comes out is
what a real session produces: trials, flip-stamped PROBE_ON events, the
paradigm's per-cell coverage, and the live map artifact — covered by the
manifest, because the map is written before the manifest is.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from alhazen.analysis.io.session import load_run
from alhazen.config.loader import load_model, load_rig
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    MonitorConfig,
    RigConfig,
    SpikeSourceConfig,
)
from alhazen.modes import Mode
from alhazen.modes.session import build_mode_session
from alhazen.task.templates.rf_mapping import V1RFMapTask
from support import load_example_task

EXAMPLE_DIR = Path(__file__).parents[2] / "examples" / "rf_mapping"


def small_params() -> V1RFMapTask.params_model:
    """A 3x3, one-repetition session: nine flashes, two fixation trials,
    a couple of seconds of wall clock."""
    return V1RFMapTask.params_model(
        grid_cols=3,
        grid_rows=3,
        grid_extent_x_dva=6.0,
        grid_extent_y_dva=6.0,
        n_reps_per_cell=1,
        probes_per_trial=5,
        flash={"ms": 50},
        isi={"ms": 30},
        window_start_ms=10.0,
        window_end_ms=60.0,
        initial_hold={"ms": 60},
        acquire_timeout={"ms": 500},
        iti={"ms": 0},
        n_display_maps=2,
    )


def sim_rig(tmp_path: Path) -> RigConfig:
    return RigConfig(
        monitor=MonitorConfig(
            width_px=1920,
            height_px=1080,
            width_cm=60.0,
            distance_cm=60.0,
            refresh_rate_hz=60.0,
            fullscreen=False,
        ),
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(
            spikes=SpikeSourceConfig(
                backend="simulated",
                sim_channels=3,
                sim_rf_centers_dva=((2.0, 2.0), (-2.0, -2.0), (0.0, 0.0)),
                sim_rf_sigma_dva=1.0,
                sim_baseline_hz=2.0,
                sim_peak_hz=300.0,
                sim_latency_ms=20.0,
                sim_duration_ms=30.0,
            )
        ),
        data_root=tmp_path / "data",
    )


@pytest.mark.slow
def test_simulated_rf_mapping_session_end_to_end(tmp_path):
    built = build_mode_session(
        Mode.SIMULATE,
        rig=sim_rig(tmp_path),
        task=V1RFMapTask(small_params()),
        subject="sim",
        session=1,
        seed=11,
        # Paced simulated flips (the default), deliberately: the flash
        # sequence is frame-counted, so an unpaced display would flash all
        # nine probes within a millisecond of real time and every counting
        # window would cover every response. Paced at 60 Hz, the timing the
        # spike windows depend on is real — which is exactly what this test
        # exists to exercise. ~1.5 s of wall clock, hence the slow marker.
    )
    built.runner.run()

    run_dir = next((built.data_root / "sub-sim" / "ses-001").glob("run-01_*"))

    # The manifest verifies — which proves the live map was written BEFORE
    # the manifest, or load_run would report an unlisted file.
    run = load_run(run_dir)
    assert run.manifest_problems == []

    # Every planned flash was shown, across however many trials it took.
    probes = run.events.loc[run.events["event"] == "PROBE_ON"]
    assert len(probes) == 9
    payloads = [json.loads(raw) for raw in probes["payload_json"]]
    assert {(p["col"], p["row"]) for p in payloads} == {(c, r) for c in range(3) for r in range(3)}
    # Flip-stamped and strictly ordered on the one session clock.
    assert probes["t"].astype(float).is_monotonic_increasing

    # The trials table carries the same log the events do.
    assert run.trials["rf_n_probes_shown"].astype(int).sum() == 9
    assert set(run.trials["outcome"]) == {"COMPLETED"}

    # The paradigm summary says the coverage is complete, cell by cell.
    with (run_dir / f"{run.base}_paradigm.csv").open() as f:
        cells = list(csv.DictReader(f))
    assert len(cells) == 9
    assert all(row["shown"] == "1" for row in cells)

    # The live map artifact: every flash mapped, and the channel whose
    # ground-truth field sat at (+2, +2) peaks in that cell.
    saved = np.load(run_dir / "rf_live_maps.npz")
    assert saved["flashes"].sum() == 9
    assert saved["n_unmapped_flashes"] == 0
    counts = saved["counts"]  # (3 channels, rows, cols); row 0 = bottom
    channel_0 = counts[0]
    row, col = np.unravel_index(channel_0.argmax(), channel_0.shape)
    assert (col, row) == (2, 2)  # the cell centred on (+2, +2)
    channel_1 = counts[1]
    row, col = np.unravel_index(channel_1.argmax(), channel_1.shape)
    assert (col, row) == (0, 0)  # the cell centred on (-2, -2)


@pytest.mark.slow
def test_the_example_adopts_the_template_and_runs(tmp_path):
    """The downstream pattern the example demonstrates, proven end to end:
    subclass a preset, re-centre the grid, run through the same wiring —
    with every example config loaded through its real loader, because a
    config that merely exists is not a config that works."""
    module = load_example_task(EXAMPLE_DIR)
    rig = load_rig(EXAMPLE_DIR / "rig-sim.yaml")
    load_rig(EXAMPLE_DIR / "rig-demo.yaml")
    load_rig(EXAMPLE_DIR / "rig-lab.yaml")
    load_model(EXAMPLE_DIR / "task.yaml", module.ArrayV4MapParams)

    rig = rig.model_copy(update={"data_root": tmp_path / "data"})
    params = module.ArrayV4MapParams(
        grid_cols=2,
        grid_rows=2,
        n_reps_per_cell=1,
        probes_per_trial=4,
        flash={"ms": 50},
        isi={"ms": 30},
        window_start_ms=10.0,
        window_end_ms=60.0,
        initial_hold={"ms": 60},
        iti={"ms": 0},
    )
    built = build_mode_session(
        Mode.SIMULATE,
        rig=rig,
        task=module.ArrayV4MapTask(params),
        subject="sim",
        session=1,
        seed=5,
    )
    built.runner.run()

    run_dir = next((built.data_root / "sub-sim" / "ses-001").glob("run-01_*"))
    run = load_run(run_dir)
    assert run.manifest_problems == []
    # The grid the example pinned is the grid that ran: probes sit in the
    # lower-left quadrant the task re-centred onto.
    probes = run.events.loc[run.events["event"] == "PROBE_ON"]
    assert len(probes) == 4
    xs = [json.loads(raw)["x_dva"] for raw in probes["payload_json"]]
    assert all(x < 0 for x in xs)
    assert (run_dir / "rf_live_maps.npz").exists()
