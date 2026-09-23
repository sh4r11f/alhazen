"""``alhazen.display.reflow``: hard-wrapped prose joined into paragraphs.

Pure string work, so every rule is pinned here with no display at all. The
PsychoPy backend's use of it is in test_display.py (TestMessageReflow).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alhazen.display import reflow

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


class TestProseIsJoined:
    def test_a_hard_wrapped_paragraph_becomes_one_line(self):
        text = "Move the cursor onto the central\nfixation point and keep it\nthere."
        assert reflow(text) == "Move the cursor onto the central fixation point and keep it there."

    def test_a_blank_line_separates_paragraphs(self):
        text = "First paragraph,\nwrapped.\n\nSecond paragraph,\nalso wrapped."
        assert reflow(text) == "First paragraph, wrapped.\n\nSecond paragraph, also wrapped."

    def test_text_without_newlines_is_unchanged(self):
        assert reflow("stage: 2") == "stage: 2"

    def test_spaces_inside_a_line_are_left_alone(self):
        """Runs of spaces are how a column is aligned, so only the line
        break itself is replaced."""
        assert reflow("a  b\nc   d") == "a  b c   d"


class TestLineEndings:
    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
    def test_every_line_ending_reflows_the_same(self, newline):
        """A file written on Windows and read in binary, or pasted from an
        old Mac editor, must not keep stray carriage returns in the text."""
        text = newline.join(["one", "two", "", "three"])
        assert reflow(text) == "one two\n\nthree"

    def test_mixed_line_endings(self):
        assert reflow("one\r\ntwo\nthree\r\rfour") == "one two three\n\nfour"


class TestBlankLinesAndOuterWhitespace:
    def test_a_run_of_blank_lines_is_one_paragraph_break(self):
        assert reflow("one\n\n\n\ntwo") == "one\n\ntwo"

    def test_a_whitespace_only_line_is_blank(self):
        """An editor that indents empty lines leaves spaces or tabs on them;
        they still separate paragraphs rather than being an indented line."""
        assert reflow("one\n   \t\ntwo") == "one\n\ntwo"

    def test_blank_lines_before_and_after_are_dropped(self):
        assert reflow("\n\n  \nHello\nthere.\n\n\n") == "Hello there."

    def test_trailing_whitespace_is_dropped_from_each_line(self):
        """Otherwise joining 'word   ' to the next line leaves a run of
        spaces in the middle of a sentence."""
        assert reflow("one   \ntwo\t\n\nthree  ") == "one two\n\nthree"

    @pytest.mark.parametrize("text", ["", "\n", "  \n\t\r\n"])
    def test_nothing_but_whitespace_reflows_to_nothing(self, text):
        assert reflow(text) == ""


class TestLaidOutLinesKeepTheirBreaks:
    def test_an_indented_line_keeps_its_break_and_indentation(self):
        text = "Keys:\n  SPACE   start\n  ESC     skip the trial"
        assert reflow(text) == text

    def test_a_tab_indented_line_keeps_its_break(self):
        assert reflow("Keys:\n\tSPACE\tstart") == "Keys:\n\tSPACE\tstart"

    def test_an_indented_first_line_keeps_its_indentation(self):
        assert reflow("    quoted\n    block") == "    quoted\n    block"

    @pytest.mark.parametrize("marker", ["-", "*", "+", "•", "1.", "12.", "3)"])
    def test_a_list_item_keeps_its_break(self, marker):
        text = f"Remember:\n{marker} fixate\n{marker} respond"
        assert reflow(text) == text

    def test_a_list_item_continues_onto_an_unindented_line(self):
        """A list item wrapped by the editor: the continuation is plain text,
        so it joins the item it continues, as Markdown reads it."""
        text = "- Press SPACE when you\nsee the target.\n- Press ESC to skip."
        assert reflow(text) == "- Press SPACE when you see the target.\n- Press ESC to skip."

    def test_prose_after_an_indented_line_joins_it(self):
        """The rule looks at each line on its own: a plain line joins
        whatever line precedes it. Prose meant to follow a table as its own
        paragraph needs the blank line a paragraph always needs."""
        assert reflow("  row one\nmore") == "  row one more"

    @pytest.mark.parametrize(
        "line",
        ["-5 degrees to the left.", "*very* carefully.", "1.5 seconds later.", "+3 dB louder."],
    )
    def test_a_marker_character_without_a_space_is_prose(self, line):
        """A list marker is a marker only when a space follows it, so prose
        that merely starts with one of those characters is still joined."""
        assert reflow(f"Move the target\n{line}") == f"Move the target {line}"

    def test_a_bare_marker_line_is_a_list_item(self):
        assert reflow("Items:\n-\n- two") == "Items:\n-\n- two"


class TestItIsSafeToApplyTwice:
    @pytest.mark.parametrize(
        "text",
        [
            "a\nb\n\n\nc",
            "Keys:\n  SPACE start\n- item\nwrapped\r\n\r\nend  ",
            "\n\n1. one\n2. two\n",
        ],
    )
    def test_reflowing_reflowed_text_changes_nothing(self, text):
        """The runner, a backend and an experiment may each reflow the same
        text; the result must not depend on how many of them did."""
        once = reflow(text)
        assert reflow(once) == once


class TestTheExampleInstructions:
    """The instructions every example ships, as hard-wrapped Markdown: after
    reflow, each paragraph reaches the display as one line. This is the check
    experiment packages kept their own copies of."""

    @pytest.mark.parametrize(
        "path", sorted(EXAMPLES.glob("*/instructions.md")), ids=lambda p: p.parent.name
    )
    def test_each_paragraph_is_one_line(self, path):
        source = path.read_text(encoding="utf-8")
        paragraphs = [p for p in source.replace("\r\n", "\n").split("\n\n") if p.strip()]
        reflowed = reflow(source).split("\n\n")
        assert len(reflowed) == len(paragraphs)
        assert all("\n" not in paragraph for paragraph in reflowed)
        # Nothing is lost on the way: the same words, in the same order.
        assert " ".join(reflowed).split() == source.split()

    def test_there_are_examples_to_check(self):
        """A moved examples directory must fail this, not quietly skip it."""
        assert len(list(EXAMPLES.glob("*/instructions.md"))) >= 5
