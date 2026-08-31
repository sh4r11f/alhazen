"""The probe grid and the receptive-field accumulator.

A receptive-field map, reduced to arithmetic: probes flash at the cells of a
grid, spikes are counted in a fixed window after each flash, and the map is
counts per flash per second for every (channel, cell). The same accumulator
serves the live dashboard (fed by the threshold detector during a session)
and the offline analysis (fed by sorted spikes afterwards), which is why it
lives here, below both, and knows nothing about either.

Coordinates are degrees of visual angle in the *centered* frame the rest of
the framework uses — x growing right, y growing up — and rows are indexed
bottom-to-top (row 0 is the lowest row), matching the y axis. Anything that
draws a map top-first (a screen, the dashboard) does its own flip and says
so; the math here keeps one orientation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ProbeGrid:
    """A rectangular grid of probe locations, in centered dva.

    ``spacing`` is the cell pitch; the grid's total extent is
    ``cols × spacing_x`` by ``rows × spacing_y``, centred on ``center``.
    Cell centres sit at the middle of each cell, so a 4-column grid of
    extent 8 has centres at −3, −1, +1, +3 — never on the outer edge.
    """

    cols: int
    rows: int
    center_x_dva: float
    center_y_dva: float
    spacing_x_dva: float
    spacing_y_dva: float

    def __post_init__(self) -> None:
        if self.cols < 1 or self.rows < 1:
            raise ValueError(f"grid needs at least 1x1 cells, got {self.cols}x{self.rows}")
        if self.spacing_x_dva <= 0 or self.spacing_y_dva <= 0:
            raise ValueError(
                f"grid spacing must be > 0 dva, got {self.spacing_x_dva} x {self.spacing_y_dva}"
            )

    @classmethod
    def from_extent(
        cls,
        cols: int,
        rows: int,
        extent_x_dva: float,
        extent_y_dva: float,
        center: tuple[float, float] = (0.0, 0.0),
    ) -> ProbeGrid:
        """The construction a config speaks: total extent, divided into cells."""
        if cols < 1 or rows < 1:
            raise ValueError(f"grid needs at least 1x1 cells, got {cols}x{rows}")
        return cls(
            cols=cols,
            rows=rows,
            center_x_dva=center[0],
            center_y_dva=center[1],
            spacing_x_dva=extent_x_dva / cols,
            spacing_y_dva=extent_y_dva / rows,
        )

    @property
    def n_cells(self) -> int:
        return self.cols * self.rows

    @property
    def x_centers_dva(self) -> np.ndarray:
        """Column centres, ascending (leftmost first)."""
        half = self.cols * self.spacing_x_dva / 2.0
        return self.center_x_dva - half + (np.arange(self.cols) + 0.5) * self.spacing_x_dva

    @property
    def y_centers_dva(self) -> np.ndarray:
        """Row centres, ascending — row 0 is the BOTTOM row, y grows up."""
        half = self.rows * self.spacing_y_dva / 2.0
        return self.center_y_dva - half + (np.arange(self.rows) + 0.5) * self.spacing_y_dva

    @property
    def x_edges_dva(self) -> np.ndarray:
        """Column boundaries, cols+1 of them — what a heatmap draws cells
        between."""
        half = self.cols * self.spacing_x_dva / 2.0
        return self.center_x_dva - half + np.arange(self.cols + 1) * self.spacing_x_dva

    @property
    def y_edges_dva(self) -> np.ndarray:
        half = self.rows * self.spacing_y_dva / 2.0
        return self.center_y_dva - half + np.arange(self.rows + 1) * self.spacing_y_dva

    def cell_center_dva(self, col: int, row: int) -> tuple[float, float]:
        self._check_cell(col, row)
        return float(self.x_centers_dva[col]), float(self.y_centers_dva[row])

    def _check_cell(self, col: int, row: int) -> None:
        if not (0 <= col < self.cols and 0 <= row < self.rows):
            raise ValueError(f"cell ({col}, {row}) is outside this {self.cols}x{self.rows} grid")


class RFAccumulator:
    """Spike counts per (channel, cell), and the maps derived from them.

    ``window_s`` is the length of the counting window each flash contributed,
    so ``rate_map`` can report spikes/s rather than raw counts — a number an
    experimenter can compare against a known baseline rate.
    """

    def __init__(self, grid: ProbeGrid, n_channels: int, window_s: float) -> None:
        if n_channels < 1:
            raise ValueError(f"n_channels must be >= 1, got {n_channels}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self.grid = grid
        self.n_channels = n_channels
        self.window_s = float(window_s)
        # counts[channel, row, col]; flashes[row, col]. int64 because a long
        # mapping session on 384 channels genuinely accumulates.
        self.counts = np.zeros((n_channels, grid.rows, grid.cols), dtype=np.int64)
        self.flashes = np.zeros((grid.rows, grid.cols), dtype=np.int64)

    @property
    def n_flashes(self) -> int:
        return int(self.flashes.sum())

    @property
    def n_spikes(self) -> int:
        return int(self.counts.sum())

    def add_flash(self, col: int, row: int, per_channel_counts: np.ndarray) -> None:
        """Fold one flash's counting-window spike counts into the map."""
        self.grid._check_cell(col, row)
        counts = np.asarray(per_channel_counts)
        if counts.shape != (self.n_channels,):
            raise ValueError(
                f"per_channel_counts must have shape ({self.n_channels},), got {counts.shape}"
            )
        if counts.min(initial=0) < 0:
            raise ValueError("spike counts cannot be negative")
        self.flashes[row, col] += 1
        self.counts[:, row, col] += counts.astype(np.int64)

    # ------------------------------------------------------------------
    # Maps
    # ------------------------------------------------------------------

    def rate_map(self, channel: int) -> np.ndarray:
        """One channel's map in spikes/s, ``(rows, cols)``, NaN where a cell
        has not been probed yet — an unmeasured cell must never be drawn as
        a measured zero."""
        if not 0 <= channel < self.n_channels:
            raise ValueError(f"channel {channel} is outside 0..{self.n_channels - 1}")
        return self._rates(self.counts[channel])

    def pooled_rate_map(self) -> np.ndarray:
        """All channels' spikes pooled: the population map, in spikes/s
        summed over channels. This is the map that shows *where* the probe's
        channels see the world before any single channel has enough spikes
        to say so on its own."""
        return self._rates(self.counts.sum(axis=0))

    def _rates(self, counts: np.ndarray) -> np.ndarray:
        rates = np.full(counts.shape, np.nan, dtype=np.float64)
        probed = self.flashes > 0
        rates[probed] = counts[probed] / (self.flashes[probed] * self.window_s)
        return rates

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def responsiveness(self) -> np.ndarray:
        """Per channel: peak cell rate minus median cell rate, over probed
        cells (NaN before anything is probed). The median stands in for the
        baseline, so a channel that fires everywhere ranks below one that
        fires *somewhere* — which is the channel a mapping session is
        looking for."""
        scores = np.full(self.n_channels, np.nan)
        if not (self.flashes > 0).any():
            return scores
        for channel in range(self.n_channels):
            rates = self.rate_map(channel)
            probed = rates[~np.isnan(rates)]
            scores[channel] = float(probed.max() - np.median(probed))
        return scores

    def best_channels(self, n: int) -> list[int]:
        """The ``n`` most responsive channels, most responsive first."""
        scores = self.responsiveness()
        if np.all(np.isnan(scores)):
            return []
        order = np.argsort(np.nan_to_num(scores, nan=-np.inf))[::-1]
        return [int(channel) for channel in order[: max(0, n)]]

    def peak_cell(self, channel: int) -> tuple[int, int] | None:
        """The (col, row) of the channel's highest-rate probed cell, or None
        before any cell is probed."""
        rates = self.rate_map(channel)
        if np.all(np.isnan(rates)):
            return None
        row, col = np.unravel_index(np.nanargmax(rates), rates.shape)
        return int(col), int(row)

    def centroid_dva(self, channel: int, floor_frac: float = 0.5) -> tuple[float, float] | None:
        """The rate-weighted centre of the cells above ``floor_frac`` of the
        peak — a receptive-field centre estimate that one noisy cell cannot
        drag around the way the raw argmax can. None before any probing, or
        when the map is flat at zero (there is no field to have a centre)."""
        rates = self.rate_map(channel)
        if np.all(np.isnan(rates)):
            return None
        peak = np.nanmax(rates)
        if peak <= 0:
            return None
        weights = np.where(np.isnan(rates) | (rates < floor_frac * peak), 0.0, rates)
        total = weights.sum()
        if total <= 0:
            return None
        x = (weights.sum(axis=0) * self.grid.x_centers_dva).sum() / total
        y = (weights.sum(axis=1) * self.grid.y_centers_dva).sum() / total
        return float(x), float(y)

    def coverage(self, min_flashes: int = 1) -> float:
        """The fraction of cells probed at least ``min_flashes`` times — the
        number that says how much of the planned map exists yet."""
        if min_flashes < 1:
            raise ValueError(f"min_flashes must be >= 1, got {min_flashes}")
        return float((self.flashes >= min_flashes).mean())
