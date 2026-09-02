"""The calibration guide's text: what the experimenter reads before the
first target appears, tested without a display or a tracker."""

from __future__ import annotations

import pytest

from alhazen.devices.eyetracker.guide import (
    ADVANCE_LINES,
    GUIDE_TITLE,
    calibration_guide,
)

KEYS = [("SPACE", "accept the target"), ("BACKSPACE", "redo the previous target"), ("ESC", "abort")]


def guide(**overrides: object) -> str:
    """A guide with sensible facts, any of which a test can override."""
    facts: dict[str, object] = {
        "tracker": "TRACKPixx3",
        "eye": "LEFT eye read by the session; both eyes are calibrated",
        "layout": "HV9",
        "n_targets": 9,
        "area": 0.8,
        "advance": "manual",
        "keys": KEYS,
    }
    facts.update(overrides)
    return calibration_guide(**facts)  # type: ignore[arg-type]


class TestFacts:
    def test_the_title_is_the_same_for_every_backend(self) -> None:
        assert GUIDE_TITLE == "CALIBRATION"

    def test_every_fact_has_its_own_line(self) -> None:
        lines = guide().split("\n")
        assert lines[0] == "tracker   TRACKPixx3"
        assert lines[1] == "eye       LEFT eye read by the session; both eyes are calibrated"
        assert lines[2] == "targets   HV9 — 9 targets over 80% of the screen, centre first"
        assert lines[3] == "advance   " + ADVANCE_LINES["manual"]

    def test_the_area_is_a_whole_percentage(self) -> None:
        assert "over 65% of the screen" in guide(area=0.65)
        assert "over 100% of the screen" in guide(area=1.0)

    def test_the_two_advance_modes_read_differently(self) -> None:
        assert "MANUAL — press SPACE" in guide(advance="manual")
        assert "AUTO — each target is accepted by itself" in guide(advance="auto")

    def test_an_unknown_advance_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown calibration advance mode 'automatic'"):
            guide(advance="automatic")


class TestKeys:
    def test_the_keys_form_an_aligned_column_after_a_keys_heading(self) -> None:
        lines = guide().split("\n")
        keys_at = lines.index("keys")
        # The heading is separated from the facts by a blank line.
        assert lines[keys_at - 1] == ""
        rows = lines[keys_at + 1 : keys_at + 1 + len(KEYS)]
        # Every key is padded to the longest one ("BACKSPACE"), so the labels
        # all start in the same column.
        assert rows == [
            "SPACE       accept the target",
            "BACKSPACE   redo the previous target",
            "ESC         abort",
        ]

    def test_no_keys_still_leaves_the_heading(self) -> None:
        lines = guide(keys=[]).split("\n")
        keys_at = lines.index("keys")
        assert lines[keys_at + 1] == ""


class TestStatusAndStart:
    def test_the_start_line_closes_the_guide(self) -> None:
        lines = guide().split("\n")
        assert lines[-2] == ""
        assert lines[-1] == "press SPACE to start, ESC to abort"

    def test_a_backend_can_say_how_to_start_in_its_own_words(self) -> None:
        assert guide(start_line="press SPACE to start, ESC to skip").endswith(
            "\npress SPACE to start, ESC to skip"
        )

    def test_the_live_status_sits_between_the_keys_and_the_start_line(self) -> None:
        lines = guide(status="eyes: both tracked").split("\n")
        assert lines[-4:] == ["", "eyes: both tracked", "", "press SPACE to start, ESC to abort"]

    def test_without_a_status_there_is_no_empty_slot_for_it(self) -> None:
        lines = guide().split("\n")
        assert lines[-3] == "ESC         abort"
        assert "eyes:" not in guide()
