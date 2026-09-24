"""Writing a measured fraction beside the threshold it was compared with.

Several messages state a comparison and print its numbers: frame QA recycles
a trial because "21 of 209 frames dropped (10.05%), over the 10% budget";
the session pauses because trials "dropped over 10% of their frames"; the
alignment refuses a fit in which "only 399 of 500 ... events matched a pulse
(79.8% < 80%)". Printed
with a fixed number of decimals, each of those could contradict the very
comparison it reports. At the shipped 10% budget, 21 of 209 frames read
"(10.0%), over the 10% budget"; a 7.5% budget read "8%", so 3 of 39 frames
read "(7.7%), over the 8% budget"; 399 of 500 matched events read
"(80% < 80%)". A reader can trust none of those numbers. So every such
message writes its numbers by one rule:

- **The threshold is written with the fewest decimals that state it**:
  0.1 is "10%", 0.075 is "7.5%", 0.855 is "85.5%". It is never rounded to a
  number that was not set.
- **The measured fraction is written with the fewest decimals that keep it
  on the correct side of that threshold**, and never fewer than the caller's
  usual number (frame QA's lines have always had one decimal). A fraction
  that one decimal would round onto the threshold gets a second: "10.05%",
  "79.95%". Each printed number is still a correctly rounded value of its
  own number; nothing is nudged.

"The correct side" is the caller's own test, passed as the relation that
holds: frame QA recycles when the fraction is MORE than the budget, so it
passes ``">"`` for a trial over it and ``"<="`` for one within it, where
equal is correct ("10.0%" against "10%" for 3 of 30 frames). The alignment
refuses when the fraction is LESS than the threshold, so it passes ``"<"``.

Why this lives in ``alhazen.data``: the rule is needed by three layers:
display (``display/frames.py``), session (``session/runner.py``) and analysis
(``analysis/sync.py``). The layering contract (pyproject
``[tool.importlinter]``) lets a module import only from the lines below its
own. ``config | data`` is the lowest line, below display, so a module there is
importable by all three callers and by any layer added between them. Of
those two, ``data`` is the package kept "deliberately the bottom of the stack
and deliberately ignorant". It knows nothing about trials, frames or
alignment, and neither does this. Its text also ends up in what a run writes:
the trial row's ``frame_qa_reason`` and ``session.log``. ``config`` holds the
validated models and their loader, and a string rule there would make
"config" mean "anything low". ``alhazen.errors`` sits outside the contract,
but it is the exception vocabulary, and two of these messages are not errors
(a log line and a pause heading). A new top-level module would have had to
join the contract as a layer of its own for two functions.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from typing import Literal

# The relation the caller's own test found between the fraction and the
# threshold, which the printed numbers must also show.
Relation = Literal["<", "<=", ">", ">="]

_HOLDS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}

# A threshold is something a person typed (0.1, 0.075, 0.855), so six
# decimals of a percent state any threshold anyone sets. One that needs more
# (1/3) is written to six: "33.333333%".
_THRESHOLD_MAX_PLACES = 6

# The most decimals the measured fraction may take. A count of frames or
# events never needs this many; the bound only guarantees the search ends.
_VALUE_MAX_PLACES = 12

# How far a percent may sit from its text and still count as stated by it.
# This absorbs the float noise of scaling by 100 (0.55 x 100 is
# 55.00000000000001), which is not part of what was set.
_SCALING_NOISE = 1e-9


def threshold_percent(threshold: float) -> str:
    """``threshold`` (a fraction) as a percent, with the fewest decimals that
    state it: 0.1 is ``"10%"``, 0.075 is ``"7.5%"``, 0.855 is ``"85.5%"``.

    For a message that states a threshold without a measured fraction beside
    it, such as the session's failure-streak pause ("dropped over 7.5% of
    their frames"). Whole percents would have written that budget as "8%".
    ``compared_percents`` writes its threshold with this too, so the two
    kinds of message agree on how a threshold looks.

    Raises ``ValueError`` for NaN or an infinity: there is no percent to
    write, and "nan%" in a message would hide the fault that produced it.
    """
    return f"{_threshold_digits(threshold)}%"


def compared_percents(
    value: float, relation: Relation, threshold: float, *, min_places: int = 0
) -> tuple[str, str]:
    """``value`` and ``threshold`` as percents whose printed numbers show the
    same ``relation`` as the numbers themselves.

    Read the call as the comparison it reports:
    ``compared_percents(21 / 209, ">", 0.10, min_places=1)`` is
    ``("10.05%", "10%")``, ``compared_percents(399 / 500, "<", 0.8)`` is
    ``("79.8%", "80%")``. The threshold is written as ``threshold_percent``
    writes it. The value gets the fewest decimals, at least ``min_places``,
    that keep the relation true of the two printed numbers.

    If no number of decimals separates them, both are returned at full
    precision as plain fractions, without a ``%``. That happens only when the
    two are closer than printed percents can show: a threshold within a float
    rounding error of the value (3 of 10 against 0.1 + 0.2), or a threshold
    that needs more than six decimals with the value inside that last digit.
    Scaling by 100 could make two such floats equal, so they are returned
    unscaled. The message still reads true: "(0.3 < 0.30000000000000004)".

    Raises ``ValueError`` if ``relation`` is not one of ``<``, ``<=``, ``>``,
    ``>=``; if either number is NaN or infinite; if ``min_places`` is outside
    0 to 12; or if the relation does not actually hold. The last is a bug in
    the caller, which would otherwise print a comparison that is false.
    """
    holds = _HOLDS.get(relation)
    if holds is None:
        raise ValueError(
            f"relation must be one of {', '.join(repr(r) for r in _HOLDS)}, got {relation!r}"
        )
    if not math.isfinite(value):
        raise ValueError(f"cannot write {value!r} as a percent: it is not a finite number")
    if not 0 <= min_places <= _VALUE_MAX_PLACES:
        raise ValueError(f"min_places must be between 0 and {_VALUE_MAX_PLACES}, got {min_places}")
    threshold_digits = _threshold_digits(threshold)
    # Checked on the numbers, before any rounding. A caller that asks for a
    # relation its own test did not find would get a message that lies, so
    # this stops it here.
    if not holds(value, threshold):
        raise ValueError(
            f"asked to write {value!r} {relation} {threshold!r}, which is false; a message "
            f"that states it would contradict itself"
        )

    value_pct = value * 100
    for places in range(min_places, _VALUE_MAX_PLACES + 1):
        value_digits = f"{value_pct:.{places}f}"
        # Compared as the text that will be printed, not the floats behind
        # it, so the check is on exactly what the reader sees. Rounding can
        # carry the value onto the threshold (10.048 is "10.0" to one
        # decimal, 79.95 is "80.0"), which is why this adds decimals until
        # the relation holds rather than fixing a number of them.
        if holds(float(value_digits), float(threshold_digits)):
            return f"{value_digits}%", f"{threshold_digits}%"
    # No number of decimals separates them as percents. repr is the shortest
    # text that reads back as exactly the same float, so the relation between
    # the two texts is the relation between the two numbers.
    return repr(value), repr(threshold)


def _threshold_digits(threshold: float) -> str:
    """The digits of ``threshold_percent``, without the ``%``. The comparison
    in ``compared_percents`` reads them back as a number."""
    if not math.isfinite(threshold):
        raise ValueError(f"cannot write {threshold!r} as a percent: it is not a finite number")
    threshold_pct = threshold * 100
    # The first text, from whole percents up, that reads back as the
    # threshold itself (to within the noise of the scaling).
    for places in range(_THRESHOLD_MAX_PLACES + 1):
        digits = f"{threshold_pct:.{places}f}"
        if abs(float(digits) - threshold_pct) < _SCALING_NOISE:
            return digits
    # A threshold that needs more than six decimals (1/3) is written to six.
    # compared_percents compares the value against this printed text, so the
    # two printed numbers still agree with each other. When no printed value
    # can agree with it, compared_percents falls back to full precision.
    return f"{threshold_pct:.{_THRESHOLD_MAX_PLACES}f}"
