"""GazeNoise: the eye's own movement and the tracker's noise, for simulated subjects.

Every test seeds its own generator, so each one is exact on every run; the
statistical ones sample long enough that the tolerance is several standard
errors wide.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from alhazen.modes.gaze_noise import GazeNoise


def noise(seed: int = 0, **settings: float) -> GazeNoise:
    return GazeNoise(np.random.default_rng(seed), **settings)


def trace(source: GazeNoise, hz: float, seconds: float) -> np.ndarray:
    """The offsets sampled at ``hz`` for ``seconds``: an (n, 2) array, degrees."""
    times = np.arange(0.0, seconds, 1.0 / hz)
    return np.array([source.offset_dva(float(t)) for t in times])


# The pieces one at a time: only the drift, only the tracker, only the jumps.
DRIFT_ONLY = {"tracker_sd_dva": 0.0, "microsaccade_rate_hz": 0.0}
TRACKER_ONLY = {"drift_sd_dva": 0.0, "microsaccade_rate_hz": 0.0}
JUMPS_ONLY = {"tracker_sd_dva": 0.0, "drift_sd_dva": 0.0}


class TestAFixatingEye:
    def test_it_scatters_by_a_fraction_of_a_degree_and_never_leaves_a_fixation_window(self):
        # The requirement in one line: a simulated eye that is fixating
        # moves the way a real one does, a tenth of a degree or two, and
        # stays well inside any fixation window a task would draw (a degree
        # at the tightest).
        offsets = trace(noise(seed=1), hz=120.0, seconds=120.0)

        assert 0.1 < offsets[:, 0].std() < 0.35
        assert 0.1 < offsets[:, 1].std() < 0.35
        assert np.hypot(offsets[:, 0], offsets[:, 1]).max() < 1.0

    def test_it_wanders_around_the_aim_rather_than_away_from_it(self):
        offsets = trace(noise(seed=2), hz=60.0, seconds=300.0)

        assert offsets.mean(axis=0) == pytest.approx((0.0, 0.0), abs=0.05)

    def test_consecutive_samples_are_close_the_eye_does_not_teleport(self):
        # At 120 Hz the eye moves a little between frames — drift and the
        # tracker's noise — and only a microsaccade moves it further.
        offsets = trace(noise(seed=3), hz=120.0, seconds=60.0)
        steps = np.hypot(*np.diff(offsets, axis=0).T)

        assert np.median(steps) < 0.05
        assert steps.max() < 1.5


class TestTheDrift:
    def test_its_spread_is_the_setting(self):
        offsets = trace(noise(seed=4, drift_sd_dva=0.2, **DRIFT_ONLY), hz=10.0, seconds=3000.0)

        assert offsets.std(axis=0) == pytest.approx((0.2, 0.2), rel=0.1)

    @pytest.mark.parametrize("hz", [60.0, 240.0])
    def test_it_is_the_same_at_any_frame_rate(self, hz):
        # The exact step is what makes this hold: the spread, and how fast
        # the eye forgets where it was (correlation e**-1 one time constant
        # later), do not depend on how often it is asked.
        tau = 0.5
        offsets = trace(
            noise(seed=5, drift_sd_dva=0.2, drift_tau_s=tau, **DRIFT_ONLY), hz=hz, seconds=600.0
        )
        lag = round(tau * hz)
        x = offsets[:, 0]
        correlation = np.corrcoef(x[:-lag], x[lag:])[0, 1]

        assert x.std() == pytest.approx(0.2, rel=0.1)
        assert correlation == pytest.approx(math.exp(-1.0), abs=0.07)


class TestTheTracker:
    def test_its_noise_is_white_and_the_size_of_the_setting(self):
        offsets = trace(noise(seed=6, tracker_sd_dva=0.03, **TRACKER_ONLY), hz=120.0, seconds=60.0)
        x = offsets[:, 0]

        assert x.std() == pytest.approx(0.03, rel=0.05)
        # White: a sample says nothing about the next one.
        assert abs(np.corrcoef(x[:-1], x[1:])[0, 1]) < 0.05


class TestMicrosaccades:
    def test_they_come_at_the_set_rate_and_the_set_size(self):
        rate, size = 2.0, 0.3
        offsets = trace(
            noise(seed=7, microsaccade_rate_hz=rate, microsaccade_dva=size, **JUMPS_ONLY),
            hz=500.0,
            seconds=200.0,
        )
        # With no drift and no tracker noise, nothing moves the eye between
        # samples but a jump (and the pull back toward the aim, which over
        # 2 ms is a fraction of a percent of the offset).
        jumps = np.hypot(*np.diff(offsets, axis=0).T)
        jumps = jumps[jumps > 0.02]

        assert len(jumps) == pytest.approx(rate * 200.0, rel=0.12)
        assert np.median(jumps) == pytest.approx(size, rel=0.15)

    def test_they_are_aimed_back_toward_the_aim(self):
        # Aimed, not landed: a jump the typical size, from an eye only a
        # little off the point, overshoots it — as real ones do. What the
        # model promises is the direction: back toward the fixated point,
        # strayed by a 45-degree spread, so nearly all point the home half.
        offsets = trace(
            noise(seed=8, microsaccade_rate_hz=2.0, **{**JUMPS_ONLY, "drift_sd_dva": 0.2}),
            hz=500.0,
            seconds=200.0,
        )
        before, after = offsets[:-1], offsets[1:]
        jump = after - before
        moved = np.hypot(*jump.T) > 0.1
        # Positive when the jump points toward the aim, i.e. against the offset.
        homeward = np.einsum("ij,ij->i", jump[moved], -before[moved])

        assert moved.sum() > 300
        assert (homeward > 0).mean() > 0.9


class TestItIsAnHonestTrajectory:
    def test_a_seed_replays_exactly(self):
        assert np.array_equal(
            trace(noise(seed=9), hz=60.0, seconds=5.0), trace(noise(seed=9), hz=60.0, seconds=5.0)
        )

    def test_another_seed_is_another_eye(self):
        assert not np.array_equal(
            trace(noise(seed=9), hz=60.0, seconds=5.0), trace(noise(seed=10), hz=60.0, seconds=5.0)
        )

    def test_the_same_instant_asked_twice_is_the_same_sample(self):
        source = noise(seed=11)
        source.offset_dva(1.0)

        assert source.offset_dva(1.5) == source.offset_dva(1.5)

    def test_time_going_backwards_is_refused(self):
        source = noise(seed=12)
        source.offset_dva(2.0)

        with pytest.raises(ValueError, match="backwards"):
            source.offset_dva(1.0)

    def test_a_long_gap_is_crossed_at_once(self):
        # A break between blocks, or a whole session's worth of idle clock:
        # the eye has forgotten where it was, and stepping through every
        # microsaccade of the gap would take as long as the gap.
        source = noise(seed=13)
        source.offset_dva(0.0)
        started = time.perf_counter()
        offset = source.offset_dva(1.0e6)

        assert time.perf_counter() - started < 0.5
        assert all(math.isfinite(v) and abs(v) < 2.0 for v in offset)

    def test_no_noise_at_all_is_a_still_eye(self):
        still = noise(
            seed=14,
            tracker_sd_dva=0.0,
            drift_sd_dva=0.0,
            microsaccade_rate_hz=0.0,
            microsaccade_dva=0.0,
        )

        assert np.all(trace(still, hz=60.0, seconds=2.0) == 0.0)

    def test_it_describes_its_settings_for_the_snapshot(self):
        described = noise(drift_sd_dva=0.25).describe()

        assert described == {
            "tracker_sd_dva": 0.02,
            "drift_sd_dva": 0.25,
            "drift_tau_s": 0.5,
            "microsaccade_rate_hz": 1.0,
            "microsaccade_dva": 0.3,
        }


class TestSettingsAreChecked:
    @pytest.mark.parametrize(
        "name",
        ["tracker_sd_dva", "drift_sd_dva", "microsaccade_rate_hz", "microsaccade_dva"],
    )
    @pytest.mark.parametrize("bad", [-0.1, math.nan, math.inf])
    def test_a_size_that_is_not_a_finite_non_negative_number_is_refused(self, name, bad):
        with pytest.raises(ValueError, match=name):
            noise(**{name: bad})

    @pytest.mark.parametrize("bad", [0.0, -1.0, math.nan])
    def test_the_time_constant_must_be_positive(self, bad):
        with pytest.raises(ValueError, match="drift_tau_s"):
            noise(drift_tau_s=bad)
