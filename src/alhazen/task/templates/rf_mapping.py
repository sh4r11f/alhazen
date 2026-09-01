"""Receptive-field mapping: flashed probes on a grid, mapped live.

The procedure every physiology rig runs before the experiment proper: the
subject holds fixation while small bright/dark squares flash one at a time
at random cells of a grid, spikes are counted in a fixed window after each
flash, and counts-per-cell become a receptive-field map — per channel,
while the probe is still in the brain and still movable.

One base task and four presets, differing only in their parameter defaults
(grid extent, probe size, timing, counting window), scaled to the receptive
fields of the area being mapped:

===========  ============  ==========  ===========  ================
task         grid          probe       flash / isi  counting window
===========  ============  ==========  ===========  ================
rf-map-v1    16x16 · 8°    0.5°        100/100 ms   30–100 ms
rf-map-v2    12x12 · 12°   1.0°        100/100 ms   40–110 ms
rf-map-v4    10x10 · 14°   1.75°       150/100 ms   50–150 ms
rf-map-mt    10x10 · 20°   3.0°        100/100 ms   30–120 ms
===========  ============  ==========  ===========  ================

Every number is a *starting point*, the way the scaffold's monitor numbers
are: recording sites live at particular eccentricities, and the grid should
be re-centred and re-sized for yours (``grid_center_x_dva`` and friends in
a params YAML). The presets encode the relative scale across areas, which
is the part worth shipping.

**Scheduling is per probe, not per trial.** A trial is one fixation hold
carrying ``probes_per_trial`` flashes; the unit of completion is the flash.
Probes shown before a fixation break keep their data (the flash happened
and the spikes are real); the unshown remainder goes back in the queue —
so a subject that breaks often costs time, never coverage.

**The live map** (``LiveRFMap``) is this task's ``live_analysis``: it notes
each ``PROBE_ON`` off the bus, drains the rig's spike source between
trials, counts spikes in the window after each flash, and hands the
dashboard finished heat-map panels. On a rig with ``spikes: {backend:
simulated}`` the whole loop runs against ground-truth receptive fields —
which is also how it is tested: known field in, same field out.

The probe log is recorded three ways, deliberately redundant: each flash is
a ``PROBE_ON`` event (flip-stamped, with cell and position in the payload —
the record offline analysis uses), each trial's row carries the same log as
JSON plus counts, and the live map's final state lands in the run directory
as ``rf_live_maps.npz``.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, model_validator

from alhazen.config.models import Duration, Model, RewardPulses
from alhazen.core.events import EventSchema
from alhazen.core.trial import CircleRegion, Outcome, PhaseAction, TrialContext, outcomes
from alhazen.dashboard.spec import DashboardPanel, DashboardSpec
from alhazen.devices.spikes import SpikeSource
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError
from alhazen.neural.rfmap import ProbeGrid, RFAccumulator
from alhazen.paradigms.base import Condition, TrialSource
from alhazen.stimuli.base import Stimulus
from alhazen.stimuli.fixation import make_fixation
from alhazen.task import phases
from alhazen.task.live import LiveWiring
from alhazen.task.plan import TrialPlan, TrialSetup
from alhazen.task.reward_policy import RewardPolicy
from alhazen.task.task import Task

log = logging.getLogger(__name__)

# The one event the whole pipeline pivots on: emitted on the flip that
# showed each probe, payload carrying the cell and its position in dva.
# The simulated spike source and the live map both key on this name.
PROBE_EVENT = "PROBE_ON"

# How long on_trial will wait for the spike stream to cover the last
# flash's counting window before carrying the probes over to the next
# trial. Bounded: a live map must never stall the session it decorates.
_COVERAGE_WAIT_S = 1.5


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------


class RFMapParams(Model):
    """Everything an RF-mapping session varies, one YAML away.

    The four presets below are subclasses that change only defaults, so a
    downstream experiment can start from the right scale and still override
    any field per rig or per session.
    """

    # -- the grid ------------------------------------------------------
    grid_cols: int = 16
    grid_rows: int = 16
    # Total extent, in dva; cell pitch is extent / cols (rows). Centre the
    # grid over the recorded hemifield — the default (0, 0) is only right
    # for a foveal site.
    grid_extent_x_dva: float = 8.0
    grid_extent_y_dva: float = 8.0
    grid_center_x_dva: float = 0.0
    grid_center_y_dva: float = 0.0

    # -- the probe -----------------------------------------------------
    probe_size_dva: float = 0.5
    # "both" interleaves bright and dark probes (half the repetitions
    # each), the classic sparse-noise arrangement; the payload and the
    # trial log carry each flash's polarity so ON and OFF maps can be
    # split offline.
    probe_polarity: Literal["bright", "dark", "both"] = "both"

    # -- timing --------------------------------------------------------
    flash: Duration = Duration(ms=100)
    isi: Duration = Duration(ms=100)
    probes_per_trial: int = 12
    n_reps_per_cell: int = 3

    # -- spike counting ------------------------------------------------
    # The window after flash onset that counts toward the map: past the
    # area's response latency, closed before the response to the NEXT
    # flash could arrive.
    window_start_ms: float = 30.0
    window_end_ms: float = 100.0

    # -- fixation ------------------------------------------------------
    fix_size_dva: float = 0.3
    fix_window_dva: float = 2.0
    acquire_timeout: Duration = Duration(ms=2000)
    initial_hold: Duration = Duration(ms=300)

    # -- the live display ----------------------------------------------
    # How many single-channel maps the dashboard shows beside the pooled
    # one. None for map_channels means "the most responsive ones, updated
    # as the session runs"; explicit hardware channel ids pin the choice.
    n_display_maps: int = 6
    map_channels: tuple[int, ...] | None = None

    iti: Duration = Duration(ms=400)

    @model_validator(mode="after")
    def _valid(self) -> RFMapParams:
        if self.grid_cols < 1 or self.grid_rows < 1:
            raise ValueError("the grid needs at least 1x1 cells")
        for name in ("grid_extent_x_dva", "grid_extent_y_dva", "probe_size_dva"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.probes_per_trial < 1:
            raise ValueError("probes_per_trial must be >= 1")
        if self.n_reps_per_cell < 1:
            raise ValueError("n_reps_per_cell must be >= 1")
        if not 0 <= self.window_start_ms < self.window_end_ms:
            raise ValueError(
                "the counting window must satisfy 0 <= window_start_ms < window_end_ms"
            )
        if self.n_display_maps < 0:
            raise ValueError("n_display_maps must be >= 0")
        return self

    @property
    def grid(self) -> ProbeGrid:
        return ProbeGrid.from_extent(
            self.grid_cols,
            self.grid_rows,
            self.grid_extent_x_dva,
            self.grid_extent_y_dva,
            center=(self.grid_center_x_dva, self.grid_center_y_dva),
        )

    @property
    def window_s(self) -> tuple[float, float]:
        return self.window_start_ms / 1000.0, self.window_end_ms / 1000.0


@dataclass(frozen=True)
class ProbeSpec:
    """One planned flash: which cell, where that is, which polarity."""

    col: int
    row: int
    x_dva: float
    y_dva: float
    polarity: Literal["bright", "dark"]

    def payload(self, index: int) -> dict[str, Any]:
        """What PROBE_ON carries: everything analysis needs to place this
        flash without reaching back into the config."""
        return {
            "index": index,
            "col": self.col,
            "row": self.row,
            "x_dva": self.x_dva,
            "y_dva": self.y_dva,
            "polarity": self.polarity,
        }


# ----------------------------------------------------------------------
# The probe stimulus
# ----------------------------------------------------------------------


class ProbeSquare:
    """A repositionable luminance square: one PsychoPy Rect, moved and
    recoloured per flash rather than one stimulus object per cell — a
    16x16 grid would otherwise construct 256 of them per trial."""

    def __init__(self, display: DisplayBackend, screen: Screen, size_dva: float) -> None:
        from psychopy import visual

        side = screen.deg2px(size_dva)
        self._rect: Any = visual.Rect(
            display.window,
            width=side,
            height=side,
            fillColor=(1.0, 1.0, 1.0),
            lineColor=None,
            units="pix",
        )

    def set_probe(self, x_px: float, y_px: float, polarity: str) -> None:
        self._rect.pos = (x_px, y_px)
        # PsychoPy's rgb space runs -1..1: +1 is white, -1 black, and the
        # window's default grey background sits at 0 — equal contrast steps
        # up and down, which is what makes "both" polarities comparable.
        level = 1.0 if polarity == "bright" else -1.0
        self._rect.fillColor = (level, level, level)

    def update(self, dt: float) -> None:
        return  # a flash has no time-varying state between flips

    def draw(self) -> None:
        self._rect.draw()


class RecordingProbe:
    """The simulated sibling: records placements and draws, so a headless
    test can assert which cells were shown without a renderer."""

    def __init__(self) -> None:
        self.placements: list[tuple[float, float, str]] = []
        self.draw_count = 0

    def set_probe(self, x_px: float, y_px: float, polarity: str) -> None:
        self.placements.append((x_px, y_px, polarity))

    def update(self, dt: float) -> None:
        return

    def draw(self) -> None:
        self.draw_count += 1


def make_probe(display: DisplayBackend, screen: Screen, size_dva: float) -> Stimulus:
    """Backend-appropriate probe, same seam as ``make_fixation``."""
    if display.kind == "simulated":
        return RecordingProbe()
    return ProbeSquare(display, screen, size_dva)


# ----------------------------------------------------------------------
# The phase
# ----------------------------------------------------------------------


class ProbeSequence:
    """Flash the trial's probes while fixation holds; log what was shown.

    Frame-counted, not clock-counted: a display changes only on flips, so
    "100 ms" is resolved to whole frames once, in ``build_trial``, and this
    phase simply plays the resulting schedule. The blink rule applies every
    frame — an unverifiable gaze sample ends the trial as a break, and the
    gaze check runs before anything is drawn or queued, so a probe is never
    flashed into a trial that has already failed.

    The probe log is photon-honest: each flash queues ``PROBE_ON`` via
    ``emit_on_flip`` and its timestamp is *harvested back* from the record
    on a later frame (the engine mirrors every event as ``t_probe_on``
    after the flip that showed it). The log is re-serialised into the
    record after every harvest, so however the trial ends — break, pause,
    abort — the row says exactly which probes were really shown.

    The schedule ends with ``tail_frames`` of bare fixation so the last
    flash's spike-counting window closes while the eye is still on the
    point; without the tail, the final probes of every trial would be
    counted against a moving eye.
    """

    name = "probe_sequence"

    def __init__(
        self,
        probes: list[ProbeSpec],
        *,
        flash_frames: int,
        isi_frames: int,
        tail_frames: int,
        on_break: Outcome,
        on_done: Outcome,
        region: str = "fixation",
        fixation_key: str = "fixation",
        probe_key: str = "probe",
    ) -> None:
        if not probes:
            raise ValueError("ProbeSequence needs at least one probe")
        if flash_frames < 1:
            raise ValueError("flash_frames must be >= 1 — a zero-frame flash never shows")
        if isi_frames < 0 or tail_frames < 1:
            raise ValueError("isi_frames must be >= 0 and tail_frames >= 1")
        self._probes = list(probes)
        self._on_break = on_break
        self._on_done = on_done
        self._region = region
        self._fixation_key = fixation_key
        self._probe_key = probe_key
        # One entry per frame: (draw_probe_index | None, is_onset_frame).
        # Precomputed because the schedule is fixed; a couple of hundred
        # tuples per trial, traded for per-frame logic that cannot drift.
        self._timeline: list[tuple[int | None, bool]] = []
        for k in range(len(self._probes)):
            self._timeline.extend([(None, False)] * isi_frames)
            self._timeline.append((k, True))
            self._timeline.extend([(k, False)] * (flash_frames - 1))
        self._timeline.extend([(None, False)] * tail_frames)

    def on_enter(self, ctx: TrialContext) -> None:
        self._cursor = 0
        self._shown: list[dict[str, Any]] = []
        self._awaiting: dict[str, Any] | None = None
        self._last_t = ctx.record.get(f"t_{PROBE_EVENT.lower()}")
        self._write_log(ctx)

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        # Collect the previous flash's flip time before anything else, so
        # even a trial that breaks on this very frame keeps it.
        self._harvest(ctx)
        # The blink rule, before the completion check and before drawing:
        # an unverifiable sample on the last tail frame is a break, never a
        # lucky completion.
        if not ctx.regions[self._region].contains(ctx.inputs.gaze):
            return self._on_break
        fixation = ctx.stimuli[self._fixation_key]
        fixation.update(ctx.dt)
        fixation.draw()

        if self._cursor >= len(self._timeline):
            return self._on_done
        probe_index, is_onset = self._timeline[self._cursor]
        self._cursor += 1
        if probe_index is not None:
            probe_stimulus = ctx.stimuli[self._probe_key]
            if is_onset:
                spec = self._probes[probe_index]
                probe_stimulus.set_probe(
                    ctx.screen.deg2px(spec.x_dva), ctx.screen.deg2px(spec.y_dva), spec.polarity
                )
                # Queued, not emitted: the flash is only true once the flip
                # that carries it returns, and the engine stamps it then.
                ctx.emit_on_flip(PROBE_EVENT, spec.payload(probe_index))
                self._awaiting = spec.payload(probe_index)
            probe_stimulus.update(ctx.dt)
            probe_stimulus.draw()
        return PhaseAction.CONTINUE

    def _harvest(self, ctx: TrialContext) -> None:
        """Move the pending probe into the shown log once its flip time has
        appeared in the record (the engine writes ``t_probe_on`` after the
        flip that showed it — one frame after we queued it)."""
        if self._awaiting is None:
            return
        t = ctx.record.get(f"t_{PROBE_EVENT.lower()}")
        if t is None or t == self._last_t:
            return
        self._shown.append({**self._awaiting, "t": t})
        self._awaiting = None
        self._last_t = t
        self._write_log(ctx)

    def _write_log(self, ctx: TrialContext) -> None:
        """Keep the record's log current after every harvest, so any exit
        path — break, pause, abort — leaves the row telling the truth about
        what was actually flashed."""
        ctx.record["rf_n_probes_shown"] = len(self._shown)
        ctx.record["rf_probes_json"] = json.dumps(self._shown)


# ----------------------------------------------------------------------
# The scheduler: completion is per probe, not per trial
# ----------------------------------------------------------------------


class ProbeSchedule:
    """Serve every (cell, polarity) repetition exactly once, across trials.

    A ``TrialSource`` whose real queue is probes: ``next()`` says only
    "another fixation trial, please" while there are probes left, and
    ``take()`` — called by ``build_trial`` — hands the next batch out.
    ``record()`` reads how many of the batch were actually shown (the
    phase's count is a prefix of the batch, since probes show in order) and
    returns the rest to the back of the queue, the same place a re-served
    condition goes in every other scheduler.
    """

    def __init__(
        self,
        grid: ProbeGrid,
        *,
        n_reps_per_cell: int,
        polarity: str,
        probes_per_trial: int,
        rng: np.random.Generator,
    ) -> None:
        self._grid = grid
        self._probes_per_trial = probes_per_trial
        planned: list[ProbeSpec] = []
        for row in range(grid.rows):
            for col in range(grid.cols):
                x, y = grid.cell_center_dva(col, row)
                for rep in range(n_reps_per_cell):
                    # "both" splits each cell's repetitions between the
                    # polarities (odd counts favour bright), keeping the
                    # session length identical across the three settings.
                    if polarity == "both":
                        chosen = "bright" if rep % 2 == 0 else "dark"
                    else:
                        chosen = polarity
                    planned.append(ProbeSpec(col, row, x, y, chosen))  # type: ignore[arg-type]
        order = rng.permutation(len(planned))
        self._queue: list[ProbeSpec] = [planned[i] for i in order]
        self._outstanding: list[ProbeSpec] = []
        self._shown_per_cell = np.zeros((grid.rows, grid.cols), dtype=np.int64)
        self._planned_per_cell = n_reps_per_cell

    @property
    def remaining(self) -> int:
        return len(self._queue) + len(self._outstanding)

    def take(self, n: int) -> list[ProbeSpec]:
        """The next batch for one trial. The batch is remembered until
        ``record()`` reconciles it — taking twice in between is a
        programming error, not a scheduling state."""
        if self._outstanding:
            raise RuntimeError(
                "ProbeSchedule.take() called again before record() reconciled the previous "
                "batch — build_trial must take exactly once per served trial"
            )
        batch, self._queue = self._queue[:n], self._queue[n:]
        self._outstanding = batch
        return list(batch)

    # -- TrialSource ---------------------------------------------------

    def next(self) -> Condition | None:
        if not self._queue:
            return None
        return Condition({})

    def record(self, condition: Condition, result: Any) -> None:
        shown = int(result.record.get("rf_n_probes_shown", 0))
        if shown > len(self._outstanding):
            raise RuntimeError(
                f"trial reports {shown} probes shown but only {len(self._outstanding)} were "
                f"taken — the phase and the schedule disagree about this trial"
            )
        for spec in self._outstanding[:shown]:
            self._shown_per_cell[spec.row, spec.col] += 1
        # The unshown remainder goes to the BACK of the queue, like every
        # scheduler's re-serve: retrying it immediately would hammer the
        # same cells against a subject that just broke fixation.
        self._queue.extend(self._outstanding[shown:])
        self._outstanding = []

    def summary(self) -> Any:
        """Per-cell coverage for ``*_paradigm.csv``: which cells finished
        their repetitions and which still owe some when the session ended."""
        import pandas as pd

        rows = []
        for row in range(self._grid.rows):
            for col in range(self._grid.cols):
                x, y = self._grid.cell_center_dva(col, row)
                rows.append(
                    {
                        "col": col,
                        "row": row,
                        "x_dva": round(x, 4),
                        "y_dva": round(y, 4),
                        "planned": self._planned_per_cell,
                        "shown": int(self._shown_per_cell[row, col]),
                    }
                )
        return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# The live map
# ----------------------------------------------------------------------


class LiveRFMap:
    """The between-trials analysis: PROBE_ON events + the spike stream →
    heat-map panels and a saved artifact.

    Bus side (``on_event``, mid-trial): append the flash to a list —
    nothing else, per the live-analysis contract. Runner side
    (``on_trial``, between trials): drain the spike source, and fold in
    every flash whose counting window the stream has fully covered;
    flashes still inside the detector's latency stay pending and fold in
    next time, so the newest flashes are never undercounted. ``finish``
    flushes once more and writes ``rf_live_maps.npz``.
    """

    def __init__(self, params: RFMapParams, spikes: SpikeSource | None) -> None:
        self._params = params
        self._grid = params.grid
        self._spikes = spikes
        self._window = params.window_s
        self._pending: list[dict[str, Any]] = []
        self._spike_times: np.ndarray = np.empty(0, dtype=np.float64)
        self._spike_rows: np.ndarray = np.empty(0, dtype=np.int32)
        self._covered: float | None = None
        self._wait_warned = False
        if spikes is None:
            self._accumulator: RFAccumulator | None = None
            self._display_rows: list[int] | None = None
            return
        window_length = self._window[1] - self._window[0]
        self._accumulator = RFAccumulator(self._grid, spikes.n_channels, window_length)
        # Pinned display channels resolve hardware ids to monitored rows
        # now, at build time, so an id the source does not monitor fails
        # before the session rather than as a blank panel during it.
        if params.map_channels is None:
            self._display_rows = None  # auto: the most responsive, per publish
        else:
            ids = list(spikes.channel_ids)
            missing = [c for c in params.map_channels if c not in ids]
            if missing:
                raise ConfigError(
                    f"map_channels names channels {missing} but the spike source monitors "
                    f"ids {ids[:8]}{'...' if len(ids) > 8 else ''} — fix map_channels or the "
                    f"rig's spikes.channels"
                )
            self._display_rows = [ids.index(c) for c in params.map_channels]

    # -- bus side ------------------------------------------------------

    def on_event(self, event: Any) -> None:
        if event.name != PROBE_EVENT:
            return
        payload = event.payload
        self._pending.append(
            {
                "t": event.t,
                "col": payload["col"],
                "row": payload["row"],
                "polarity": payload.get("polarity", "bright"),
            }
        )

    # -- runner side ---------------------------------------------------

    def on_trial(self, record: dict[str, Any]) -> None:
        if self._spikes is None:
            return
        self._absorb(wait=True)

    def _absorb(self, wait: bool) -> None:
        """Drain the source and fold in every coverable pending flash.

        ``wait`` bounds a short real-time wait for the stream to cover the
        newest flash's window — normally satisfied within one fetch
        interval. On timeout the flashes stay pending (they fold in on the
        next trial) and the delay is logged once: a live map that silently
        lagged a trial behind would read as a response latency change.
        """
        assert self._spikes is not None and self._accumulator is not None
        deadline = time.monotonic() + (_COVERAGE_WAIT_S if wait else 0.0)
        while True:
            batch = self._spikes.drain()
            if len(batch):
                self._spike_times = np.concatenate([self._spike_times, batch.times])
                self._spike_rows = np.concatenate([self._spike_rows, batch.channels])
            if batch.covered_until is not None:
                self._covered = batch.covered_until
            still_pending = self._fold_covered()
            if not still_pending or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if still_pending and wait and not self._wait_warned:
            self._wait_warned = True
            log.warning(
                "live RF map: the spike stream had not covered the last %d flash(es) within "
                "%.1f s; they will be folded in with the next trial",
                len(self._pending),
                _COVERAGE_WAIT_S,
            )

    def _fold_covered(self) -> bool:
        """Fold in every pending flash whose window is fully covered;
        returns whether any remain pending."""
        assert self._accumulator is not None
        if not self._pending:
            return False
        # Sorted once per fold: batches arrive nearly ordered, and the
        # buffer is pruned below, so this stays small.
        order = np.argsort(self._spike_times, kind="stable")
        times, rows = self._spike_times[order], self._spike_rows[order]
        remaining: list[dict[str, Any]] = []
        for flash in self._pending:
            start = flash["t"] + self._window[0]
            end = flash["t"] + self._window[1]
            if self._covered is None or self._covered < end:
                remaining.append(flash)
                continue
            lo = int(np.searchsorted(times, start, side="left"))
            hi = int(np.searchsorted(times, end, side="left"))
            counts = np.bincount(rows[lo:hi], minlength=self._accumulator.n_channels)
            self._accumulator.add_flash(flash["col"], flash["row"], counts)
        self._pending = remaining
        # Prune spikes nothing can need any more: everything older than the
        # earliest window any pending flash could still open. Without this
        # the buffer grows for the whole session.
        horizon = min(
            [flash["t"] + self._window[0] for flash in remaining],
            default=self._covered if self._covered is not None else 0.0,
        )
        keep = times >= horizon - 1.0
        self._spike_times, self._spike_rows = times[keep], rows[keep]
        return bool(remaining)

    # -- dashboard side ------------------------------------------------

    def panels(self) -> list[dict[str, Any]]:
        title = "Receptive fields"
        section = "RF map"
        if self._spikes is None or self._accumulator is None:
            return [
                {
                    "title": title,
                    "section": section,
                    "data": {
                        "form": "empty",
                        "message": (
                            "no spike source on this rig — add devices.spikes to the rig "
                            "config to map live"
                        ),
                    },
                }
            ]
        accumulator = self._accumulator
        if accumulator.n_flashes == 0:
            return [
                {
                    "title": title,
                    "section": section,
                    "data": {"form": "empty", "message": "waiting for the first mapped flashes"},
                }
            ]

        ids = self._spikes.channel_ids
        if self._display_rows is not None:
            shown_rows = self._display_rows
        else:
            shown_rows = accumulator.best_channels(self._params.n_display_maps)
        maps = [{"name": "population", "matrix": _matrix(accumulator.pooled_rate_map())}]
        for row_index in shown_rows:
            entry: dict[str, Any] = {
                "name": f"ch {ids[row_index]}",
                "matrix": _matrix(accumulator.rate_map(row_index)),
            }
            centroid = accumulator.centroid_dva(row_index)
            if centroid is not None:
                entry["centroid"] = [round(centroid[0], 3), round(centroid[1], 3)]
            maps.append(entry)

        values = [v for m in maps for line in m["matrix"] for v in line if v is not None]
        stats = [
            {"label": "flashes mapped", "value": f"{accumulator.n_flashes:,}"},
            {"label": "spikes", "value": f"{accumulator.n_spikes:,}"},
            {"label": "coverage", "value": f"{100 * accumulator.coverage():.0f}%"},
        ]
        if self._pending:
            stats.append({"label": "pending", "value": f"{len(self._pending):,}"})
        return [
            {
                "title": title,
                "section": section,
                "data": {
                    "form": "heatmap",
                    "maps": maps,
                    "x_edges": [round(float(e), 4) for e in self._grid.x_edges_dva],
                    "y_edges": [round(float(e), 4) for e in self._grid.y_edges_dva],
                    # Flash counts per cell, same orientation as the maps —
                    # the hover readout's "how many flashes is this rate
                    # from", without which a hot cell from one lucky flash
                    # reads like a receptive field.
                    "flashes": [[int(n) for n in line] for line in accumulator.flashes],
                    "vmax": max(values) if values else 0.0,
                    "x_label": "azimuth (dva)",
                    "y_label": "elevation (dva)",
                    "value_label": "spikes/s",
                    "stats": stats,
                    "note": (
                        "live threshold crossings, counted "
                        f"{self._params.window_start_ms:g}-{self._params.window_end_ms:g} ms "
                        "after each flash — a quick look, not the sorted analysis"
                    ),
                },
            }
        ]

    # -- teardown ------------------------------------------------------

    def finish(self, run_dir: Path) -> None:
        if self._spikes is None or self._accumulator is None:
            log.info("live RF map: no spike source was wired, so no map artifact is written")
            return
        # One last, non-waiting flush: whatever the stream has covered by
        # teardown is folded in; anything else is reported, not invented.
        self._absorb(wait=True)
        if self._pending:
            log.warning(
                "live RF map: %d flash(es) never had their window covered by the spike "
                "stream and are absent from the saved map",
                len(self._pending),
            )
        out = run_dir / "rf_live_maps.npz"
        np.savez(
            out,
            counts=self._accumulator.counts,
            flashes=self._accumulator.flashes,
            channel_ids=np.asarray(self._spikes.channel_ids, dtype=np.int64),
            x_edges_dva=self._grid.x_edges_dva,
            y_edges_dva=self._grid.y_edges_dva,
            window_s=np.asarray(self._window),
            n_unmapped_flashes=np.int64(len(self._pending)),
        )
        log.info(
            "live RF map written: %s (%d flashes, %d spikes, %.0f%% coverage)",
            out.name,
            self._accumulator.n_flashes,
            self._accumulator.n_spikes,
            100 * self._accumulator.coverage(),
        )


def _matrix(rates: np.ndarray) -> list[list[float | None]]:
    """A rate map as JSON-safe nested lists: row 0 is the grid's BOTTOM row
    (y ascending, matching y_edges), NaN → null — JSON has no NaN, and an
    unprobed cell must reach the page as "unknown", not as zero."""
    return [
        [None if math.isnan(value) else round(float(value), 4) for value in line] for line in rates
    ]


# ----------------------------------------------------------------------
# The task
# ----------------------------------------------------------------------


class RFMapTask(Task):
    """The shared machinery of the four presets; abstract (no ``name``).

    Subclass a preset — or this — for your own sites: override
    ``params_model`` with new defaults for the geometry, and everything
    else (trial structure, live map, demo, movie, autopilot) comes along.
    """

    events = EventSchema(("FIX_ON", "FIX_ACQUIRED", PROBE_EVENT))
    # COMPLETED carries no success flag on purpose: holding fixation is not
    # "correct", so the dashboard's performance panel falls back to the
    # completion rate — the number that actually describes a mapping
    # session's subject.
    outcomes = outcomes(
        COMPLETED=dict(completed=True),
        FIX_BREAK=dict(completed=False),
        NO_FIXATION=dict(completed=False),
    )
    params_model: ClassVar[type[Model]] = RFMapParams
    reward = RewardPolicy(by_outcome={"COMPLETED": RewardPulses(n_pulses=2, pulse_ms=150)})
    # The defaults assume saccade landings and response keys, none of which
    # a mapping session has; these five say what it does have. The RF maps
    # themselves arrive as live-analysis panels beside them.
    dashboard = DashboardSpec(
        include_defaults=False,
        panels=(
            DashboardPanel(kind="performance", title="Fixation held"),
            DashboardPanel(kind="rewards", title="Reward earned"),
            DashboardPanel(kind="outcomes", title="Outcomes"),
            DashboardPanel(
                kind="series", title="Probes shown per trial", value="rf_n_probes_shown"
            ),
            DashboardPanel(kind="stat", title="Probes shown", value="rf_n_probes_shown", agg="sum"),
        ),
    )

    def __init__(self, params: Model) -> None:
        super().__init__(params)
        self._schedule: ProbeSchedule | None = None

    # -- scheduling ----------------------------------------------------

    def make_source(self, params: BaseModel, rng: np.random.Generator) -> TrialSource:
        p: RFMapParams = params  # type: ignore[assignment]
        self._schedule = ProbeSchedule(
            p.grid,
            n_reps_per_cell=p.n_reps_per_cell,
            polarity=p.probe_polarity,
            probes_per_trial=p.probes_per_trial,
            rng=rng,
        )
        return self._schedule

    # -- one trial -----------------------------------------------------

    def build_trial(self, setup: TrialSetup) -> TrialPlan:
        params: RFMapParams = self.params  # type: ignore[assignment]
        if self._schedule is None:
            raise RuntimeError(
                "build_trial before make_source: the probe schedule owns which cells are "
                "left, so a session must create it first (build_session does)"
            )
        hz = setup.refresh_rate_hz
        flash_frames = params.flash.n_frames(hz)
        if flash_frames < 1:
            raise ConfigError(
                f"flash {params.flash} resolves to 0 frames at {hz:g} Hz — a probe that "
                f"never shows maps nothing; give it at least one frame"
            )
        isi_frames = params.isi.n_frames(hz)
        # Fixation must outlive the last flash's counting window, or the
        # final probes of every trial are counted against a moving eye.
        tail_frames = max(1, math.ceil(params.window_end_ms / 1000.0 * hz))

        probes = self._schedule.take(params.probes_per_trial)
        screen = setup.screen
        return TrialPlan(
            phases=[
                phases.AcquireFixation(
                    timeout_s=params.acquire_timeout.seconds(hz),
                    on_timeout=self.outcomes["NO_FIXATION"],
                ),
                phases.HoldFixation(
                    duration_s=params.initial_hold.seconds(hz),
                    on_break=self.outcomes["FIX_BREAK"],
                ),
                ProbeSequence(
                    probes,
                    flash_frames=flash_frames,
                    isi_frames=isi_frames,
                    tail_frames=tail_frames,
                    on_break=self.outcomes["FIX_BREAK"],
                    on_done=self.outcomes["COMPLETED"],
                ),
            ],
            stimuli={
                "fixation": make_fixation(setup.display, screen, params.fix_size_dva),
                "probe": make_probe(setup.display, screen, params.probe_size_dva),
            },
            regions={"fixation": CircleRegion((0.0, 0.0), screen.deg2px(params.fix_window_dva))},
            record={"rf_n_probes_planned": len(probes)},
        )

    # -- the live analysis --------------------------------------------

    def live_analysis(self, wiring: LiveWiring) -> LiveRFMap:
        params: RFMapParams = self.params  # type: ignore[assignment]
        return LiveRFMap(params, wiring.spikes)

    # -- the other modes ----------------------------------------------

    def simulation(self, seed: int) -> Any:
        """An autopilot that simply fixates — which is the whole task. Pair
        it with a rig whose ``spikes`` backend is ``simulated`` and the
        session maps that source's ground-truth fields end to end."""
        from alhazen.devices.automated import AutomatedGazeTracker
        from alhazen.modes.simulation import Simulation

        return Simulation(
            tracker=AutomatedGazeTracker(),
            describe={"gaze": "holds fixation for every trial", "seed": seed},
        )

    def demo_views(self, setup: Any) -> list[Any]:
        """Two views: the grid's geometry, and the flash sequence at speed."""
        from psychopy import visual

        from alhazen.modes.demo import DemoView

        params: RFMapParams = self.params  # type: ignore[assignment]
        grid = params.grid
        screen: Screen = setup.screen
        window = setup.display.window
        side = screen.deg2px(params.probe_size_dva)

        fixation = visual.Circle(
            window,
            radius=screen.deg2px(params.fix_size_dva) / 2.0,
            fillColor=(1.0, 1.0, 1.0),
            lineColor=(1.0, 1.0, 1.0),
            units="pix",
        )
        probe = visual.Rect(
            window,
            width=side,
            height=side,
            fillColor=(1.0, 1.0, 1.0),
            lineColor=None,
            units="pix",
        )
        # The grid's cell boundaries, once: cols+1 verticals, rows+1
        # horizontals — a readable skeleton, not 256 rectangles per frame.
        line_color = (-0.4, -0.4, -0.4)
        x_edges = [screen.deg2px(x) for x in grid.x_edges_dva]
        y_edges = [screen.deg2px(y) for y in grid.y_edges_dva]
        lines = [
            visual.Line(
                window,
                start=(x, y_edges[0]),
                end=(x, y_edges[-1]),
                lineColor=line_color,
                units="pix",
            )
            for x in x_edges
        ] + [
            visual.Line(
                window,
                start=(x_edges[0], y),
                end=(x_edges[-1], y),
                lineColor=line_color,
                units="pix",
            )
            for y in y_edges
        ]

        def place(cell: int, polarity: str) -> None:
            col, row = cell % grid.cols, (cell // grid.cols) % grid.rows
            x, y = grid.cell_center_dva(col, row)
            probe.pos = (screen.deg2px(x), screen.deg2px(y))
            level = 1.0 if polarity == "bright" else -1.0
            probe.fillColor = (level, level, level)

        def draw_layout(elapsed: float) -> None:
            for line in lines:
                line.draw()
            # Walk the cells in order, alternating polarity, so size,
            # spacing and both contrasts can all be judged in one look.
            step = int(elapsed / 0.4)
            place(step, "bright" if step % 2 == 0 else "dark")
            probe.draw()
            fixation.draw()

        # The sequence view runs at the configured pace. Demo mode has no
        # measured refresh (that is a session's business), so frame-denominated
        # durations are shown at their 60 Hz equivalent — a preview, not a
        # measurement, which is all a demo claims to be.
        flash_s = _approx_seconds(params.flash)
        period_s = flash_s + _approx_seconds(params.isi)

        def draw_sequence(elapsed: float) -> None:
            index = int(elapsed / period_s) if period_s > 0 else 0
            # Seeded per flash index: deterministic, and restarting the
            # view replays the identical sequence.
            rng = np.random.default_rng((7, index))
            in_flash = (elapsed - index * period_s) < flash_s
            if in_flash:
                if params.probe_polarity == "both":
                    polarity = "bright" if rng.random() < 0.5 else "dark"
                else:
                    polarity = params.probe_polarity
                place(int(rng.integers(grid.n_cells)), polarity)
                probe.draw()
            fixation.draw()

        return [
            DemoView(
                name="layout",
                caption=(
                    f"{grid.cols}x{grid.rows} cells over "
                    f"{params.grid_extent_x_dva:g}x{params.grid_extent_y_dva:g} dva — "
                    f"{params.probe_size_dva:g} dva probes"
                ),
                draw=draw_layout,
                key="1",
            ),
            DemoView(
                name="sequence",
                caption="the flash sequence at configured speed",
                draw=draw_sequence,
                key="2",
            ),
        ]

    def movie_clips(self, setup: Any) -> list[Any]:
        """One clip of the probe sequence, composited in numpy — the same
        geometry a trial draws, on the rig ``--rig`` named."""
        from alhazen.modes.movie import MovieClip

        params: RFMapParams = self.params  # type: ignore[assignment]
        grid = params.grid
        screen: Screen = setup.screen
        hz = setup.hz
        flash_frames = params.flash.n_frames(hz)
        if flash_frames < 1:
            raise ConfigError(
                f"flash {params.flash} resolves to 0 frames at {hz:g} Hz — a probe that "
                f"never shows cannot be filmed either"
            )
        isi_frames = params.isi.n_frames(hz)
        n_flashes = 24

        def frames() -> Any:
            # Its own fixed seed, so recording the clip twice (a file and a
            # sheet panel) yields identical pixels.
            rng = np.random.default_rng(20260831)
            base = np.full((screen.height_px, screen.width_px), 0.5, dtype=np.float32)
            _paint_square(base, screen, 0.0, 0.0, params.fix_size_dva, 1.0)
            for _ in range(n_flashes):
                cell = int(rng.integers(grid.n_cells))
                col, row = cell % grid.cols, cell // grid.cols
                if params.probe_polarity == "both":
                    value = 1.0 if rng.random() < 0.5 else 0.0
                else:
                    value = 1.0 if params.probe_polarity == "bright" else 0.0
                for _ in range(isi_frames):
                    yield base
                flashed = base.copy()
                x, y = grid.cell_center_dva(col, row)
                _paint_square(flashed, screen, x, y, params.probe_size_dva, value)
                for _ in range(flash_frames):
                    yield flashed

        return [
            MovieClip(
                name="probe-sequence",
                label=(
                    f"sparse probes · {grid.cols}x{grid.rows} over "
                    f"{params.grid_extent_x_dva:g}x{params.grid_extent_y_dva:g} dva"
                ),
                frames=frames,
            )
        ]


def _approx_seconds(duration: Duration) -> float:
    """A Duration as seconds for the demo's pacing: exact when given in ms,
    read at 60 Hz when given in frames — the demo has no measured refresh
    rate and does not pretend to (movie mode, which does, resolves
    exactly)."""
    if duration.ms is not None:
        return duration.ms / 1000.0
    assert duration.frames is not None
    return duration.frames / 60.0


def _paint_square(
    canvas: np.ndarray, screen: Screen, x_dva: float, y_dva: float, size_dva: float, value: float
) -> None:
    """Fill an axis-aligned square into a (height, width) luminance frame.

    Centered-dva in, array indices out: array row 0 is the TOP of the
    screen, so y flips here — the same conversion Screen does for trackers,
    reproduced for pixels because a numpy frame has no Screen to ask.
    """
    half = screen.deg2px(size_dva) / 2.0
    cx = screen.width_px / 2.0 + screen.deg2px(x_dva)
    cy = screen.height_px / 2.0 - screen.deg2px(y_dva)
    x0, x1 = int(round(cx - half)), int(round(cx + half))
    y0, y1 = int(round(cy - half)), int(round(cy + half))
    canvas[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)] = value


# ----------------------------------------------------------------------
# The presets: same task, area-scaled defaults
# ----------------------------------------------------------------------


class V1RFMapParams(RFMapParams):
    """Parafoveal V1: small fields, fine grid, early window."""


class V2RFMapParams(RFMapParams):
    """V2: fields roughly twice V1's, a slightly later window."""

    grid_cols: int = 12
    grid_rows: int = 12
    grid_extent_x_dva: float = 12.0
    grid_extent_y_dva: float = 12.0
    probe_size_dva: float = 1.0
    window_start_ms: float = 40.0
    window_end_ms: float = 110.0


class V4RFMapParams(RFMapParams):
    """V4: larger fields at mid eccentricity, longer flashes for a slower,
    later response."""

    grid_cols: int = 10
    grid_rows: int = 10
    grid_extent_x_dva: float = 14.0
    grid_extent_y_dva: float = 14.0
    probe_size_dva: float = 1.75
    flash: Duration = Duration(ms=150)
    window_start_ms: float = 50.0
    window_end_ms: float = 150.0


class MTRFMapParams(RFMapParams):
    """MT: fields comparable to their eccentricity — a coarse, wide grid
    and big probes. Flashed squares drive MT well enough to place fields;
    direction tuning is a follow-up task's job, not a mapping grid's."""

    grid_cols: int = 10
    grid_rows: int = 10
    grid_extent_x_dva: float = 20.0
    grid_extent_y_dva: float = 20.0
    probe_size_dva: float = 3.0
    window_start_ms: float = 30.0
    window_end_ms: float = 120.0


class V1RFMapTask(RFMapTask):
    name = "rf-map-v1"
    params_model = V1RFMapParams


class V2RFMapTask(RFMapTask):
    name = "rf-map-v2"
    params_model = V2RFMapParams


class V4RFMapTask(RFMapTask):
    name = "rf-map-v4"
    params_model = V4RFMapParams


class MTRFMapTask(RFMapTask):
    name = "rf-map-mt"
    params_model = MTRFMapParams
