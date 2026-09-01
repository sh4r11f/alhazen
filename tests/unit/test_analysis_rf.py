"""Offline RF maps: the run's own snapshot in, a map with a known peak out."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alhazen.analysis import rf
from alhazen.analysis.io.session import RunData
from alhazen.errors import DataError

PARAMS = {
    "grid_cols": 3,
    "grid_rows": 3,
    "grid_extent_x_dva": 6.0,
    "grid_extent_y_dva": 6.0,
    "grid_center_x_dva": 0.0,
    "grid_center_y_dva": 0.0,
    "window_start_ms": 20.0,
    "window_end_ms": 120.0,
}


def probe_events(onsets: list[tuple[float, int, int, str]]) -> pd.DataFrame:
    rows = []
    for t, col, row, polarity in onsets:
        rows.append(
            {
                "trial_index": 1,
                "event": "PROBE_ON",
                "t": t,
                "payload_json": json.dumps(
                    {
                        "index": 0,
                        "col": col,
                        "row": row,
                        "x_dva": -2.0 + 2.0 * col,
                        "y_dva": -2.0 + 2.0 * row,
                        "polarity": polarity,
                    }
                ),
            }
        )
    return pd.DataFrame(rows)


def run_data(events: pd.DataFrame, params: dict | None = None) -> RunData:
    return RunData(
        run_dir=Path("/nowhere/run-01"),
        base="base",
        trials=pd.DataFrame(),
        events=events,
        frames=pd.DataFrame(),
        snapshot={"config": {"task_params": PARAMS if params is None else params}},
    )


class TestReadingTheRun:
    def test_grid_and_window_come_from_the_snapshot(self):
        run = run_data(probe_events([(1.0, 0, 0, "bright")]))
        grid = rf.probe_grid(run)
        assert (grid.cols, grid.rows) == (3, 3)
        assert grid.cell_center_dva(0, 0) == (-2.0, -2.0)
        assert rf.counting_window(run) == (0.02, 0.12)

    def test_a_non_rf_run_is_named_not_guessed(self):
        run = run_data(probe_events([(1.0, 0, 0, "bright")]), params={"other": 1})
        with pytest.raises(DataError, match="does not look like an RF-mapping run"):
            rf.probe_grid(run)

    def test_probe_onsets_decode_the_payloads(self):
        run = run_data(probe_events([(1.0, 0, 2, "bright"), (1.5, 2, 1, "dark")]))
        onsets = rf.probe_onsets(run)
        assert onsets["t"].tolist() == [1.0, 1.5]
        assert onsets["col"].tolist() == [0, 2]
        assert onsets["polarity"].tolist() == ["bright", "dark"]

    def test_a_run_with_no_probes_is_loud(self):
        empty = run_data(
            pd.DataFrame(
                {"event": ["TRIAL_START"], "t": [0.0], "trial_index": [1], "payload_json": ["{}"]}
            )
        )
        with pytest.raises(DataError, match="no PROBE_ON events"):
            rf.probe_onsets(empty)


class TestMapping:
    def test_planted_responses_recover_the_planted_cell(self):
        # Cell (1, 2) always answered by unit 7, 50 ms after onset; unit 3
        # never does. Twenty onsets across cells.
        onsets = []
        spike_times = []
        spike_labels = []
        t = 10.0
        for _rep in range(2):
            for row in range(3):
                for col in range(3):
                    onsets.append((t, col, row, "bright"))
                    if (col, row) == (1, 2):
                        spike_times += [t + 0.05, t + 0.06, t + 0.07]
                        spike_labels += [7, 7, 7]
                    spike_times.append(t + 0.5)  # outside every window
                    spike_labels.append(3)
                    t += 1.0
        run = run_data(probe_events(onsets))
        acc, labels = rf.map_from_spike_times(
            rf.probe_onsets(run),
            spike_times_s=np.array(spike_times),
            spike_labels=np.array(spike_labels),
            grid=rf.probe_grid(run),
            window_s=rf.counting_window(run),
        )
        assert labels == [3, 7]
        assert acc.peak_cell(labels.index(7)) == (1, 2)
        assert acc.rate_map(labels.index(7))[2, 1] == pytest.approx(3 / 0.1)
        assert np.nansum(acc.rate_map(labels.index(3))) == 0.0

    def test_polarity_filter_splits_on_and_off_maps(self):
        onsets = [(1.0, 0, 0, "bright"), (2.0, 0, 0, "dark"), (3.0, 1, 1, "bright")]
        run = run_data(probe_events(onsets))
        spikes = np.array([1.05, 2.05])
        labels = np.array([0, 0])
        bright, _ = rf.map_from_spike_times(
            rf.probe_onsets(run),
            spike_times_s=spikes,
            spike_labels=labels,
            grid=rf.probe_grid(run),
            window_s=(0.02, 0.12),
            polarity="bright",
        )
        assert bright.n_flashes == 2  # the dark flash is excluded
        assert bright.counts.sum() == 1

    def test_mismatched_spike_arrays_are_refused(self):
        run = run_data(probe_events([(1.0, 0, 0, "bright")]))
        with pytest.raises(DataError, match="parallel"):
            rf.map_from_spike_times(
                rf.probe_onsets(run),
                spike_times_s=np.array([1.0, 2.0]),
                spike_labels=np.array([0]),
                grid=rf.probe_grid(run),
                window_s=(0.0, 0.1),
            )
