"""Text preparation for what a display draws — pure string work, no renderer.

``reflow`` is here rather than inside the PsychoPy backend so it can be tested
with no display, used by every backend alike, and called by an experiment
that wants to see what its instructions will look like on screen.
"""

from __future__ import annotations

import re

# A line that opens a list item: a bullet (-, *, + or •) or a number followed
# by "." or ")" — and then whitespace or the end of the line. The whitespace is
# required so that prose which merely starts with one of those characters
# ("-5 degrees", "*very* important", "1.5 seconds later") is not taken for a
# list and keeps being joined like any other line.
_LIST_ITEM = re.compile(r"(?:[-*+•]|\d+[.)])(?:\s|$)")


def reflow(text: str) -> str:
    """Join hard-wrapped lines into paragraphs, keeping deliberate breaks.

    Instructions are usually written as hard-wrapped prose (an
    ``instructions.md`` wrapped at 80 columns). A display wraps text again at
    its own measure, so every source line break left in place ends a line
    early: ragged text, orphaned words, and a taller block than the words
    need. Reflowing first means the display's wrapping is the only wrapping.

    The rule, applied after ``\\r\\n`` and lone ``\\r`` endings become ``\\n``:

    - **Paragraphs are separated by blank lines** (lines that are empty or
      only whitespace). Any run of them becomes exactly one blank line, and
      blank lines before the first paragraph or after the last are dropped —
      a paragraph break is a break, however many newlines typed it.
    - **Inside a paragraph, a line is joined to the one before it with a
      single space** — unless it starts with whitespace (an indented line: a
      table row, an aligned key list, a quoted block) or with a list marker
      (``-``, ``*``, ``+``, ``•``, ``1.``, ``1)`` followed by a space). Those
      keep their line break and their text exactly, indentation included, so
      text laid out by hand survives. A plain line after one of them joins
      it, which is how a list item's text continues onto the next line.
    - **Trailing whitespace is dropped from each line**; everything else
      inside a line, runs of spaces included, is left alone, because spaces
      are how a column is aligned.

    Text whose every line break matters (a pause menu, a table with an
    unindented first column) should not be reflowed at all — see
    ``show_message(..., reflow=False)``.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Each paragraph is a list of output lines; a new paragraph is opened by
    # the first non-blank line after a blank one.
    paragraphs: list[list[str]] = []
    after_blank = True
    for raw in lines:
        line = raw.rstrip()
        if not line:
            after_blank = True
            continue
        if after_blank:
            paragraphs.append([line])
            after_blank = False
        elif line[0].isspace() or _LIST_ITEM.match(line):
            # Laid out by hand: its break and its indentation are the point.
            paragraphs[-1].append(line)
        else:
            # Prose wrapped by the writer's editor: the break is an accident
            # of the source file, so it becomes the space it stands for.
            paragraphs[-1][-1] += " " + line
    return "\n\n".join("\n".join(paragraph) for paragraph in paragraphs)
