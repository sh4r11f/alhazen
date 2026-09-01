"""The probe grid, the accumulator, and the live timebase — arithmetic with
known answers in, so each test can say what must come out."""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.neural.rfmap import ProbeGrid, RFAccumulator
from alhazen.neural.timebase import StreamTimebase


class TestProbeGrid:
    def test_from_extent_centres_and_edges(self):
        grid = ProbeGrid.from_extent(4, 2, 8.0, 4.0, center=(1.0, -1.0))
        assert grid.spacing_x_dva == 2.0
        assert grid.spacing_y_dva == 2.0
        # Centres sit mid-cell: extent 8 around x=1 spans -3..5.
        assert grid.x_centers_dva.tolist() == [-2.0, 0.0, 2.0, 4.0]
        assert grid.y_centers_dva.tolist() == [-2.0, 0.0]  # row 0 is the bottom
        assert grid.x_edges_dva.tolist() == [-3.0, -1.0, 1.0, 3.0, 5.0]
        assert grid.cell_center_dva(0, 1) == (-2.0, 0.0)

    def test_bad_cells_and_bad_shapes_are_refused(self):
        grid = ProbeGrid.from_extent(2, 2, 4.0, 4.0)
        with pytest.raises(ValueError, match="outside"):
            grid.cell_center_dva(2, 0)
        with pytest.raises(ValueError, match="at least 1x1"):
            ProbeGrid.from_extent(0, 2, 4.0, 4.0)
        with pytest.raises(ValueError, match="spacing"):
            ProbeGrid(
                cols=2, rows=2, center_x_dva=0, center_y_dva=0, spacing_x_dva=0.0, spacing_y_dva=1.0
            )


class TestRFAccumulator:
    def grid(self) -> ProbeGrid:
        return ProbeGrid.from_extent(3, 3, 6.0, 6.0)

    def test_rates_are_counts_per_flash_per_second(self):
        acc = RFAccumulator(self.grid(), n_channels=2, window_s=0.1)
        acc.add_flash(1, 2, np.array([3, 0]))
        acc.add_flash(1, 2, np.array([1, 0]))
        rates = acc.rate_map(0)
        # 4 spikes over 2 flashes of 0.1 s = 20 spikes/s.
        assert rates[2, 1] == pytest.approx(20.0)
        # An unprobed cell is NaN — unknown, never a measured zero.
        assert np.isnan(rates[0, 0])
        assert acc.rate_map(1)[2, 1] == 0.0
        assert acc.n_flashes == 2 and acc.n_spikes == 4

    def test_pooled_map_sums_channels(self):
        acc = RFAccumulator(self.grid(), n_channels=3, window_s=0.5)
        acc.add_flash(0, 0, np.array([1, 2, 3]))
        assert acc.pooled_rate_map()[0, 0] == pytest.approx(12.0)  # 6 / (1 * 0.5)

    def test_best_channels_ranks_by_peakedness_not_total(self):
        acc = RFAccumulator(self.grid(), n_channels=2, window_s=0.1)
        # Channel 0 fires everywhere (high total, flat); channel 1 fires in
        # one cell only. The mapping session is looking for channel 1.
        for row in range(3):
            for col in range(3):
                acc.add_flash(col, row, np.array([5, 4 if (col, row) == (2, 2) else 0]))
        assert acc.best_channels(1) == [1]

    def test_peak_and_centroid_find_a_planted_field(self):
        acc = RFAccumulator(self.grid(), n_channels=1, window_s=0.1)
        strengths = {(1, 1): 10, (2, 1): 5}  # peak at centre cell, shoulder right
        for row in range(3):
            for col in range(3):
                acc.add_flash(col, row, np.array([strengths.get((col, row), 0)]))
        assert acc.peak_cell(0) == (1, 1)
        centroid = acc.centroid_dva(0)
        assert centroid is not None
        x, y = centroid
        assert 0.0 < x < 2.0  # pulled toward the shoulder, inside the pair
        assert y == pytest.approx(0.0)

    def test_empty_and_silent_maps_have_no_centre(self):
        acc = RFAccumulator(self.grid(), n_channels=1, window_s=0.1)
        assert acc.peak_cell(0) is None
        assert acc.centroid_dva(0) is None
        assert acc.best_channels(2) == []
        acc.add_flash(0, 0, np.array([0]))
        # Probed but silent: a flat zero map has no field to have a centre.
        assert acc.centroid_dva(0) is None

    def test_coverage_counts_min_flashes(self):
        acc = RFAccumulator(self.grid(), n_channels=1, window_s=0.1)
        acc.add_flash(0, 0, np.array([1]))
        acc.add_flash(0, 0, np.array([1]))
        acc.add_flash(1, 0, np.array([0]))
        assert acc.coverage() == pytest.approx(2 / 9)
        assert acc.coverage(min_flashes=2) == pytest.approx(1 / 9)

    def test_shape_errors_are_loud(self):
        acc = RFAccumulator(self.grid(), n_channels=2, window_s=0.1)
        with pytest.raises(ValueError, match="shape"):
            acc.add_flash(0, 0, np.array([1, 2, 3]))
        with pytest.raises(ValueError, match="negative"):
            acc.add_flash(0, 0, np.array([1, -2]))
        with pytest.raises(ValueError, match="outside"):
            acc.add_flash(5, 0, np.array([1, 2]))
        with pytest.raises(ValueError, match="channel"):
            acc.rate_map(7)


class TestStreamTimebase:
    RATE = 30_000.0

    def test_minimum_offset_wins_over_fetch_latency(self):
        # The stream's true offset is 5 s; each observation is late by its
        # own network latency, so the minimum recovers the least-late one.
        timebase = StreamTimebase(self.RATE)
        latencies = [0.008, 0.002, 0.011, 0.004]
        for i, latency in enumerate(latencies):
            end = (i + 1) * 3000
            timebase.note_fetch(end, 5.0 + end / self.RATE + latency)
        assert timebase.offset_s() == pytest.approx(5.002)
        assert timebase.sample_to_session(30_000) == pytest.approx(6.002)
        np.testing.assert_allclose(timebase.to_session(np.array([0, 15_000])), [5.002, 5.502])

    def test_window_forgets_old_observations(self):
        # A drifting pair of clocks must not be held to an offset measured
        # minutes ago: the window bounds how far back the minimum reaches.
        timebase = StreamTimebase(self.RATE, window=2)
        timebase.note_fetch(1000, 1000 / self.RATE + 5.000)
        timebase.note_fetch(2000, 2000 / self.RATE + 5.010)
        timebase.note_fetch(3000, 3000 / self.RATE + 5.012)
        assert timebase.offset_s() == pytest.approx(5.010)

    def test_uncalibrated_and_backwards_are_loud(self):
        timebase = StreamTimebase(self.RATE)
        assert not timebase.calibrated
        with pytest.raises(ValueError, match="uncalibrated"):
            timebase.offset_s()
        timebase.note_fetch(5000, 1.0)
        with pytest.raises(ValueError, match="backwards"):
            timebase.note_fetch(4000, 1.1)
        with pytest.raises(ValueError, match="rate_hz"):
            StreamTimebase(0.0)
