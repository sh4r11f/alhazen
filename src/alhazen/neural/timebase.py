"""Placing a recording stream's samples on the session clock, live.

An acquisition host and the session machine each have their own crystal, so a
sample index divided by the sample rate is a time on the *recording's* clock,
not the session's. Offline, the TTL sync pulses settle this properly
(``alhazen.analysis.sync``). Live, there is a cheaper mapping that is good
enough for a response window tens of milliseconds wide, and this module is
that mapping — with its error budget stated rather than implied.

Each network fetch gives one simultaneous observation of both clocks: the
stream's newest sample index (from the fetch result) and the session clock
(read the moment the fetch returns). ``offset = t_session − end/rate`` would
be exact if the fetch were instantaneous; in reality it is late by however
long the request took, so every observation *over*-estimates the offset by
its own network latency. Taking the **minimum** over a sliding window keeps
the observation with the least latency in it — the standard estimator for
one-way-delayed clock pairs (NTP and LabStreamingLayer do the same).

What remains is honest jitter: the best fetch's residual latency (sub-ms on
a lab LAN, a few ms worst case) plus crystal drift across the window (~1e-5
relative, so ~0.3 ms over a 30 s window). Both are far inside the 50–150 ms
response windows receptive-field mapping counts spikes in, and both are far
outside what spike-timing analyses need — which is why the offline path
aligns with TTL pulses instead, and nothing here pretends otherwise.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class StreamTimebase:
    """The live sample-index → session-seconds mapping for one stream.

    ``note_fetch`` is called once per network fetch with the stream position
    and the session clock; ``to_session`` then converts sample indices. The
    window is counted in fetches, not seconds, so the estimator needs no
    clock of its own.
    """

    def __init__(self, rate_hz: float, window: int = 32) -> None:
        if rate_hz <= 0:
            raise ValueError(f"rate_hz must be > 0, got {rate_hz}")
        if window < 1:
            raise ValueError(f"window must be >= 1 fetch, got {window}")
        self._rate = float(rate_hz)
        # One offset observation per fetch; the window bounds how far back
        # the minimum may reach, which is what bounds the drift term above.
        self._offsets: deque[float] = deque(maxlen=window)
        self._last_end: int | None = None

    @property
    def rate_hz(self) -> float:
        return self._rate

    @property
    def calibrated(self) -> bool:
        """Whether at least one fetch has been noted — before that, no sample
        can be placed on the session clock at all."""
        return bool(self._offsets)

    def note_fetch(self, end_sample: int, t_session: float) -> None:
        """Record one simultaneous reading of both clocks.

        ``end_sample`` is one past the newest sample the fetch returned;
        ``t_session`` is the session clock read immediately after the fetch
        call returned. Read the clock *after*, never before: the samples
        existed by the time the reply arrived, so "after" bounds the error on
        one known side (late), which is the side the minimum removes.
        """
        if end_sample < 0:
            raise ValueError(f"end_sample must be >= 0, got {end_sample}")
        if self._last_end is not None and end_sample < self._last_end:
            # A stream position that moves backwards means the caller mixed
            # two streams or the acquisition restarted; either way every
            # previous observation is about a different timeline.
            raise ValueError(
                f"stream position went backwards ({self._last_end} -> {end_sample}); "
                f"reset the timebase when the acquisition restarts"
            )
        self._last_end = end_sample
        self._offsets.append(t_session - end_sample / self._rate)

    def offset_s(self) -> float:
        """The current best offset estimate (session = sample/rate + offset)."""
        if not self._offsets:
            raise ValueError("no fetch has been noted yet; the timebase is uncalibrated")
        return min(self._offsets)

    def to_session(self, samples: np.ndarray) -> np.ndarray:
        """Sample indices → session-clock seconds, as float64."""
        return np.asarray(samples, dtype=np.float64) / self._rate + self.offset_s()

    def sample_to_session(self, sample: int) -> float:
        """One sample index → session seconds."""
        if not self._offsets:
            raise ValueError("no fetch has been noted yet; the timebase is uncalibrated")
        return sample / self._rate + self.offset_s()
