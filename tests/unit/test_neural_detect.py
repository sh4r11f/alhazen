"""The threshold detector: planted spikes in, the same spikes out.

Synthetic streams with known spike times, fed in awkward chunk sizes on
purpose — chunk boundaries are where streaming detectors quietly lose or
double spikes, and every test here would catch that."""

from __future__ import annotations

import numpy as np
import pytest

from alhazen.neural.detect import SpikeDetector

RATE = 30_000.0
NOISE_SD = 30.0


def noise(n: int, channels: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.normal(0.0, NOISE_SD, size=(n, channels))).astype(np.int16)


def plant(data: np.ndarray, channel: int, at: int, amplitude: float = -1200.0) -> None:
    """A ~0.7 ms triangular negative deflection — a caricature spike far
    above threshold, so detection is about the machinery, not the SNR."""
    width = 20
    shape = np.concatenate([np.linspace(0, 1, width // 2), np.linspace(1, 0, width // 2)])
    data[at : at + width, channel] = (
        data[at : at + width, channel].astype(np.float64) + amplitude * shape
    ).astype(np.int16)


def run_chunks(detector: SpikeDetector, data: np.ndarray, sizes: list[int]) -> tuple:
    """Feed `data` split into the given chunk sizes (padded with a final
    chunk if they do not cover it) and pool the detections."""
    samples, channels = [], []
    start = 0
    for size in sizes + [len(data) - sum(sizes)]:
        if size <= 0:
            continue
        s, c = detector.process(data[start : start + size], start)
        samples.append(s)
        channels.append(c)
        start += size
    return np.concatenate(samples), np.concatenate(channels)


class TestDetection:
    def test_planted_spikes_recovered_once_each(self):
        data = noise(60_000, 2, seed=1)
        planted = [(5_000, 0), (12_345, 1), (30_001, 0), (44_444, 1), (58_000, 0)]
        for at, channel in planted:
            plant(data, channel, at)
        detector = SpikeDetector(2, RATE, threshold_sigmas=5.0, car=False)
        samples, channels = run_chunks(detector, data, [1000, 777, 12345, 20000])

        assert len(samples) == len(planted)
        ordered = channels[np.argsort(samples)]
        for (at, channel), found, on in zip(planted, np.sort(samples), ordered, strict=True):
            # The crossing lands inside the deflection, never before it.
            assert at - 1 <= found <= at + 20
            assert on == channel

    def test_spike_straddling_a_chunk_boundary_is_found_exactly_once(self):
        data = noise(20_000, 1, seed=2)
        boundary = 10_000
        plant(data, 0, boundary - 10)  # half before, half after the cut
        detector = SpikeDetector(1, RATE, threshold_sigmas=5.0, car=False)
        samples, _ = run_chunks(detector, data, [boundary])
        assert len(samples) == 1
        assert boundary - 11 <= samples[0] <= boundary + 10

    def test_refractory_merges_recrossings_of_one_waveform(self):
        data = noise(20_000, 1, seed=3)
        # Two deflections 0.5 ms apart: inside a 1 ms dead time they are one
        # event; a detector without the rule reports two.
        plant(data, 0, 9_000)
        plant(data, 0, 9_000 + 15)
        detector = SpikeDetector(1, RATE, threshold_sigmas=5.0, refractory_ms=1.0, car=False)
        samples, _ = run_chunks(detector, data, [7_000])
        assert len(samples) == 1

    def test_common_average_reference_removes_shared_artifacts(self):
        # A -600 step on every channel at once — a lick artifact. With CAR
        # the median tracks it out; without, every channel "spikes". Sixteen
        # channels: the floor CAR is honest over (detect.CAR_MIN_CHANNELS).
        artifact = noise(30_000, 16, seed=4)
        artifact[15_000:15_040, :] = (artifact[15_000:15_040, :].astype(float) - 600).astype(
            np.int16
        )
        with_car = SpikeDetector(16, RATE, threshold_sigmas=5.0, car=True)
        samples, _ = run_chunks(with_car, artifact, [8_000, 9_000])
        assert len(samples) == 0

        without_car = SpikeDetector(16, RATE, threshold_sigmas=5.0, car=False)
        samples, channels = run_chunks(without_car, artifact, [8_000, 9_000])
        assert set(channels.tolist()) == set(range(16))

    def test_car_below_the_channel_floor_is_refused(self):
        # A biased noise estimate silently lowers the threshold; the
        # detector refuses the configuration instead.
        with pytest.raises(ValueError, match="car=True needs at least"):
            SpikeDetector(4, RATE, car=True)

    def test_pure_noise_produces_no_detections_at_this_threshold(self):
        detector = SpikeDetector(2, RATE, threshold_sigmas=5.5, car=False)
        samples, _ = run_chunks(detector, noise(60_000, 2, seed=5), [10_000])
        assert len(samples) == 0

    def test_sigma_estimate_tracks_the_noise_scale(self):
        detector = SpikeDetector(1, RATE, car=False)
        detector.process(noise(30_000, 1, seed=6), 0)
        sigma = detector.sigma
        assert sigma is not None
        # The high-pass removes some low-frequency power, so the filtered
        # sigma sits at or slightly below the raw noise SD.
        assert 0.7 * NOISE_SD < sigma[0] < 1.2 * NOISE_SD


class TestStreamDiscipline:
    def test_discontinuous_chunk_is_refused(self):
        detector = SpikeDetector(1, RATE, car=False)
        detector.process(noise(1000, 1), 0)
        with pytest.raises(ValueError, match="reset"):
            detector.process(noise(1000, 1), 5000)

    def test_reset_accepts_a_new_start(self):
        detector = SpikeDetector(1, RATE, car=False)
        detector.process(noise(1000, 1), 0)
        detector.reset()
        detector.process(noise(1000, 1), 5000)  # no raise

    def test_emitted_until_trails_the_stream_by_the_window_half(self):
        detector = SpikeDetector(1, RATE, hp_window_ms=5.0, car=False)
        detector.process(noise(10_000, 1), 0)
        assert detector.emitted_until == 10_000 - 1 - detector.delay_samples

    def test_wrong_channel_count_is_refused(self):
        detector = SpikeDetector(3, RATE, car=False)
        with pytest.raises(ValueError, match="n_samples, 3"):
            detector.process(noise(100, 2), 0)

    def test_tiny_chunks_accumulate_until_a_window_fits(self):
        data = noise(2_000, 1, seed=7)
        plant(data, 0, 700)
        detector = SpikeDetector(1, RATE, threshold_sigmas=5.0, car=False)
        # 30-sample chunks are far below the 151-sample window; the detector
        # must buffer, never crash, and still find the spike.
        samples, _ = run_chunks(detector, data, [30] * 60)
        assert len(samples) == 1

    def test_construction_validates_loudly(self):
        with pytest.raises(ValueError, match="n_channels"):
            SpikeDetector(0, RATE)
        with pytest.raises(ValueError, match="threshold_sigmas"):
            SpikeDetector(1, RATE, threshold_sigmas=0)
        with pytest.raises(ValueError, match="hp_window_ms"):
            SpikeDetector(1, RATE, hp_window_ms=-1)
