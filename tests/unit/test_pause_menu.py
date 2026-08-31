"""The pause screen: what it offers, what it refuses to offer, and the colour.

The menu's whole job is to be believed — an experimenter reads it and presses
what it says. So the cases that matter are the ones where it could lie: a key
listed for hardware that is not there, a key that is listed but does nothing,
and a rebound key still showing its old binding.
"""

from __future__ import annotations

import re

import pytest

from alhazen.core.commands import Command
from alhazen.session.pause import (
    FAULT_COLOR,
    PAUSE_COLOR,
    build_pause_menu,
    key_label,
    run_pause_menu,
)


class TestOnlyOffersWhatTheSessionHas:
    def test_a_bare_session_offers_only_resume_and_quit(self):
        menu = build_pause_menu()

        assert menu.actions() == {"SPACE": "resume", "Q or ESC": "quit"}

    @pytest.mark.parametrize(
        ("wiring", "action"),
        [
            ({"has_tracker": True}, "calibrate"),
            ({"has_reward": True}, "manual_reward"),
            ({"has_training": True}, "promote_stage"),
        ],
    )
    def test_each_component_adds_its_own_action(self, wiring, action):
        assert action in build_pause_menu(**wiring).actions().values()
        assert action not in build_pause_menu().actions().values()

    def test_a_missing_component_is_not_listed_even_as_reference(self):
        """The reward key is real and bound, but on a rig with no pump it does
        nothing. Listing it would leave the experimenter unable to tell a dead
        key from a dead pump."""
        rendered = build_pause_menu().render()

        assert "reward" not in rendered.lower()
        assert "training stage" not in rendered.lower()

    def test_the_reference_group_never_repeats_an_offered_action(self):
        """Same key, same effect, two rows would suggest the presses differ."""
        menu = build_pause_menu(has_reward=True, has_training=True)
        offered = {item.action for item in menu.now}

        assert offered  # guard: the assertion below is vacuous if this is empty
        for item in menu.in_trial:
            assert item.label not in [row.label for row in menu.now]
        # ESC survives, because from a trial it abandons the trial and from
        # here it ends the session — the one genuinely two-meaning key.
        assert any("abandon this trial" in item.label for item in menu.in_trial)


class TestItMatchesTheKeyboard:
    def test_a_rebound_key_is_shown_with_its_new_binding(self):
        menu = build_pause_menu(keymap={"x": Command.SKIP_TRIAL, "p": Command.PAUSE})

        assert any(item.key == "X" for item in menu.in_trial)
        assert not any(item.key == "ESC" for item in menu.in_trial)

    @pytest.mark.parametrize(
        ("key", "shown"),
        [("space", "SPACE"), ("escape", "ESC"), ("bracketright", "]"), ("h", "H")],
    )
    def test_keys_are_spelled_for_a_human(self, key, shown):
        assert key_label(key) == shown


class TestTheColourSaysWhatKindOfStopThisIs:
    def test_a_deliberate_pause_is_orange(self):
        assert build_pause_menu().color == PAUSE_COLOR

    def test_a_fault_gets_its_own_colour_and_leads_with_the_reason(self):
        menu = build_pause_menu(fault="REWARD FAILURE — check the pump")

        assert menu.color == FAULT_COLOR
        assert menu.title == "REWARD FAILURE — check the pump"
        # Same controls underneath: a fault still has to be resumable.
        assert "resume" in menu.actions().values()


class TestTheLoop:
    def _keys(self, *batches):
        queue = list(batches)
        return lambda: queue.pop(0) if queue else []

    @pytest.mark.parametrize(
        ("key", "action"),
        [("space", "resume"), ("q", "quit"), ("escape", "quit"), ("c", "calibrate")],
    )
    def test_a_key_returns_its_action(self, key, action):
        menu = build_pause_menu(has_tracker=True)

        chosen = run_pause_menu(menu, lambda _m: None, self._keys([], [key]), lambda _s: None)

        assert chosen == action

    def test_an_unlisted_key_is_ignored_rather_than_acted_on(self):
        """A rig with no pump: R is bound, but pressing it here must not
        return an action the caller would then try to perform."""
        menu = build_pause_menu()

        chosen = run_pause_menu(
            menu, lambda _m: None, self._keys(["r"], ["z"], ["space"]), lambda _s: None
        )

        assert chosen == "resume"

    def test_the_menu_is_shown_before_any_key_is_read(self):
        shown: list = []
        menu = build_pause_menu()

        run_pause_menu(menu, shown.append, self._keys(["space"]), lambda _s: None)

        assert shown == [menu]


class TestRender:
    def test_the_key_column_is_aligned(self):
        """The body is drawn in a monospace face precisely so this alignment
        survives to the screen; unaligned, eight rows read as a paragraph."""
        rows = [
            line
            for line in build_pause_menu(has_tracker=True, has_reward=True).render().splitlines()
            if line.startswith(("SPACE", "C ", "R ", "Q or ESC"))
        ]

        assert len(rows) == 4
        # Where the label starts: the first non-space after the run of spaces
        # that follows the key.
        starts = {re.match(r"\S+(?: \S+)*\s+", line).end() for line in rows}
        assert len(starts) == 1, f"labels start at different columns: {starts}"
