"""Threshold-crossing spike detection over a chunked int16 stream.

This is deliberately the *quick-look* detector — the one that answers "is
this channel responding, and to which probe" while the subject is still in
the chair. It is not a spike sorter and does not try to be: the raw stream
is being recorded in full by the acquisition host, and anything that needs
identified units runs offline over that recording (Kilosort, read back by
``alhazen.analysis.io.kilosort``).

The pipeline per chunk, all numpy and all stateful across chunk boundaries:

1. **Common-average reference** (optional): subtract the per-sample median
   across channels. Movement and licking artifacts arrive on every channel
   at once; the median removes them without a spike on one channel (a large
   value on 1 of N channels) dragging the reference along.
2. **High-pass by moving-average subtraction**: ``y[t] = x[t] − mean(x[t−h …
   t+h])``. A boxcar this short is a crude filter, but the only job here is
   to hold the LFP still under a fixed threshold, and a 5 ms window (~90 Hz
   equivalent cutoff) does that while leaving a ~1 ms spike mostly intact.
   It is also the only high-pass that is exactly vectorizable over a chunk
   with plain cumsum — no scipy in the live path, by design.
3. **Robust noise estimate**: ``σ = median(|y|) / 0.6745`` per channel — the
   standard MAD estimator (Quiroga 2004), robust to the spikes themselves —
   smoothed across chunks with an EMA so one artifact-heavy chunk cannot
   swing the threshold.
4. **Negative threshold crossings**: a spike is the first sample below
   ``−k·σ`` after a sample at or above it, with a refractory dead time so a
   wide waveform is one event, not five.

Chunk boundaries are where streaming detectors quietly lose spikes, so the
state is explicit: the filter carries the previous ``w−1`` raw samples (the
centred window needs future samples, so detections trail the stream by
``w//2`` samples), the crossing test carries the previous filtered sample,
and the refractory rule carries the last spike per channel. A spike whose
waveform straddles two fetches is detected exactly once.
"""

from __future__ import annotations

import numpy as np

# MAD → standard deviation for a normal distribution: median(|x|) = 0.6745 σ.
_MAD_TO_SIGMA = 0.6745

# The fewest channels the common-average reference is honest over. Each
# channel participates in the median it is referenced against, which
# concentrates the residual's centre and biases the MAD noise estimate LOW —
# so the threshold quietly drops and false positives climb. The bias shrinks
# like 1/n and is negligible from a few tens of channels up (measured: ~20%
# at 4 channels, ~2% at 16); below this floor CAR is refused loudly rather
# than applied wrongly.
CAR_MIN_CHANNELS = 16


class SpikeDetector:
    """Stateful chunk-by-chunk threshold detection for one stream.

    ``process`` consumes strictly consecutive chunks (the caller states each
    chunk's absolute start index, and a mismatch is a loud error — a dropped
    network fetch must be handled by ``reset``, never papered over) and
    returns the absolute sample index and channel of each detected spike.
    """

    def __init__(
        self,
        n_channels: int,
        rate_hz: float,
        *,
        threshold_sigmas: float = 4.5,
        hp_window_ms: float = 5.0,
        refractory_ms: float = 1.0,
        car: bool = True,
        sigma_smoothing: float = 0.2,
    ) -> None:
        if n_channels < 1:
            raise ValueError(f"n_channels must be >= 1, got {n_channels}")
        if rate_hz <= 0:
            raise ValueError(f"rate_hz must be > 0, got {rate_hz}")
        if threshold_sigmas <= 0:
            raise ValueError(f"threshold_sigmas must be > 0, got {threshold_sigmas}")
        if hp_window_ms <= 0:
            raise ValueError(f"hp_window_ms must be > 0, got {hp_window_ms}")
        if refractory_ms < 0:
            raise ValueError(f"refractory_ms must be >= 0, got {refractory_ms}")
        if not 0 < sigma_smoothing <= 1:
            raise ValueError(f"sigma_smoothing must be in (0, 1], got {sigma_smoothing}")
        if car and n_channels < CAR_MIN_CHANNELS:
            raise ValueError(
                f"car=True needs at least {CAR_MIN_CHANNELS} channels to leave the noise "
                f"estimate unbiased (got {n_channels}); monitor more channels or set car off"
            )

        self._n_channels = n_channels
        self._rate = float(rate_hz)
        self._k = float(threshold_sigmas)
        # The boxcar length in samples, forced odd so it has a centre sample
        # and the filtered stream is not shifted by half a sample.
        w = int(round(hp_window_ms / 1000.0 * rate_hz))
        self._w = max(3, w | 1)
        self._half = self._w // 2
        self._refractory = max(1, int(round(refractory_ms / 1000.0 * rate_hz)))
        self._car = bool(car)
        self._alpha = float(sigma_smoothing)

        # --- state carried between chunks -----------------------------
        # Raw (post-CAR) samples the centred window still needs: the last
        # w−1 rows of everything consumed so far.
        self._carry: np.ndarray | None = None
        # Absolute index of carry[0]; only meaningful when carry is not None.
        self._carry_start = 0
        # The last *emitted* filtered row, so a crossing whose "before"
        # sample was in the previous chunk is still seen.
        self._last_filtered: np.ndarray | None = None
        # Last spike per channel, for the refractory rule across chunks.
        self._last_spike = np.full(n_channels, -(2**62), dtype=np.int64)
        # EMA of the per-channel noise sigma, in the filtered signal's units.
        self._sigma: np.ndarray | None = None
        # The absolute index the next chunk must start at (continuity check).
        self._next_expected: int | None = None
        # The newest absolute index whose detection verdict is final —
        # everything at or before this has been emitted (or never will be).
        self._emitted_until: int = -1

    # ------------------------------------------------------------------

    @property
    def n_channels(self) -> int:
        return self._n_channels

    @property
    def delay_samples(self) -> int:
        """How far detections trail the raw stream: the centred window needs
        this many future samples before a sample's verdict is final."""
        return self._half

    @property
    def emitted_until(self) -> int:
        """Absolute sample index up to which detections are complete. A
        consumer counting spikes in a window must wait until this passes the
        window's end, or the newest spikes would be silently missing."""
        return self._emitted_until

    @property
    def sigma(self) -> np.ndarray | None:
        """Per-channel noise estimate (filtered units), or None before the
        first processed block. A copy: state is not for editing."""
        return None if self._sigma is None else self._sigma.copy()

    def reset(self) -> None:
        """Forget filter continuity after a gap in the stream (a lost fetch,
        an acquisition restart). The noise estimate is kept — the electrode
        did not change because the network hiccuped — but the carried
        samples and the crossing/refractory memory are about samples that no
        longer neighbour the next chunk."""
        self._carry = None
        self._last_filtered = None
        self._next_expected = None

    # ------------------------------------------------------------------

    def process(self, chunk: np.ndarray, start_sample: int) -> tuple[np.ndarray, np.ndarray]:
        """Consume one chunk of ``(n_samples, n_channels)`` int16 samples.

        Returns ``(samples, channels)``: the absolute sample index (int64)
        and channel (int32) of each spike whose verdict became final with
        this chunk, in time order.
        """
        data = np.asarray(chunk)
        if data.ndim != 2 or data.shape[1] != self._n_channels:
            raise ValueError(
                f"chunk must be (n_samples, {self._n_channels}), got shape {data.shape}"
            )
        if self._next_expected is not None and start_sample != self._next_expected:
            raise ValueError(
                f"chunk starts at sample {start_sample} but {self._next_expected} was expected; "
                f"call reset() after a stream gap instead of feeding a discontinuous chunk"
            )
        n = data.shape[0]
        self._next_expected = start_sample + n
        if n == 0:
            return _empty()

        x = data.astype(np.float32, copy=True)
        if self._car:
            # Median, not mean: one channel's spike must not drag the
            # reference it is measured against.
            x -= np.median(x, axis=1, keepdims=True)

        # Prepend the carried tail so the centred window is continuous
        # across the fetch boundary.
        if self._carry is not None:
            block = np.concatenate([self._carry, x], axis=0)
            base = self._carry_start
        else:
            block = x
            base = start_sample

        if block.shape[0] < self._w:
            # Not enough samples yet for a single centred window; keep
            # everything and wait for the next chunk.
            self._carry = block
            self._carry_start = base
            return _empty()

        # Centred moving-average subtraction via cumulative sums:
        # sums[j] = block[j .. j+w-1].sum(), so the window centred on
        # block[j+half] is sums[j], and filtered[j] belongs to absolute
        # sample index base + half + j.
        padded = np.concatenate(
            [np.zeros((1, self._n_channels), dtype=np.float64), np.cumsum(block, axis=0)]
        )
        sums = padded[self._w :] - padded[: -self._w]
        centred = block[self._half : block.shape[0] - self._half]
        filtered = centred - (sums / self._w).astype(np.float32)
        first_center = base + self._half

        # Noise estimate over this block, EMA-smoothed. Updated before the
        # threshold is applied, so the very first block already detects
        # against a measured sigma rather than a guess.
        block_sigma = np.median(np.abs(filtered), axis=0).astype(np.float64) / _MAD_TO_SIGMA
        if self._sigma is None:
            self._sigma = block_sigma
        else:
            self._sigma = (1.0 - self._alpha) * self._sigma + self._alpha * block_sigma

        threshold = -(self._k * self._sigma).astype(np.float32)

        # A crossing is below-threshold now, at-or-above one sample ago; the
        # "one sample ago" for the first filtered row is the previous
        # chunk's last emitted row, carried exactly for this comparison.
        if self._last_filtered is not None:
            before = np.concatenate([self._last_filtered, filtered[:-1]], axis=0)
            candidates = (filtered < threshold) & (before >= threshold)
            candidate_offset = 0
        else:
            candidates = (filtered[1:] < threshold) & (filtered[:-1] >= threshold)
            candidate_offset = 1

        rows, channels = np.nonzero(candidates)
        samples = (first_center + candidate_offset + rows).astype(np.int64)

        keep_samples, keep_channels = self._apply_refractory(samples, channels.astype(np.int32))

        # --- roll the state forward ----------------------------------
        self._carry = block[-(self._w - 1) :].copy()
        self._carry_start = base + block.shape[0] - (self._w - 1)
        self._last_filtered = filtered[-1:].copy()
        self._emitted_until = first_center + filtered.shape[0] - 1
        return keep_samples, keep_channels

    # ------------------------------------------------------------------

    def _apply_refractory(
        self, samples: np.ndarray, channels: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Keep the first crossing of each waveform and drop re-crossings
        inside the dead time — per channel, remembering across chunks.

        A Python loop, on purpose: crossings are sparse (tens per chunk, not
        thousands), and the per-channel "last accepted" dependency chain is
        exactly what vectorization cannot express.
        """
        if samples.size == 0:
            return _empty()
        keep = np.zeros(samples.size, dtype=bool)
        # nonzero() returns row-major order, so samples is already
        # time-ordered; ties on the same sample across channels are fine.
        for i in range(samples.size):
            channel = channels[i]
            if samples[i] - self._last_spike[channel] >= self._refractory:
                keep[i] = True
                self._last_spike[channel] = samples[i]
        return samples[keep], channels[keep]


def _empty() -> tuple[np.ndarray, np.ndarray]:
    return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int32)
