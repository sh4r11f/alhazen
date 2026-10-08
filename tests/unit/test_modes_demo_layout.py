"""Where the demo viewer puts its caption and key table: ``demo_layout``.

Pure geometry, no window. The fixed layout every demo has always had is
pinned to the pixel (three experiments' own tests import its constants and
check their stimuli against them); the layout around a declared stimulus
extent is checked for the property it exists for — no furniture over the
stimulus, nothing off the window, nothing made smaller — and for saying so
when that cannot be had.
"""

from __future__ import annotations

import math

import pytest

from alhazen.display.screen import Screen
from alhazen.modes.demo import (
    BUILT_IN_KEYS,
    CAPTION_LINE_RATIO,
    CAPTION_LINES,
    CAPTION_Y_FRACTION,
    KEYS_HEIGHT_SCALE,
    KEYS_TOGGLE_KEY,
    KEYS_X_FRACTION,
    KEYS_Y_FRACTION,
    RESERVED_KEYS,
    Box,
    DemoControl,
    DemoState,
    DemoView,
    StimulusExtent,
    demo_layout,
    furniture_height,
)

# The two development panels the shared rigs describe: the laptop
# (2560 x 1440, 38 cm wide at 57 cm) and the Mac (3024 x 1964, 30.4 cm at
# 45 cm), as Screen.from_monitor would build them.
_TAN = math.tan(math.radians(1.0))
LAPTOP = Screen(width_px=2560, height_px=1440, px_per_deg=2560 / 38.0 * 57.0 * _TAN)
MAC = Screen(width_px=3024, height_px=1964, px_per_deg=3024 / 30.4 * 45.0 * _TAN)


def view(name, key=None):
    return DemoView(name=name, caption=f"look at {name}", draw=lambda t: None, key=key)


def a_long_table(toggle: str | None = KEYS_TOGGLE_KEY) -> DemoState:
    """A key table the size of a real one: five views, five controls with
    sentence-long labels, the built-ins and the toggle."""
    views = [view(f"display number {n}", str(n)) for n in range(1, 6)]
    controls = [
        DemoControl(k, "hide the red target / the cue: a dot on the side turns red", lambda: None)
        for k in ("t", "f", "n", "minus", "equal")
    ]
    return DemoState(views, controls, keys_toggle=toggle)


def centred(half_w: float, half_h: float) -> StimulusExtent:
    return StimulusExtent(-half_w, -half_h, half_w, half_h)


# ---------------------------------------------------------------------------
# No extent: the layout that has always been
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen", [LAPTOP, MAC], ids=["laptop", "mac"])
def test_without_an_extent_the_layout_is_the_fixed_one_to_the_pixel(screen):
    table = a_long_table(toggle=None).key_table()
    layout = demo_layout(screen, table)

    font = max(15.0, screen.height_px * 0.013)
    assert (layout.caption.x, layout.caption.y) == (0.0, screen.height_px * CAPTION_Y_FRACTION)
    assert layout.caption.height == font * 1.35
    assert layout.caption.anchor == "center"
    assert (layout.keys.x, layout.keys.y) == (
        screen.width_px * KEYS_X_FRACTION,
        screen.height_px * KEYS_Y_FRACTION,
    )
    assert layout.keys.height == font * KEYS_HEIGHT_SCALE
    assert layout.keys.anchor == "left"
    assert layout.keys_shown is True
    assert layout.hint is None and layout.stimulus is None and layout.warnings == ()


def test_the_fixed_constants_have_not_moved():
    """amodal-averaging, attention-clamp and kde-vergence import these to
    check their stimuli against the fixed layout."""
    assert (CAPTION_Y_FRACTION, KEYS_X_FRACTION, KEYS_Y_FRACTION, KEYS_HEIGHT_SCALE) == (
        -0.32,
        -0.47,
        0.47,
        0.85,
    )
    assert frozenset({"right", "space", "left", "s", "escape", "q"}) == RESERVED_KEYS
    assert [key for key, _ in BUILT_IN_KEYS] == ["RIGHT or SPACE", "LEFT", "S", "ESC or Q"]


def test_a_demo_that_declares_nothing_has_no_toggle_and_no_extra_row():
    state = a_long_table(toggle=None)

    assert state.keys_hint() is None
    assert state.reserved_keys() == RESERVED_KEYS
    assert KEYS_TOGGLE_KEY.upper() not in [row.split()[0] for row in state.key_table().splitlines()]
    # H is the experiment's to bind, as it always was.
    bound = DemoState([view("a")], [DemoControl("h", "mine", lambda: "mine")])
    assert bound.press("h") == "continue" and bound.suffix == "mine"
    assert bound.keys_shown is True


# ---------------------------------------------------------------------------
# A stimulus that leaves room: nothing moves
# ---------------------------------------------------------------------------


def test_a_small_stimulus_keeps_the_fixed_places_and_the_table_drawn():
    layout = demo_layout(
        LAPTOP, a_long_table().key_table(), centred(4.0, 3.0), keys_hint=a_long_table().keys_hint()
    )
    fixed = demo_layout(LAPTOP, a_long_table().key_table())

    assert layout.caption == fixed.caption
    assert layout.keys == fixed.keys
    assert layout.keys_shown is True
    assert layout.warnings == ()
    assert not layout.caption.box.overlaps(layout.stimulus)
    assert not layout.keys.box.overlaps(layout.stimulus)


# ---------------------------------------------------------------------------
# A stimulus that fills the window: the furniture moves, the stimulus does not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen", [LAPTOP, MAC], ids=["laptop", "mac"])
def test_a_wide_tall_stimulus_gets_the_caption_below_it_and_the_table_hidden(screen):
    """26 x 14 deg, about kde-vergence's cylinder with its largest dot: on the
    laptop the fixed caption would sit inside it and on both panels the
    table would cover its upper left."""
    state = a_long_table()
    extent = centred(13.34, 7.34)
    layout = demo_layout(screen, state.key_table(), extent, keys_hint=state.keys_hint())

    stimulus = layout.stimulus
    assert stimulus == Box(*(screen.deg2px(v) for v in (-13.34, -7.34, 13.34, 7.34)))
    # The caption: under the stimulus, all of its two lines on the window.
    assert layout.caption.box.top < stimulus.bottom
    assert layout.caption.box.inside(layout.window)
    assert layout.caption.box.bottom >= layout.window.bottom
    # The table would cover the stimulus, so it starts hidden...
    assert layout.keys.box.overlaps(stimulus)
    assert layout.keys_shown is False
    # ...and the hint in its place covers nothing and is on the window.
    assert not layout.hint.box.overlaps(stimulus)
    assert layout.hint.box.inside(layout.window)
    assert layout.warnings == ()


@pytest.mark.parametrize("screen", [LAPTOP, MAC], ids=["laptop", "mac"])
def test_nothing_is_made_smaller_to_fit(screen):
    state = a_long_table()
    fixed = demo_layout(screen, state.key_table())
    layout = demo_layout(
        screen, state.key_table(), centred(13.34, 7.34), keys_hint=state.keys_hint()
    )

    assert (
        layout.caption.height == fixed.caption.height == furniture_height(screen.height_px) * 1.35
    )
    assert layout.keys.height == layout.hint.height == fixed.keys.height
    assert layout.keys.height >= 15.0 * KEYS_HEIGHT_SCALE


def test_the_caption_moves_only_as_far_as_it_has_to():
    """Its usual place when that clears the stimulus (the Mac); flush under
    the stimulus, one clearance down, when it does not (the laptop)."""
    state = a_long_table()
    extent = centred(13.34, 7.34)
    on_mac = demo_layout(MAC, state.key_table(), extent, keys_hint=state.keys_hint())
    on_laptop = demo_layout(LAPTOP, state.key_table(), extent, keys_hint=state.keys_hint())

    assert on_mac.caption.y == MAC.height_px * CAPTION_Y_FRACTION
    clearance = on_laptop.caption.height * 0.5
    assert on_laptop.caption.y == pytest.approx(on_laptop.stimulus.bottom - clearance)
    assert on_laptop.caption.y < LAPTOP.height_px * CAPTION_Y_FRACTION


def test_the_caption_box_holds_two_lines():
    layout = demo_layout(LAPTOP, "K   key", centred(13.34, 7.34), keys_hint="H   show the keys")
    caption = layout.caption
    assert caption.box.top - caption.box.bottom == pytest.approx(
        caption.height * CAPTION_LINE_RATIO * CAPTION_LINES
    )


def test_a_table_that_clears_the_stimulus_is_drawn_even_when_hideable():
    """Hidden only when it has to be: a narrow stimulus leaves the corner."""
    state = a_long_table()
    layout = demo_layout(LAPTOP, state.key_table(), centred(4.0, 7.34), keys_hint=state.keys_hint())
    assert layout.keys_shown is True and layout.warnings == ()


def test_a_table_running_off_the_bottom_is_hidden_too():
    """A short window: the table clears a small stimulus sideways but its
    rows run past the bottom edge."""
    short = Screen(width_px=2560, height_px=200, px_per_deg=67.0)
    state = a_long_table()
    layout = demo_layout(short, state.key_table(), centred(1.0, 1.0), keys_hint=state.keys_hint())
    assert not layout.keys.box.overlaps(layout.stimulus)
    assert not layout.keys.box.inside(layout.window)
    assert layout.keys_shown is False


# ---------------------------------------------------------------------------
# When it cannot fit: said, not hidden
# ---------------------------------------------------------------------------


def test_a_stimulus_with_no_room_below_it_is_warned_about_and_the_caption_stays_on_screen():
    state = a_long_table()
    layout = demo_layout(
        LAPTOP, state.key_table(), centred(13.0, 10.5), keys_hint=state.keys_hint()
    )

    assert layout.caption.box.inside(layout.window)
    assert layout.caption.box.overlaps(layout.stimulus)
    assert any("caption" in w and "overlaps the stimulus by" in w for w in layout.warnings)


def test_a_stimulus_larger_than_the_window_is_warned_about():
    layout = demo_layout(LAPTOP, "K   key", centred(25.0, 5.0), keys_hint="H   show the keys")
    assert any("does not fit" in w for w in layout.warnings)


def test_a_stimulus_over_the_hints_corner_is_warned_about():
    layout = demo_layout(
        LAPTOP, "K   key", StimulusExtent(-19.0, 9.0, -10.0, 10.7), keys_hint="H   show the keys"
    )
    assert any("hint" in w for w in layout.warnings)


def test_a_table_that_cannot_be_hidden_is_drawn_and_warned_about():
    layout = demo_layout(LAPTOP, a_long_table(toggle=None).key_table(), centred(13.34, 7.34))
    assert layout.keys_shown is True
    assert any("cannot hide" in w for w in layout.warnings)


# ---------------------------------------------------------------------------
# The extent itself, checked where it comes in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [(-1, -2, 1, 2), [-1.0, -2.0, 1.0, 2.0]])
def test_four_numbers_are_an_extent(value):
    assert StimulusExtent.parse(value) == StimulusExtent(-1.0, -2.0, 1.0, 2.0)


def test_none_is_no_extent_and_an_extent_is_itself():
    extent = centred(1.0, 1.0)
    assert StimulusExtent.parse(None) is None
    assert StimulusExtent.parse(extent) is extent


@pytest.mark.parametrize("value", [(1, 2, 3), "abcd", 5, {"left": 1}])
def test_anything_else_is_refused_by_shape(value):
    with pytest.raises(TypeError, match="four"):
        StimulusExtent.parse(value)


@pytest.mark.parametrize(
    "value",
    [
        (1, -1, -1, 1),
        (-1, 1, 1, -1),
        (0, 0, 0, 1),
        (-1, float("nan"), 1, 1),
        (-1, -1, float("inf"), 1),
    ],
)
def test_a_box_inside_out_or_not_finite_is_refused(value):
    with pytest.raises(ValueError, match="extent"):
        StimulusExtent.parse(value)


# ---------------------------------------------------------------------------
# The toggle key
# ---------------------------------------------------------------------------


def test_the_toggle_shows_and_hides_the_table_and_leaves_the_rest_alone():
    state = DemoState(
        [view("a", "1"), view("b", "2")],
        [DemoControl("x", "say so", lambda: "said")],
        keys_toggle=KEYS_TOGGLE_KEY,
    )
    state.keys_shown = False
    state.press("x")
    state.press("2")
    state.press("x")

    assert state.press(KEYS_TOGGLE_KEY) == "continue"
    assert state.keys_shown is True
    assert state.press(KEYS_TOGGLE_KEY.upper()) == "continue"
    assert state.keys_shown is False
    # Same view, same caption suffix: the table is not the stimulus.
    assert state.view.name == "b" and state.suffix == "said"


def test_the_toggle_is_listed_and_hinted():
    state = a_long_table()
    last = state.key_table().splitlines()[-1]
    assert last.split()[0] == KEYS_TOGGLE_KEY.upper() and "show / hide" in last
    assert state.keys_hint().split()[0] == KEYS_TOGGLE_KEY.upper()


def test_a_demo_with_the_toggle_refuses_a_binding_of_its_key():
    with pytest.raises(ValueError, match="already owns"):
        DemoState([view("a", KEYS_TOGGLE_KEY)], keys_toggle=KEYS_TOGGLE_KEY)
    with pytest.raises(ValueError, match="already owns"):
        DemoState(
            [view("a")], [DemoControl("H", "help", lambda: None)], keys_toggle=KEYS_TOGGLE_KEY
        )


# ---------------------------------------------------------------------------
# run_demo: what the loop draws, with a stand-in for PsychoPy
# ---------------------------------------------------------------------------


class _FakeText:
    def __init__(self, drawn, window, **kwargs):
        self.kwargs = kwargs
        self.text = kwargs.get("text", "")
        self._drawn = drawn

    def draw(self):
        self._drawn.append(self)


class _FakeDisplay:
    flips = 0

    def __init__(self, monitor, windowed=False):
        self.window = object()

    def open(self):
        pass

    def close(self):
        pass

    def flip(self):
        type(self).flips += 1


def _fake_psychopy(monkeypatch, keys_per_frame, drawn):
    """psychopy.core / event / visual stand-ins: one list of keys per frame,
    then Q. Every TextStim drawn is recorded, frame by frame."""
    import sys
    import types

    from alhazen.display import psychopy_backend

    frames = list(keys_per_frame) + [["q"]]
    per_frame: list[list] = []

    def get_keys():
        per_frame.append(list(drawn))
        drawn.clear()
        return frames.pop(0)

    core = types.SimpleNamespace(Clock=lambda: types.SimpleNamespace(getTime=lambda: 0.0))
    event = types.SimpleNamespace(getKeys=get_keys)
    visual = types.SimpleNamespace(TextStim=lambda window, **kw: _FakeText(drawn, window, **kw))
    package = types.ModuleType("psychopy")
    package.core, package.event, package.visual = core, event, visual
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.core", core)
    monkeypatch.setitem(sys.modules, "psychopy.event", event)
    monkeypatch.setitem(sys.modules, "psychopy.visual", visual)
    monkeypatch.setattr(psychopy_backend, "PsychoPyDisplay", _FakeDisplay)
    return per_frame


def _laptop_rig():
    import types

    monitor = types.SimpleNamespace(width_px=2560, height_px=1440, width_cm=38.0, distance_cm=57.0)
    return types.SimpleNamespace(monitor=monitor)


def _run(monkeypatch, keys, extent):
    from alhazen.modes.demo import run_demo

    drawn: list = []
    per_frame = _fake_psychopy(monkeypatch, keys, drawn)
    said: list[str] = []
    state = a_long_table(toggle=None)
    code = run_demo(
        lambda setup: list(state.views),
        rig=_laptop_rig(),
        params=None,
        controls=lambda setup: list(state.controls),
        extent=extent,
        echo=said.append,
    )
    assert code == 0
    return per_frame, said


def _texts(frame):
    return [stim.text for stim in frame]


def test_run_demo_without_an_extent_draws_the_table_every_frame_where_it_always_was(monkeypatch):
    per_frame, said = _run(monkeypatch, [[], ["h"]], extent=None)

    for frame in per_frame:
        assert len(frame) == 2
        caption, keys = frame
        assert keys.text.splitlines()[-1].startswith("ESC or Q")
        assert keys.kwargs["pos"] == (2560 * KEYS_X_FRACTION, 1440 * KEYS_Y_FRACTION)
        assert caption.kwargs["pos"] == (0.0, 1440 * CAPTION_Y_FRACTION)
    assert not any("WARNING" in line for line in said)


def test_run_demo_with_a_covering_extent_starts_on_the_hint_and_h_brings_the_table(monkeypatch):
    per_frame, said = _run(
        monkeypatch, [[], ["h"], [], ["h"], []], extent=lambda setup: (-13.34, -7.34, 13.34, 7.34)
    )

    hint = f"{KEYS_TOGGLE_KEY.upper()}   show the keys"
    shown = [hint in _texts(frame) for frame in per_frame]
    table = [
        any(t.endswith("show / hide this table") for t in _texts(frame)) for frame in per_frame
    ]
    # Frames: hint, hint (H read after it), table, table, hint, hint.
    assert shown == [True, True, False, False, True, True]
    assert table == [False, False, True, True, False, False]
    # The caption is under the stimulus on every frame.
    stimulus_bottom = LAPTOP.deg2px(-7.34)
    for frame in per_frame:
        caption = frame[0]
        assert caption.kwargs["pos"][1] < stimulus_bottom
    assert any("press H to show it" in line for line in said)


def test_run_demo_refuses_an_extent_of_the_wrong_shape(monkeypatch):
    with pytest.raises(TypeError, match="four"):
        _run(monkeypatch, [], extent=lambda setup: (1.0, 2.0))


def test_the_cli_hands_the_tasks_extent_to_the_viewer(monkeypatch, tmp_path):
    """`--mode demo` passes Task.demo_stimulus_extent, whose default says
    nothing (None), so a task that never declares one keeps the fixed
    layout."""
    from alhazen.cli.modes import run_experiment
    from alhazen.config.models import Model
    from alhazen.core.events import EventSchema
    from alhazen.core.trial import outcomes as make_outcomes
    from alhazen.modes import demo
    from alhazen.task.task import Task

    seen = {}

    def fake_run_demo(setup_views, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(demo, "run_demo", fake_run_demo)

    class P(Model):
        pass

    class Shown(Task):
        name = "shown"
        events = EventSchema(())
        outcomes = make_outcomes(DONE=dict(completed=True, success=True))
        params_model = P

        def demo_views(self, setup):
            return []

    from test_cli_modes import rig_file

    rig = rig_file(tmp_path)
    assert (
        run_experiment(
            task_class=Shown, default_rig=rig, argv=["--mode", "demo", "--task", "shown"]
        )
        == 0
    )
    assert seen["extent"].__func__ is Task.demo_stimulus_extent
    assert seen["extent"](None) is None
