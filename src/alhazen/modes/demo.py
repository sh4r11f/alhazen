"""Demo mode: look at the stimulus, with nothing else running.

The stimulus is the one part of an experiment no test can check. A test can
assert that dot *k* sits where the formula says; it cannot assert that a human
sees a transparent cylinder, that the percept flips on its own, or that an
illusory strip appears at one alignment and not at another. Those questions
have to be answered by looking, and they have to be answered before a subject
is asked about them.

Both experiments alhazen was built for wrote their own viewer for this, and
the two were the same program twice: open a window from the rig config, draw
some furniture (a caption and a key table), loop on keys, quit on Q. What
differed was the pixels — which is the experiment's own business, and the only
part it should have to write.

One thing the shared version fixes for both. Each viewer opened its own
``visual.Window`` directly, which means neither got the checks a session gets:
a demo on a Retina Mac showed the stimulus at half size and said nothing,
which is precisely the machine you are most likely to be judging a stimulus
on. Here the window comes from the same ``PsychoPyDisplay`` a session opens,
so the framebuffer check, the monitor registration and the measured gamma all
apply. What you look at is what a subject would see.

The state machine (which view is showing, what the caption says, what a key
does) is separate from the drawing loop and is unit-tested directly, so the
part with the logic in it does not need a renderer to be checked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen

# The furniture's styling. Two faces on purpose: the caption is prose and gets
# the same humanist sans a session's messages use, so the viewer and a session
# look like one tool; the key list is a table and gets a monospace face so its
# columns line up. DejaVu Sans Mono is on every desktop Linux and ships with
# matplotlib, so no rig has to install anything.
CAPTION_FONT = "Open Sans"
KEYS_FONT = "DejaVu Sans Mono"
CAPTION_COLOR = (0.88, 0.88, 0.92)
KEYS_COLOR = (0.55, 0.55, 0.60)

# Where the two blocks sit, as a fraction of the window's height from centre.
# Fractions rather than degrees because this is operator furniture, not
# stimulus: it has to clear a stimulus that may be most of the window tall and
# still be on screen. An earlier viewer placed its key list 6.2 deg down,
# which on a 1200 px window is 65 px BELOW the bottom edge — drawn every frame
# and never once visible.
#
# The caption goes under the stimulus (what you are looking at sits below the
# thing you are judging) and the key table at the top, out of the way.
CAPTION_Y_FRACTION = -0.32
KEYS_Y_FRACTION = 0.38


@dataclass(frozen=True)
class DemoSetup:
    """What an experiment needs to build its views: the same window and pixel
    scale a session would draw into."""

    display: DisplayBackend
    screen: Screen
    params: Any
    rng: np.random.Generator


@dataclass
class DemoView:
    """One thing to look at.

    ``draw`` is called once per frame with the seconds elapsed since this view
    was selected, so an animated stimulus restarts cleanly each time it is
    chosen rather than resuming mid-cycle from whenever it was last on screen.
    """

    name: str
    caption: str
    draw: Callable[[float], None]
    # A key that jumps straight here. Optional: with none, the view is only
    # reachable by stepping through with the arrow keys.
    key: str | None = None


@dataclass
class DemoControl:
    """An experiment-specific key: a new seed, a faster spin, a toggle.

    ``action`` returns the caption suffix to show after it (or None to leave
    the caption alone), so a control that changes something invisible — a
    random seed — can still say that it did.
    """

    key: str
    label: str
    action: Callable[[], str | None]


# The keys every demo has, whatever it is showing. Spelled out rather than
# drawn with arrow glyphs: Open Sans renders a missing glyph as a hollow box,
# and a key table with tofu in it is worse than one with a few extra words.
BUILT_IN_KEYS = (
    ("RIGHT or SPACE", "next display"),
    ("LEFT", "previous display"),
    ("S", "save a screenshot"),
    ("ESC or Q", "quit"),
)


@dataclass
class DemoState:
    """Which view is showing and what the caption says.

    Pure logic, no renderer: this is where the behaviour that could be wrong
    lives, so it is the part that is unit-tested.
    """

    views: Sequence[DemoView]
    controls: Sequence[DemoControl] = ()
    index: int = 0
    # Set by a control that wants to say what it just did.
    suffix: str | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.views:
            raise ValueError("a demo needs at least one view")
        keys = [view.key for view in self.views if view.key]
        clashes = {key for key in keys if keys.count(key) > 1}
        clashes |= {c.key for c in self.controls} & set(keys)
        if clashes:
            raise ValueError(
                f"demo keys are bound twice: {sorted(clashes)}. One key, one thing — "
                f"a viewer whose key does two things cannot be used to judge either."
            )

    @property
    def view(self) -> DemoView:
        return self.views[self.index]

    def caption(self) -> str:
        """The line under the stimulus: what this is, and anything a control
        has changed since."""
        text = f"{self.view.name} — {self.view.caption}"
        return f"{text}     {self.suffix}" if self.suffix else text

    def key_table(self) -> str:
        """The reference block, aligned into columns.

        Built from the views and controls that actually exist, so it cannot
        list a key the viewer does not have.
        """
        rows = [(view.key.upper(), view.name) for view in self.views if view.key]
        rows += [(control.key.upper(), control.label) for control in self.controls]
        rows += list(BUILT_IN_KEYS)
        width = max(len(key) for key, _ in rows)
        return "\n".join(f"{key:<{width}}   {label}" for key, label in rows)

    def press(self, key: str) -> str:
        """Apply one keypress; returns "quit", "screenshot" or "continue".

        Selecting a view clears the caption suffix, because a suffix describes
        something a control did to the view that was showing, and carrying it
        onto the next one would make it a lie.
        """
        key = key.lower()
        if key in ("escape", "q"):
            return "quit"
        if key == "s":
            return "screenshot"
        if key in ("right", "space"):
            self.select((self.index + 1) % len(self.views))
        elif key == "left":
            self.select((self.index - 1) % len(self.views))
        else:
            for position, view in enumerate(self.views):
                if view.key and view.key.lower() == key:
                    self.select(position)
                    return "continue"
            for control in self.controls:
                if control.key.lower() == key:
                    self.suffix = control.action()
                    return "continue"
        return "continue"

    def select(self, index: int) -> None:
        self.index = index
        self.suffix = None


def _text(visual: Any, window: Any, *, height: float, color: Any, align: str, y: float, font: str):
    """One furniture block.

    ``wrapWidth`` is set explicitly because TextStim's default in pixel units
    is far narrower than any of this text, so an eight-row key table wraps
    into a ragged mess. It is a multiple of the text height rather than a
    fraction of the window: 80% of an ultrawide is one enormous line.
    """
    return visual.TextStim(
        window,
        text="",
        font=font,
        height=height,
        color=color,
        colorSpace="rgb",
        alignText=align,
        anchorHoriz="center",
        anchorVert="top",
        pos=(0.0, y),
        wrapWidth=height * 60,
        units="pix",
    )


def run_demo(
    setup_views: Callable[[DemoSetup], list[DemoView]],
    *,
    rig: Any,
    params: Any,
    controls: Callable[[DemoSetup], list[DemoControl]] | None = None,
    seed: int = 0,
    windowed: bool = False,
    screenshot_dir: Any = None,
    echo: Callable[[str], None] = print,
) -> int:
    """Open a window, draw the views, and read the keyboard until Q.

    The window comes from ``PsychoPyDisplay``, not from ``visual.Window``
    directly, so a demo inherits every check a session gets — most usefully
    the framebuffer check, since the Retina Mac it catches is exactly the
    machine a stimulus is most often judged on.
    """
    from pathlib import Path

    from psychopy import core, event

    from alhazen.display.psychopy_backend import PsychoPyDisplay

    screen = Screen.from_monitor(rig.monitor)
    display = PsychoPyDisplay(rig.monitor, windowed=windowed)
    display.open()
    try:
        from psychopy import visual

        setup = DemoSetup(
            display=display, screen=screen, params=params, rng=np.random.default_rng(seed)
        )
        state = DemoState(
            views=setup_views(setup),
            controls=controls(setup) if controls is not None else (),
        )

        window = display.window
        # Sized off the panel rather than in degrees: furniture has to stay
        # legible whatever display this opens on.
        height = max(15.0, rig.monitor.height_px * 0.013)
        caption = _text(
            visual,
            window,
            height=height * 1.35,
            color=CAPTION_COLOR,
            align="center",
            y=rig.monitor.height_px * CAPTION_Y_FRACTION,
            font=CAPTION_FONT,
        )
        keys = _text(
            visual,
            window,
            height=height,
            color=KEYS_COLOR,
            align="left",
            y=rig.monitor.height_px * KEYS_Y_FRACTION,
            font=KEYS_FONT,
        )
        keys.text = state.key_table()
        echo(state.key_table())

        clock = core.Clock()
        started = clock.getTime()
        shots = 0
        while True:
            state.view.draw(clock.getTime() - started)
            caption.text = state.caption()
            caption.draw()
            keys.draw()
            display.flip()

            for key in event.getKeys():
                before = state.index
                action = state.press(key)
                if action == "quit":
                    return 0
                if action == "screenshot":
                    if screenshot_dir is None:
                        echo("no --screenshots directory given, so S does nothing")
                        continue
                    out = Path(screenshot_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    shots += 1
                    path = out / f"{state.view.name}-{shots:02d}.png"
                    window.getMovieFrame()
                    window.saveMovieFrames(str(path))
                    echo(f"written: {path}")
                elif state.index != before:
                    # Restart the clock so an animated view begins at its
                    # start rather than resuming mid-cycle.
                    started = clock.getTime()
    finally:
        display.close()
