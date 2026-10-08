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

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alhazen.display.backend import DisplayBackend
from alhazen.display.psychopy_backend import HEADING_FONT, MONO_FONT
from alhazen.display.screen import Screen

# The furniture's styling. Two faces on purpose: the caption is prose and gets
# the same humanist sans the pause menu's heading uses, so the viewer and a
# session look like one tool; the key list is a table and gets a monospace
# face so its columns line up. The display registers both faces when it
# opens (display.psychopy_backend) and warns if it cannot, so neither has to
# be installed on the rig and neither is substituted silently.
CAPTION_FONT = HEADING_FONT
KEYS_FONT = MONO_FONT
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
# The key table goes in the TOP-LEFT corner, not stacked above the stimulus.
# Stacked, it competes with the stimulus for vertical space and loses: a
# cylinder 8 degrees tall on a dense panel is 858 px of a 1500 px window,
# leaving 321 px above it, and a table of a dozen rows needs more than that.
# In the corner it uses the horizontal room a centred stimulus does not, and
# the layout stops depending on how many keys an experiment happens to bind.
KEYS_X_FRACTION = -0.47
KEYS_Y_FRACTION = 0.47
# The key table is set smaller than the caption. They are read at different
# moments and for different reasons: the caption is what you read WHILE
# judging the stimulus, so it matches the caption's own size; the key table is
# looked up once and then ignored. Smaller also keeps a dozen rows clear of a
# stimulus that fills the middle of the window.
KEYS_HEIGHT_SCALE = 0.85

# The furniture's text size, before the two blocks' own scales: sized off the
# panel rather than in degrees, so it stays legible whatever display the demo
# opens on, and never under 15 px.
FURNITURE_MIN_PX = 15.0
FURNITURE_HEIGHT_FRACTION = 0.013
CAPTION_HEIGHT_SCALE = 1.35

# How much room a block of text takes, for the layout below to keep it clear
# of the stimulus. Upper bounds, measured in a real PsychoPy window and
# rounded up (tests/unit/test_modes_demo_render.py checks them against the
# rendered text): the height of one rendered line per unit of letter height,
# and the advance of one monospace character per unit of letter height.
CAPTION_LINE_RATIO = 1.5
KEYS_LINE_RATIO = 1.3
MONO_ADVANCE_RATIO = 0.61
# A caption is one line, plus the status a control appends to it; two lines
# is what the layout keeps free for it, so a caption that wraps once still
# clears the stimulus.
CAPTION_LINES = 2
# The clearance kept between a block of furniture and the stimulus, and
# between a block and the window's edge, as a fraction of the caption's
# letter height.
CLEARANCE_SCALE = 0.5

# The key that shows and hides the key table, in a demo whose experiment
# declares where its stimulus is (Task.demo_stimulus_extent). Only then: a
# demo that declares nothing keeps exactly the layout, the keys and the
# reserved names it always had. H for help; no experiment built on alhazen
# binds it (kde-vergence, amodal-averaging, attention-clamp and mbri were
# checked when it was chosen).
KEYS_TOGGLE_KEY = "h"
KEYS_TOGGLE_LABEL = "show / hide this table"
KEYS_HINT_LABEL = "show the keys"


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
# drawn with arrow glyphs: a face renders a glyph it lacks as a hollow box,
# and a key table with tofu in it is worse than one with a few extra words.
# The key names the viewer itself consumes, as the keyboard reports them.
# `press` checks these BEFORE the experiment's own bindings, so an experiment
# that bound one would be silently shadowed — the key table would advertise it
# and something else would happen. That is refused at construction instead;
# see DemoState.__post_init__.
RESERVED_KEYS = frozenset({"right", "space", "left", "s", "escape", "q"})

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
    # Whether S can actually write a file. Listed in the key table only when
    # it can: a viewer that advertises a key which does nothing leaves the
    # reader unable to tell a dead key from a failed save.
    can_screenshot: bool = True
    # Set by a control that wants to say what it just did.
    suffix: str | None = field(default=None)
    # The key that shows and hides the key table, or None for a demo whose
    # table is always drawn (every demo that declares no stimulus extent).
    # When set it is the viewer's own key, reserved like the others, and the
    # table lists it.
    keys_toggle: str | None = None
    # Whether the full key table is drawn now. run_demo sets the default from
    # the layout: hidden when the table would cover the stimulus.
    keys_shown: bool = True

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
        # Against the viewer's own keys as well. press() checks those first,
        # so a binding here would never fire while the key table went on
        # advertising it — a viewer that documents a key it does not have is
        # worse than one with fewer keys, because the table is the only
        # documentation anybody reads.
        taken = {key.lower() for key in keys if key}
        taken |= {control.key.lower() for control in self.controls}
        owned = self.reserved_keys()
        reserved = sorted(taken & owned)
        if reserved:
            raise ValueError(
                f"demo binds key(s) the viewer already owns: {reserved}. "
                f"The viewer keeps {sorted(owned)} for paging, screenshots, "
                f"the key table and quitting, and checks them first, so these "
                f"would never fire."
            )

    def reserved_keys(self) -> frozenset[str]:
        """The keys this viewer consumes before the experiment's bindings."""
        if self.keys_toggle is None:
            return RESERVED_KEYS
        return RESERVED_KEYS | {self.keys_toggle.lower()}

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
        rows += [row for row in BUILT_IN_KEYS if self.can_screenshot or row[0] != "S"]
        if self.keys_toggle is not None:
            rows.append((self.keys_toggle.upper(), KEYS_TOGGLE_LABEL))
        width = max(len(key) for key, _ in rows)
        return "\n".join(f"{key:<{width}}   {label}" for key, label in rows)

    def keys_hint(self) -> str | None:
        """The one line drawn in the table's place while it is hidden, or
        None when this viewer has no way to hide it."""
        if self.keys_toggle is None:
            return None
        return f"{self.keys_toggle.upper()}   {KEYS_HINT_LABEL}"

    def press(self, key: str) -> str:
        """Apply one keypress; returns "quit", "screenshot" or "continue".

        Selecting a view clears the caption suffix, because a suffix describes
        something a control did to the view that was showing, and carrying it
        onto the next one would make it a lie.
        """
        key = key.lower()
        if key in ("escape", "q"):
            return "quit"
        if self.keys_toggle is not None and key == self.keys_toggle.lower():
            # Leaves the caption's suffix alone: showing the keys changes
            # nothing about the stimulus the suffix describes.
            self.keys_shown = not self.keys_shown
            return "continue"
        if key == "s":
            return "screenshot" if self.can_screenshot else "continue"
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


# ---------------------------------------------------------------------------
# Where the furniture goes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StimulusExtent:
    """Where a demo's stimulus draws, in degrees of visual angle from the
    centre of the window, y up: the box every frame of it stays inside.

    An experiment declares it (``Task.demo_stimulus_extent``) so the viewer
    can keep its furniture off the stimulus rather than at fixed fractions of
    the window. It is the stimulus's own box, not every pixel any frame could
    ever reach: a stray dot past the box for a frame is the stimulus's
    business; a caption permanently over it is the viewer's.
    """

    left: float
    bottom: float
    right: float
    top: float

    def __post_init__(self) -> None:
        values = (self.left, self.bottom, self.right, self.top)
        if not all(math.isfinite(v) for v in values):
            raise ValueError(f"a stimulus extent must be four finite numbers, got {values}")
        if not (self.left < self.right and self.bottom < self.top):
            raise ValueError(
                f"a stimulus extent is (left, bottom, right, top) with left < right "
                f"and bottom < top, in degrees from the window's centre; got {values}"
            )

    @classmethod
    def parse(cls, value: Any) -> StimulusExtent | None:
        """What ``Task.demo_stimulus_extent`` returned, checked: None, a
        StimulusExtent, or four numbers (left, bottom, right, top).

        A plain tuple is accepted so an experiment can declare its extent
        without importing this class, and so keep working on an alhazen that
        has neither (which never asks for it).
        """
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, str | bytes) or not isinstance(value, Sequence) or len(value) != 4:
            raise TypeError(
                f"demo_stimulus_extent must return None, a StimulusExtent or four "
                f"numbers (left, bottom, right, top) in degrees; got {value!r}"
            )
        return cls(*(float(v) for v in value))


@dataclass(frozen=True)
class Box:
    """A rectangle in window pixels, from the centre, y up."""

    left: float
    bottom: float
    right: float
    top: float

    def overlaps(self, other: Box) -> bool:
        """True when the two share any area; touching edges do not count."""
        return (
            self.left < other.right
            and other.left < self.right
            and self.bottom < other.top
            and other.bottom < self.top
        )

    def inside(self, other: Box) -> bool:
        return (
            self.left >= other.left
            and self.right <= other.right
            and self.bottom >= other.bottom
            and self.top <= other.top
        )

    def grown(self, by: float) -> Box:
        return Box(self.left - by, self.bottom - by, self.right + by, self.top + by)


@dataclass(frozen=True)
class TextPlacement:
    """One block of furniture: where its top edge is anchored, its letter
    height, and the box it can cover (an upper bound, from the line and
    character ratios above)."""

    x: float
    y: float
    height: float
    anchor: str
    box: Box


@dataclass(frozen=True)
class DemoLayout:
    """Where the viewer draws its caption, its key table and, while the table
    is hidden, the one-line hint that says how to bring it back.

    ``keys_shown`` is whether the table is drawn when the demo opens.
    ``warnings`` says, in words, any way this layout still covers the
    stimulus or leaves the window: the viewer prints them rather than drawing
    over the stimulus without a word.
    """

    caption: TextPlacement
    keys: TextPlacement
    hint: TextPlacement | None
    keys_shown: bool
    stimulus: Box | None
    window: Box
    warnings: tuple[str, ...] = ()


def furniture_height(height_px: float) -> float:
    """The furniture's base letter height on a window this tall, in px."""
    return max(FURNITURE_MIN_PX, height_px * FURNITURE_HEIGHT_FRACTION)


def _block(
    x: float,
    y: float,
    height: float,
    lines: int,
    chars: int,
    *,
    anchor: str,
    line_ratio: float,
    wrap: float | None = None,
) -> TextPlacement:
    """A top-anchored block of ``lines`` lines, the longest ``chars`` wide."""
    width = chars * height * MONO_ADVANCE_RATIO
    if wrap is not None:
        width = wrap
    left = x - width / 2.0 if anchor == "center" else x
    bottom = y - height * line_ratio * lines
    return TextPlacement(
        x=x, y=y, height=height, anchor=anchor, box=Box(left, bottom, left + width, y)
    )


def demo_layout(
    screen: Screen,
    key_table: str,
    extent: StimulusExtent | None = None,
    *,
    keys_hint: str | None = None,
) -> DemoLayout:
    """Place the viewer's caption and key table on this window.

    With no ``extent`` this is the layout every demo has always had, to the
    pixel: the caption's top at ``CAPTION_Y_FRACTION`` of the window's height,
    the key table drawn in the top-left corner, and nothing else.

    With an ``extent`` (degrees, from the window's centre) the stimulus stays
    exactly where and as large as the experiment draws it; the furniture
    moves instead:

    - The caption keeps its usual place if two lines of it clear the stimulus
      there; otherwise it goes up against the stimulus's lower edge, in the
      band between the stimulus and the window's bottom.
    - The key table stays in its corner, drawn, if it clears the stimulus and
      fits on the window. If it does not, it starts hidden and a one-line
      ``keys_hint`` stands in its place; the toggle key shows the full table
      over the stimulus on demand, which is the operator's choice to make,
      not the viewer's.
    - Anything that still covers the stimulus or leaves the window is said in
      ``warnings``. Nothing is made smaller to fit.
    """
    width, height = float(screen.width_px), float(screen.height_px)
    window = Box(-width / 2.0, -height / 2.0, width / 2.0, height / 2.0)
    font = furniture_height(height)
    caption_height = font * CAPTION_HEIGHT_SCALE
    keys_height = font * KEYS_HEIGHT_SCALE
    clearance = caption_height * CLEARANCE_SCALE
    # The caption is centred and wraps at wrapWidth, so its box is that wide.
    caption_wrap = caption_height * 60
    rows = key_table.splitlines() or [""]
    widest = max(len(row) for row in rows)

    keys_x, keys_y = width * KEYS_X_FRACTION, height * KEYS_Y_FRACTION
    keys = _block(
        keys_x, keys_y, keys_height, len(rows), widest, anchor="left", line_ratio=KEYS_LINE_RATIO
    )
    legacy_caption_y = height * CAPTION_Y_FRACTION

    def caption_at(y: float) -> TextPlacement:
        return _block(
            0.0,
            y,
            caption_height,
            CAPTION_LINES,
            0,
            anchor="center",
            line_ratio=CAPTION_LINE_RATIO,
            wrap=caption_wrap,
        )

    if extent is None:
        return DemoLayout(
            caption=caption_at(legacy_caption_y),
            keys=keys,
            hint=None,
            keys_shown=True,
            stimulus=None,
            window=window,
        )

    stimulus = Box(
        screen.deg2px(extent.left),
        screen.deg2px(extent.bottom),
        screen.deg2px(extent.right),
        screen.deg2px(extent.top),
    )
    keep_clear = stimulus.grown(clearance)
    warnings: list[str] = []
    if not stimulus.inside(window):
        warnings.append(
            f"the stimulus ({_describe(stimulus)}) does not fit in the "
            f"{width:.0f} x {height:.0f} px window: the demo shows it cut off"
        )

    # The caption: its usual place if it clears the stimulus there, else the
    # band below the stimulus.
    caption = caption_at(min(legacy_caption_y, keep_clear.bottom))
    if caption.box.bottom < window.bottom:
        # No room below. Keep it on the window, at its bottom, and say what
        # it covers rather than letting it fall off the edge unseen.
        caption = caption_at(window.bottom + caption_height * CAPTION_LINE_RATIO * CAPTION_LINES)
        if caption.box.overlaps(stimulus):
            warnings.append(
                f"no room for the caption between the stimulus and the window's "
                f"bottom: it overlaps the stimulus by "
                f"{caption.box.top - stimulus.bottom:.0f} px"
            )

    hint = None
    if keys_hint is not None:
        hint = _block(
            keys_x,
            keys_y,
            keys_height,
            1,
            len(keys_hint),
            anchor="left",
            line_ratio=KEYS_LINE_RATIO,
        )
    table_fits = keys.box.inside(window) and not keys.box.overlaps(keep_clear)
    keys_shown = table_fits or hint is None
    if not table_fits and hint is None:
        warnings.append("the key table overlaps the stimulus and this viewer cannot hide it")
    if (
        not keys_shown
        and hint is not None
        and (hint.box.overlaps(stimulus) or not hint.box.inside(window))
    ):
        warnings.append("even the one-line key hint overlaps the stimulus or leaves the window")
    return DemoLayout(
        caption=caption,
        keys=keys,
        hint=hint,
        keys_shown=keys_shown,
        stimulus=stimulus,
        window=window,
        warnings=tuple(warnings),
    )


def _describe(box: Box) -> str:
    return f"x {box.left:.0f} to {box.right:.0f} px, y {box.bottom:.0f} to {box.top:.0f} px"


def _text(
    visual: Any,
    window: Any,
    *,
    height: float,
    color: Any,
    align: str,
    y: float,
    font: str,
    x: float = 0.0,
    anchor_h: str = "center",
):
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
        anchorHoriz=anchor_h,
        anchorVert="top",
        pos=(x, y),
        wrapWidth=height * 60,
        units="pix",
    )


def run_demo(
    setup_views: Callable[[DemoSetup], list[DemoView]],
    *,
    rig: Any,
    params: Any,
    controls: Callable[[DemoSetup], list[DemoControl]] | None = None,
    extent: Callable[[DemoSetup], Any] | None = None,
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

    ``extent`` is the task's ``demo_stimulus_extent``: where the stimulus
    draws, so the caption and key table can keep off it (``demo_layout``).
    When it is None or returns None the layout is the fixed one every demo
    has always had.

    Raises DisplayError when the window cannot open, including when PsychoPy
    is not installed in this interpreter (the message names the interpreter
    and what to install into it).
    """
    from pathlib import Path

    from alhazen.display.psychopy_backend import PsychoPyDisplay

    screen = Screen.from_monitor(rig.monitor)
    display = PsychoPyDisplay(rig.monitor, windowed=windowed)
    # Opened BEFORE anything is imported from psychopy here: open() is where
    # a missing PsychoPy becomes the DisplayError that says which interpreter
    # lacks it and what to install. Importing psychopy.core first, as this
    # used to, met the same absence as a raw ModuleNotFoundError traceback —
    # which is what the dashboard's console showed for a project registered
    # with an environment that had alhazen but no PsychoPy.
    display.open()
    try:
        from psychopy import core, event, visual

        setup = DemoSetup(
            display=display, screen=screen, params=params, rng=np.random.default_rng(seed)
        )
        # Views, then controls, then the extent: the order a task can rely on
        # (an experiment's controls may act on state its views built).
        views = setup_views(setup)
        bound = controls(setup) if controls is not None else ()
        declared = StimulusExtent.parse(extent(setup)) if extent is not None else None
        state = DemoState(
            views=views,
            controls=bound,
            can_screenshot=screenshot_dir is not None,
            keys_toggle=KEYS_TOGGLE_KEY if declared is not None else None,
        )
        layout = demo_layout(screen, state.key_table(), declared, keys_hint=state.keys_hint())
        state.keys_shown = layout.keys_shown

        window = display.window

        def block(place: TextPlacement, *, color: Any, align: str, font: str) -> Any:
            return _text(
                visual,
                window,
                height=place.height,
                color=color,
                align=align,
                x=place.x,
                y=place.y,
                anchor_h=place.anchor,
                font=font,
            )

        caption = block(layout.caption, color=CAPTION_COLOR, align="center", font=CAPTION_FONT)
        keys = block(layout.keys, color=KEYS_COLOR, align="left", font=KEYS_FONT)
        keys.text = state.key_table()
        hint = None
        if layout.hint is not None:
            hint = block(layout.hint, color=KEYS_COLOR, align="left", font=KEYS_FONT)
            hint.text = state.keys_hint()
        echo(state.key_table())
        for warning in layout.warnings:
            echo(f"WARNING: {warning}")
        if not state.keys_shown:
            echo(
                f"the key table would cover the stimulus, so it starts hidden: "
                f"press {KEYS_TOGGLE_KEY.upper()} to show it"
            )

        clock = core.Clock()
        started = clock.getTime()
        shots = 0
        while True:
            state.view.draw(clock.getTime() - started)
            caption.text = state.caption()
            caption.draw()
            if state.keys_shown:
                keys.draw()
            elif hint is not None:
                hint.draw()
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
