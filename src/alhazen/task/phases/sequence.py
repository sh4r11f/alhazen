"""FrameSequence: play a compiled frame timeline.

For any stimulus whose schedule matters to the frame — a moving frame, a
flashed probe, anything where "which frame did it appear on" is the
measurement — this phase replays a ``FrameTimeline`` exactly: on frame *k* it
applies that frame's settings, draws what that frame shows, and queues that
frame's events on that frame's flip.

Counting frames rather than reading the clock is the point. A clock-driven
phase asks "has 50 ms passed yet", and the answer depends on when it happened
to be asked; a frame-driven one shows frame *k*'s content on frame *k*, every
trial, on every rig, and a dropped frame shows up in the frame log rather than
silently shifting the stimulus.
"""

from __future__ import annotations

from typing import Any

from alhazen.core.trial import Outcome, PhaseAction, TrialContext
from alhazen.display.frames import FrameTimeline
from alhazen.task.phases._draw import draw_stimuli
from alhazen.task.phases._record import column_prefix, record_once


class FrameSequence:
    """Play ``timeline`` frame by frame, then hand back ``then``.

    It shows exactly ``timeline.n_frames`` frames: ``then`` is returned on
    the frame that draws the timeline's last, so the next phase's first frame
    is the one after. Counted in frames, so it needs no ``ctx.time_up``; a
    dropped frame lengthens it in time (and shows in the frame log) rather
    than costing it a frame of its schedule.

    Records, under ``record_prefix`` (default ``sequence``):
    ``<prefix>_frames``, the timeline's length, when the phase starts; and
    ``<prefix>_break_frame``, the frame gaze left ``hold_region`` on, when
    that ends the trial. A trial with two sequences gives each its own
    prefix, for the reason ``HoldFixation`` gives:
    a column the record already holds is still overwritten, with a
    ``FutureWarning`` naming it (since 2.6), and 3.0 refuses it. A prefix
    that is not a plain identifier, or that makes one of the columns
    alhazen writes itself, is refused here."""

    name = "frame_sequence"

    def __init__(
        self,
        timeline: FrameTimeline,
        then: Any = PhaseAction.ADVANCE,
        on_break: Outcome | None = None,
        hold_region: str | None = None,
        *,
        record_prefix: str = "sequence",
    ) -> None:
        if hold_region is not None and on_break is None:
            raise ValueError("holding a region during a sequence needs an on_break outcome")
        self._timeline = timeline
        self._then = then
        self._on_break = on_break
        self._hold_region = hold_region
        self._prefix = column_prefix(
            "FrameSequence", "record_prefix", record_prefix, ("frames", "break_frame")
        )

    def on_enter(self, ctx: TrialContext) -> None:
        self._frame = 0
        # Never over another phase's value in silence (HoldFixation says why).
        record_once(
            ctx,
            f"{self._prefix}_frames",
            self._timeline.n_frames,
            phase="FrameSequence",
            argument="record_prefix",
        )

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        # The hold check comes first, as in HoldFixation: a break on the last
        # frame of the sequence is still a break.
        if self._hold_region is not None and not ctx.regions[self._hold_region].contains(
            ctx.inputs.gaze
        ):
            assert self._on_break is not None
            record_once(
                ctx,
                f"{self._prefix}_break_frame",
                self._frame,
                phase="FrameSequence",
                argument="record_prefix",
            )
            return self._on_break

        for key, attr, value in self._timeline.settings_at(self._frame):
            setattr(ctx.stimuli[key], attr, value)
        draw_stimuli(ctx, self._timeline.visible_at(self._frame))
        for name in self._timeline.events_at(self._frame):
            # Queued, not emitted: the event belongs to the flip that shows
            # this frame, which has not happened yet.
            ctx.emit_on_flip(name)

        self._frame += 1
        if self._frame >= self._timeline.n_frames:
            return self._then
        return PhaseAction.CONTINUE
