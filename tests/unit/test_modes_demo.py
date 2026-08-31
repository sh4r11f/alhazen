"""The demo viewer's state machine, without a renderer.

The drawing loop needs psychopy and a screen; everything that could be WRONG
— which view a key selects, what the caption claims you are looking at, what
the key table promises — does not, and is checked here.
"""

from __future__ import annotations

import re

import pytest

from alhazen.modes.demo import DemoControl, DemoState, DemoView


def view(name, key=None):
    return DemoView(name=name, caption=f"look at {name}", draw=lambda t: None, key=key)


def state(*names_and_keys, controls=()):
    return DemoState([view(n, k) for n, k in names_and_keys], controls)


class TestSteppingThroughViews:
    def test_it_starts_on_the_first_view(self):
        assert state(("a", None), ("b", None)).view.name == "a"

    @pytest.mark.parametrize("key", ["right", "space"])
    def test_it_advances(self, key):
        s = state(("a", None), ("b", None))

        s.press(key)

        assert s.view.name == "b"

    def test_it_wraps_at_both_ends(self):
        s = state(("a", None), ("b", None))

        s.press("left")
        assert s.view.name == "b"
        s.press("right")
        assert s.view.name == "a"

    def test_a_views_own_key_jumps_straight_to_it(self):
        s = state(("a", "1"), ("b", "2"), ("c", "3"))

        s.press("3")

        assert s.view.name == "c"

    def test_an_unbound_key_does_nothing(self):
        s = state(("a", "1"), ("b", "2"))

        assert s.press("z") == "continue"
        assert s.view.name == "a"


class TestQuitAndScreenshot:
    @pytest.mark.parametrize("key", ["escape", "q", "Q", "ESCAPE"])
    def test_quit_keys(self, key):
        assert state(("a", None)).press(key) == "quit"

    def test_s_asks_for_a_screenshot_without_changing_the_view(self):
        s = state(("a", None), ("b", None))

        assert s.press("s") == "screenshot"
        assert s.view.name == "a"


class TestControls:
    def test_a_control_runs_and_its_note_reaches_the_caption(self):
        calls = []
        control = DemoControl("n", "a new cloud", lambda: calls.append(1) or "new seed")
        s = state(("a", None), controls=[control])

        s.press("n")

        assert calls == [1]
        assert "new seed" in s.caption()

    def test_a_control_that_reports_nothing_leaves_the_caption_alone(self):
        s = state(("a", None), controls=[DemoControl("t", "toggle", lambda: None)])
        before = s.caption()

        s.press("t")

        assert s.caption() == before

    def test_changing_view_clears_a_controls_note(self):
        """The note describes what a control did to the view that was
        showing; carried onto the next one it would be a lie."""
        s = state(("a", None), ("b", None), controls=[DemoControl("n", "new", lambda: "new seed")])

        s.press("n")
        s.press("right")

        assert "new seed" not in s.caption()


class TestItCannotPromiseAKeyItDoesNotHave:
    def test_the_table_lists_every_view_key_and_control(self):
        table = state(
            ("a", "1"), ("b", "2"), controls=[DemoControl("n", "a new cloud", lambda: None)]
        ).key_table()

        assert "1" in table and "2" in table and "N" in table
        assert "a new cloud" in table

    def test_the_table_always_lists_the_built_in_keys(self):
        table = state(("a", None)).key_table()

        assert "quit" in table and "next display" in table and "screenshot" in table

    def test_a_view_with_no_key_contributes_no_row(self):
        table = state(("only", None)).key_table()

        assert "only" not in table

    def test_the_columns_are_aligned(self):
        """Drawn in a monospace face precisely so this survives to screen."""
        rows = state(("a", "1"), ("bbbb", "2")).key_table().splitlines()

        # Where each label begins: past the key text and the padding after it.
        starts = {re.match(r"\S+(?: \S+)*\s+", row).end() for row in rows}

        assert len(starts) == 1, f"labels start at different columns: {starts}"


class TestItRefusesAmbiguousBindings:
    def test_two_views_on_one_key(self):
        with pytest.raises(ValueError, match="bound twice"):
            state(("a", "1"), ("b", "1"))

    def test_a_control_stealing_a_views_key(self):
        with pytest.raises(ValueError, match="bound twice"):
            state(("a", "1"), controls=[DemoControl("1", "clash", lambda: None)])

    def test_a_demo_with_no_views_at_all(self):
        with pytest.raises(ValueError, match="at least one view"):
            DemoState([])
