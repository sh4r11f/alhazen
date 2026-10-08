"""The demo layout's text-size bounds, held against text PsychoPy really draws.

``demo_layout`` keeps furniture off a stimulus using upper bounds on how much
room text takes (CAPTION_LINE_RATIO, KEYS_LINE_RATIO, MONO_ADVANCE_RATIO).
Bounds that were too small would put the caption on the stimulus with every
pure test still passing, so this draws the real faces at the real sizes and
measures them. Needs a window: run with ``-m display`` (Xvfb will do).
"""

from __future__ import annotations

import pytest

from alhazen.modes.demo import (
    CAPTION_FONT,
    CAPTION_HEIGHT_SCALE,
    CAPTION_LINE_RATIO,
    KEYS_FONT,
    KEYS_HEIGHT_SCALE,
    KEYS_LINE_RATIO,
    MONO_ADVANCE_RATIO,
    _text,
    furniture_height,
)

pytestmark = pytest.mark.display

TABLE = "\n".join(
    [f"{n}       display number {n}" for n in range(1, 7)]
    + ["T       hide the red target / the cue: a dot on the side turns red"]
    + [
        "MINUS   slower",
        "EQUAL   faster",
        "RIGHT or SPACE   next display",
        "LEFT   previous display",
        "S   save a screenshot",
        "ESC or Q   quit",
        "H   show / hide this table",
    ]
)
CAPTION = "CUED (the experiment), spin +1 — size cue: front surface moves RIGHT     spin +1 (Qq)"


@pytest.fixture(scope="module")
def window():
    # The display marker means PsychoPy and a screen are here: a missing one
    # is a failure of the run, not a reason to skip.
    import pyglet.font
    from psychopy import visual

    from alhazen.display.psychopy_backend import _bundled_font_files

    for face, path in _bundled_font_files().items():
        if not pyglet.font.have_font(face) and path.is_file():
            pyglet.font.add_file(str(path))
    win = visual.Window(
        size=(2560, 1440), units="pix", fullscr=False, color=(-1, -1, -1), checkTiming=False
    )
    yield visual, win
    win.close()


@pytest.mark.parametrize("panel_height", [1440, 1964])
def test_a_caption_line_is_within_its_bound(window, panel_height):
    visual, win = window
    height = furniture_height(panel_height) * CAPTION_HEIGHT_SCALE
    stim = _text(
        visual, win, height=height, color=(1, 1, 1), align="center", y=0.0, font=CAPTION_FONT
    )
    stim.text = CAPTION
    _, rendered_h = stim.boundingBox
    assert rendered_h <= height * CAPTION_LINE_RATIO
    # And one that wraps onto a second line stays inside two.
    stim.text = CAPTION + " " + CAPTION
    _, wrapped_h = stim.boundingBox
    assert height * CAPTION_LINE_RATIO < wrapped_h <= 2 * height * CAPTION_LINE_RATIO


@pytest.mark.parametrize("panel_height", [1440, 1964])
def test_the_key_table_is_within_its_bounds(window, panel_height):
    visual, win = window
    height = furniture_height(panel_height) * KEYS_HEIGHT_SCALE
    stim = _text(
        visual,
        win,
        height=height,
        color=(1, 1, 1),
        align="left",
        y=0.0,
        font=KEYS_FONT,
        anchor_h="left",
    )
    stim.text = TABLE
    rendered_w, rendered_h = stim.boundingBox
    rows = TABLE.splitlines()
    assert rendered_h <= len(rows) * height * KEYS_LINE_RATIO
    assert rendered_w <= max(len(row) for row in rows) * height * MONO_ADVANCE_RATIO
