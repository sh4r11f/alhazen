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


class FrameSequence:
    """Play ``timeline`` frame by frame, then hand back ``then``."""

    name = "frame_sequence"

    def __init__(
        self,
        timeline: FrameTimeline,
        then: Any = PhaseAction.ADVANCE,
        on_break: Outcome | None = None,
        hold_region: str | None = None,
    ) -> None:
        if hold_region is not None and on_break is None:
            raise ValueError("holding a region during a sequence needs an on_break outcome")
        self._timeline = timeline
        self._then = then
        self._on_break = on_break
        self._hold_region = hold_region

    def on_enter(self, ctx: TrialContext) -> None:
        self._frame = 0
        ctx.record["sequence_frames"] = self._timeline.n_frames

    def on_frame(self, ctx: TrialContext) -> str | Outcome:
        # The hold check comes first, as in HoldFixation: a break on the last
        # frame of the sequence is still a break.
        if self._hold_region is not None and not ctx.regions[self._hold_region].contains(
            ctx.inputs.gaze
        ):
            assert self._on_break is not None
            ctx.record["sequence_break_frame"] = self._frame
            return self._on_break

        for key, attr, value in self._timeline.settings_at(self._frame):
            setattr(ctx.stimuli[key], attr, value)
        for key in self._timeline.visible_at(self._frame):
            stimulus = ctx.stimuli[key]
            stimulus.update(ctx.dt)
            stimulus.draw()
        for name in self._timeline.events_at(self._frame):
            # Queued, not emitted: the event belongs to the flip that shows
            # this frame, which has not happened yet.
            ctx.emit_on_flip(name)

        self._frame += 1
        if self._frame >= self._timeline.n_frames:
            return self._then
        return PhaseAction.CONTINUE
