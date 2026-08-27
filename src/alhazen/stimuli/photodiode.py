"""PhotodiodePatch: a corner patch that flashes on exactly the marked frames.

Why it exists: every software timestamp in a session is taken right after
``flip()`` returns, which is a claim about when photons changed, not a
measurement of it. A photodiode taped over this patch measures the real
thing. Because the patch turns white on the very frame whose flip carries the
event — the same flip that stamps the event's time and fires its sync pulse —
the diode trace, the TTL line and the recorded timestamp all refer to one
instant, and any discrepancy between them is measurable rather than assumed.

The patch is drawn on *every* frame (white or black), never conditionally
skipped, so the corner's mean luminance is constant and the diode sees a
clean two-level signal instead of a patch appearing out of nowhere.

The task never touches it: the session builder installs it as the engine's
per-frame overlay, so no phase has to remember to draw it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from alhazen.config.models import PhotodiodeConfig
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen

Corner = Literal["tl", "tr", "bl", "br"]


class PhotodiodePatch:
    """A square in one screen corner, white on armed frames and black on the
    rest.

    Unlike other stimuli (which get a NullStimulus stand-in from a factory on
    the simulated backend), this one branches internally: on a simulated
    display it records the exact white/black sequence into ``states``, which
    is what makes "the patch was white on precisely the frame that carried
    STIM_ON" a testable claim with no renderer installed.
    """

    def __init__(
        self,
        display: DisplayBackend,
        screen: Screen,
        corner: Corner = "br",
        size_px: int = 60,
        events: Iterable[str] = (),
    ) -> None:
        self._armed = frozenset(events)
        self._simulated = display.kind == "simulated"
        # Trace of every frame's state, newest last. Populated only on the
        # simulated backend, where there is nothing to look at otherwise.
        self.states: list[bool] = []
        self._stim: Any = None
        if not self._simulated:
            from psychopy import visual

            self._stim = visual.Rect(
                display.window,
                width=size_px,
                height=size_px,
                pos=_corner_pos(screen, corner, size_px),
                units="pix",
                fillColor=(1.0, 1.0, 1.0),
                lineColor=(1.0, 1.0, 1.0),
            )

    @property
    def armed_events(self) -> frozenset[str]:
        return self._armed

    def draw(self, pending_event_names: Iterable[str]) -> None:
        """Draw this frame's patch. ``pending_event_names`` are the events
        queued for the upcoming flip — the engine's overlay hook passes them
        straight from the trial context, so "white" means "this flip is the
        one carrying that event"."""
        white = any(name in self._armed for name in pending_event_names)
        if self._simulated:
            self.states.append(white)
            return
        # Colour rather than skip-drawing: an undrawn frame would let whatever
        # is behind the patch through and break the two-level signal.
        color = (1.0, 1.0, 1.0) if white else (-1.0, -1.0, -1.0)
        self._stim.fillColor = color
        self._stim.lineColor = color
        self._stim.draw()


def _corner_pos(screen: Screen, corner: Corner, size_px: int) -> tuple[float, float]:
    """Centre of the patch in centered px, flush against its corner."""
    half = size_px / 2.0
    x = screen.width_px / 2.0 - half
    y = screen.height_px / 2.0 - half
    return (
        -x if corner in ("tl", "bl") else x,
        y if corner in ("tl", "tr") else -y,
    )


def make_photodiode(
    display: DisplayBackend, screen: Screen, cfg: PhotodiodeConfig
) -> PhotodiodePatch:
    """Build the patch a rig config describes."""
    return PhotodiodePatch(
        display, screen, corner=cfg.corner, size_px=cfg.size_px, events=cfg.events
    )
