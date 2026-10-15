"""The session's gaze, handed to the live monitor while trials run.

The engine reports every displayed frame's inputs once, right after the flip
(``TrialEngine(on_frame_input=...)``). ``GazeFeed.note`` is attached there,
beside the frame-input buffer, and turns that stream into what the Trial view
draws: the gaze in dva, at most ``max_hz`` samples a second, a gap where there
was no sample (a blink, a lost eye), and the trial each sample belongs to so
the page can start a fresh trail on every trial.

It must never cost a frame. So, per frame, it does a few comparisons and an
append; a batch leaves for the server child at most every ``flush_s`` through
``sink``, which is a non-blocking put on a one-slot queue
(``LiveMonitorController.publish_gaze``). When the child has not collected the
previous batch, the sink answers False and the samples stay here, the oldest
dropped first once ``capacity`` is reached — a stalled page loses the
beginning of a trail, never the newest point, and never holds up the session.

    engine ─flip─▶ note(trial, frame, t, inputs) ─▶ pending (bounded deque)
                                           every flush_s │ sink(batch) → True / False(full)
                                                         ▼
                                    LiveMonitorController.publish_gaze ─▶ child ─▶ /api/gaze
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable

from alhazen.core.trial import InputFrame

# One sample as it travels: (trial_index, t, x_dva, y_dva). x and y are None
# for a gap — the frame had no gaze sample — and t is then the flip's time.
GazeSample = tuple[int, float, "float | None", "float | None"]
# Where a batch goes. True when it was taken, False when the slot was full and
# the batch must wait for the next flush.
GazeSink = Callable[[list[GazeSample]], bool]

# The fastest the page is sent samples. A trail drawn at 60 Hz is smooth; a
# 500 Hz tracker read once per frame (120 Hz on the lab rig) is decimated.
MAX_HZ = 60.0
# How often a batch leaves: about thirty times a second, so the point on the
# page trails the eye by a frame or two of the browser's, not by a batch.
FLUSH_S = 1.0 / 30.0
# How many samples wait here when the child stops collecting: four seconds at
# MAX_HZ. Older ones are dropped first.
CAPACITY = 240


class GazeFeed:
    """Decimates, batches and hands on the session's gaze; see the module."""

    def __init__(
        self,
        sink: GazeSink,
        px_per_deg: float,
        *,
        max_hz: float = MAX_HZ,
        flush_s: float = FLUSH_S,
        capacity: int = CAPACITY,
    ) -> None:
        if px_per_deg <= 0:
            raise ValueError(f"px_per_deg must be greater than 0, got {px_per_deg!r}")
        if max_hz <= 0 or flush_s < 0 or capacity < 1:
            raise ValueError("max_hz must be > 0, flush_s >= 0 and capacity >= 1")
        self._sink = sink
        self._px_per_deg = float(px_per_deg)
        # A hair under the period, so a 60 Hz display feeding a 60 Hz cap is
        # not decimated to 30 Hz by frame-time jitter.
        self._min_gap_s = 0.9 / max_hz
        self._flush_s = flush_s
        self._pending: deque[GazeSample] = deque(maxlen=capacity)
        self._trial: int | None = None
        self._last_kept_t: float | None = None
        self._last_gaze_t: float | None = None
        self._last_was_gap = False
        self._last_flush_t: float | None = None
        # How many samples were dropped because the child stopped collecting;
        # the page is told nothing about it beyond the hole, so it is kept for
        # a test (and a debugger) to read.
        self.dropped = 0

    def note(self, trial_index: int, frame_index: int, t: float, inputs: InputFrame) -> None:
        """One displayed frame's inputs; the engine's ``on_frame_input``."""
        new_trial = trial_index != self._trial
        if new_trial:
            # A new trial starts a new trail. What the old trial still had
            # waiting goes out first, so its end is not lost.
            self.flush(t, force=True)
            self._trial = trial_index
            self._last_kept_t = None
            self._last_gaze_t = None
            self._last_was_gap = False
        gaze = inputs.gaze
        if gaze is not None and not (math.isfinite(gaze[0]) and math.isfinite(gaze[1])):
            # Not a position the page can draw, and not JSON: a gap.
            gaze = None
        if gaze is None:
            # A gap is sent once per run of missing samples: the page breaks
            # the trail there and hides the point.
            if not self._last_was_gap:
                self._keep((trial_index, t, None, None))
                self._last_was_gap = True
        else:
            sample_t = inputs.gaze_t if inputs.gaze_t is not None else t
            repeat = self._last_gaze_t is not None and sample_t == self._last_gaze_t
            due = (
                self._last_kept_t is None
                or self._last_was_gap
                or sample_t - self._last_kept_t >= self._min_gap_s
            )
            if not repeat and due:
                self._keep(
                    (
                        trial_index,
                        sample_t,
                        gaze[0] / self._px_per_deg,
                        gaze[1] / self._px_per_deg,
                    )
                )
                self._last_kept_t = sample_t
                self._last_was_gap = False
            self._last_gaze_t = sample_t
        self.flush(t)

    def flush(self, t: float, *, force: bool = False) -> None:
        """Hand the waiting samples on, if a flush is due (or ``force``)."""
        if not self._pending:
            return
        if not force and self._last_flush_t is not None and t - self._last_flush_t < self._flush_s:
            return
        self._last_flush_t = t
        if self._sink(list(self._pending)):
            self._pending.clear()

    def _keep(self, sample: GazeSample) -> None:
        if len(self._pending) == self._pending.maxlen:
            self.dropped += 1
        self._pending.append(sample)
