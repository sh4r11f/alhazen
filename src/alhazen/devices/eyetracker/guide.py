"""The calibration guide: the screen shown before a calibration starts.

Before the first target appears, the experimenter should know what is about
to happen and what they are expected to do — which eye the session reads,
how many targets there are and where, whether they press a key for each one
or the tracker moves on by itself, and which keys do what. Without it, the
first target of a session is a black dot on a grey screen and a person
guessing whether to press something.

This module is only the *text*: a body for ``DisplayBackend.show_menu`` in
the terminal-green panel every instruction screen uses. Each backend
composes it with its own facts (the eye it calibrates, whether it drives the
walk or the Host PC does) and draws it, redrawing while the live eye status
line changes. Keeping the text here keeps it testable on a machine with no
display, and identical in shape across the two real backends — an
experimenter moving between rigs reads the same screen.
"""

from __future__ import annotations

from collections.abc import Sequence

# Every backend's guide has the same heading, so the panel is recognisable
# before it is read.
GUIDE_TITLE = "CALIBRATION"

# What the two advance modes ask of the experimenter, in their words.
ADVANCE_LINES = {
    "manual": "MANUAL — press SPACE when the subject is fixating each target",
    "auto": "AUTO — each target is accepted by itself once the subject holds it",
}

# How many targets each layout name stands for. The viewpixx backend lays its
# grid out itself and can count the positions; the EyeLink's Host PC owns its
# grid, so the guide has to know the count from the name alone. These are
# the layouts the EyeLink accepts (its ``calibration_type`` command).
TARGET_COUNTS = {"H3": 3, "HV3": 3, "HV5": 5, "HV9": 9, "HV13": 13}


def target_count(layout: str) -> int:
    """How many targets ``layout`` has, for the guide's targets line."""
    try:
        return TARGET_COUNTS[layout]
    except KeyError:
        raise ValueError(
            f"unknown calibration layout {layout!r} — expected one of {', '.join(TARGET_COUNTS)}"
        ) from None


def calibration_guide(
    *,
    tracker: str,
    eye: str,
    layout: str,
    n_targets: int,
    area: float,
    advance: str,
    keys: Sequence[tuple[str, str]],
    status: str | None = None,
    start_line: str = "press SPACE to start, ESC to abort",
) -> str:
    """The guide's body, one fact per line, keys aligned in a column.

    ``eye`` is the backend's own sentence about which eye is expected
    ("LEFT eye read by the session; both eyes are calibrated"), because the
    two real backends answer that question differently. ``status`` is the
    live line — which eyes the camera sees right now — and is omitted by a
    backend that cannot say before the procedure starts.
    """
    if advance not in ADVANCE_LINES:
        raise ValueError(f"unknown calibration advance mode {advance!r}")
    lines = [
        f"tracker   {tracker}",
        f"eye       {eye}",
        f"targets   {layout} — {n_targets} targets over {area:.0%} of the screen, centre first",
        f"advance   {ADVANCE_LINES[advance]}",
        "",
        "keys",
    ]
    # Padded to a common width so the labels line up, the way the pause
    # menu's rows do: a column can be scanned, a paragraph has to be read.
    width = max((len(key) for key, _ in keys), default=0)
    lines += [f"{key:<{width}}   {label}" for key, label in keys]
    if status is not None:
        lines += ["", status]
    lines += ["", start_line]
    return "\n".join(lines)
