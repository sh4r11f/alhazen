"""Receptive-field maps from a recorded run — the offline half.

The live map (``task.templates.rf_mapping.LiveRFMap``) is a quick look over
threshold crossings and a network clock estimate. This module recomputes the
same arithmetic from the records, properly: the flip-stamped ``PROBE_ON``
events out of the run's own files, spike times from wherever the spikes now
live (Kilosort units via ``analysis.io.kilosort``, or re-thresholded raw
data via ``analysis.io.spikeglx`` + ``alhazen.neural.detect``), and the TTL
clock alignment (``analysis.sync``) instead of the live estimate.

The composition, end to end:

```python
from alhazen.analysis import rf
from alhazen.analysis.io.kilosort import read_kilosort
from alhazen.analysis.io.session import load_run
from alhazen.analysis.sync import align_run

run = load_run(run_dir)
onsets = rf.probe_onsets(run)                       # session clock
fit = align_run(run, neural_run_dir)                # via the TTL pulses
spikes = read_kilosort(sort_dir, sample_rate_hz)    # recording clock
maps, unit_ids = rf.map_from_spike_times(
    onsets,
    spike_times_s=fit.to_behavior(spikes.times_s),  # -> session clock
    spike_labels=spikes.clusters,
    grid=rf.probe_grid(run),
    window_s=rf.counting_window(run),
)
maps.rate_map(unit_ids.index(unit))                 # spikes/s per cell
```

Everything geometric comes from the run's own snapshot — the grid an
analysis re-declared by hand will one day be wrong about a session it was
not written for.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from alhazen.analysis.io.session import RunData, event_payloads
from alhazen.errors import DataError
from alhazen.neural.rfmap import ProbeGrid, RFAccumulator

# The event the RF-mapping template stamps each flash with. A constant here
# rather than an import from the task layer: analysis deliberately imports
# nothing above itself, and the name is part of the on-disk record now.
PROBE_EVENT = "PROBE_ON"

# The task_params keys the grid and window are rebuilt from. The snapshot
# stores the fully-validated params (defaults included), so every key is
# present in any run the RF template produced; a missing one means this run
# came from some other task.
_GRID_KEYS = (
    "grid_cols",
    "grid_rows",
    "grid_extent_x_dva",
    "grid_extent_y_dva",
    "grid_center_x_dva",
    "grid_center_y_dva",
)


def _task_params(run: RunData) -> dict[str, Any]:
    params = run.config.get("task_params")
    if not isinstance(params, dict):
        raise DataError(
            f"{run.run_dir} has no task_params in its snapshot — cannot rebuild the probe grid"
        )
    return params


def probe_grid(run: RunData) -> ProbeGrid:
    """The probe grid this run actually flashed, from its own snapshot."""
    params = _task_params(run)
    missing = [key for key in _GRID_KEYS if key not in params]
    if missing:
        raise DataError(
            f"{run.run_dir} does not look like an RF-mapping run: its task_params lack "
            f"{missing} (task was {run.config.get('info', {}).get('task_name')!r})"
        )
    return ProbeGrid.from_extent(
        int(params["grid_cols"]),
        int(params["grid_rows"]),
        float(params["grid_extent_x_dva"]),
        float(params["grid_extent_y_dva"]),
        center=(float(params["grid_center_x_dva"]), float(params["grid_center_y_dva"])),
    )


def counting_window(run: RunData) -> tuple[float, float]:
    """The (start, end) counting window in seconds, as this run configured
    it — so an offline map is comparable with the live one by default. Pass
    your own window to ``map_from_spike_times`` to explore others."""
    params = _task_params(run)
    for key in ("window_start_ms", "window_end_ms"):
        if key not in params:
            raise DataError(f"{run.run_dir}'s task_params lack {key}")
    return float(params["window_start_ms"]) / 1000.0, float(params["window_end_ms"]) / 1000.0


def probe_onsets(run: RunData) -> pd.DataFrame:
    """Every flash this run showed, one row each, on the session clock.

    Columns: ``t`` (the flip that showed it), ``trial_index``, ``col``,
    ``row``, ``x_dva``, ``y_dva``, ``polarity``. Times are flip-stamped by
    the engine, so they carry the same photon honesty as everything else in
    the events table.
    """
    if run.events.empty or "event" not in run.events:
        raise DataError(f"{run.run_dir} has no events table to read probes from")
    matching = run.events.loc[run.events["event"] == PROBE_EVENT]
    if matching.empty:
        raise DataError(
            f"{run.run_dir} holds no {PROBE_EVENT} events — was this an RF-mapping run?"
        )
    payloads = event_payloads(run.events, PROBE_EVENT)
    rows = []
    for (_, event), payload in zip(matching.iterrows(), payloads, strict=True):
        rows.append(
            {
                "t": float(event["t"]),
                "trial_index": int(event["trial_index"]),
                "col": int(payload["col"]),
                "row": int(payload["row"]),
                "x_dva": float(payload["x_dva"]),
                "y_dva": float(payload["y_dva"]),
                "polarity": str(payload.get("polarity", "bright")),
            }
        )
    return pd.DataFrame(rows)


def map_from_spike_times(
    onsets: pd.DataFrame,
    *,
    spike_times_s: np.ndarray,
    spike_labels: np.ndarray,
    grid: ProbeGrid,
    window_s: tuple[float, float],
    polarity: str | None = None,
) -> tuple[RFAccumulator, list[int]]:
    """Count spikes per (label, cell) and return the filled accumulator.

    ``spike_times_s`` must already be on the SESSION clock (map recording
    times through ``AlignmentFit.to_behavior`` first — the module docstring
    shows the composition). ``spike_labels`` is any integer labelling —
    Kilosort cluster ids, channel numbers — and the returned list maps the
    accumulator's dense rows back to those labels, sorted ascending.

    ``polarity`` restricts the map to bright or dark flashes, which is how
    ON and OFF subfields are separated; None pools both.
    """
    lo, hi = window_s
    if not lo < hi:
        raise DataError(f"window_s must be (start, end) with start < end, got {window_s}")
    selected = onsets if polarity is None else onsets.loc[onsets["polarity"] == polarity]
    if selected.empty:
        raise DataError(
            "no probe onsets to map"
            + (f" with polarity {polarity!r}" if polarity is not None else "")
        )
    times = np.asarray(spike_times_s, dtype=np.float64)
    labels = np.asarray(spike_labels)
    if times.shape != labels.shape:
        raise DataError(
            f"spike_times_s {times.shape} and spike_labels {labels.shape} must be parallel"
        )
    order = np.argsort(times, kind="stable")
    times, labels = times[order], labels[order]

    unique_labels = [int(label) for label in np.unique(labels)]
    dense = {label: row for row, label in enumerate(unique_labels)}
    accumulator = RFAccumulator(grid, max(1, len(unique_labels)), hi - lo)
    for onset in selected.itertuples():
        start = int(np.searchsorted(times, onset.t + lo, side="left"))
        end = int(np.searchsorted(times, onset.t + hi, side="left"))
        counts = np.zeros(accumulator.n_channels, dtype=np.int64)
        for label in labels[start:end]:
            counts[dense[int(label)]] += 1
        accumulator.add_flash(int(onset.col), int(onset.row), counts)
    return accumulator, unique_labels
