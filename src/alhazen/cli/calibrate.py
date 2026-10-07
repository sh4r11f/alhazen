"""Calibration: the two monitor checks that are otherwise done by hand.

Both answer questions a rig config *claims* to have the answer to, and which
are wrong often enough to be worth verifying:

- ``ruler`` draws a bar of a known angular size and says how many centimetres
  it should measure. A tape measure then says whether the config's screen
  width and viewing distance are right — and if they are not, every stimulus
  size in every experiment on that rig is wrong by the same factor.
- ``gamma`` fits the display's luminance response from photometer readings and
  stores the correction beside the rig config. Without it, "50% contrast" is
  not 50% of anything.

Photometer automation is out of scope: the measurements come from a human
with a meter, in a CSV.
"""

from __future__ import annotations

import logging

from alhazen.config.gamma import (
    GAMMA_FILENAME_SUFFIX,
    fit_gamma,
    gamma_path,
    load_gamma,
    read_measurements,
    write_gamma,
)
from alhazen.config.models import RigConfig
from alhazen.display.ruler import draw_ruler_on, ruler_report

log = logging.getLogger(__name__)

# Re-exported: `alhazen calibrate gamma` is where an experimenter meets these,
# but the session builder has to read the same file and sits below the CLI, so
# they live in the config layer (alhazen.config.gamma).
#
# ruler_report and draw_ruler_on are re-exported for the same reason, one layer
# up: `alhazen calibrate ruler` is where they started, but `--mode measure`
# draws the same bar and alhazen.modes sits below the CLI, so they live in the
# display layer (alhazen.display.ruler). The names stay importable from here,
# where they have always been.
__all__ = [
    "GAMMA_FILENAME_SUFFIX",
    "draw_ruler",
    "draw_ruler_on",
    "fit_gamma",
    "gamma_path",
    "load_gamma",
    "read_measurements",
    "ruler_report",
    "write_gamma",
]


def draw_ruler(rig: RigConfig, size_dva: float = 10.0, windowed: bool = False) -> str:
    """Open the rig's real display and draw the bar until a key is pressed.

    The printed report says what a bar *should* measure; this is the bar. On a
    simulated display there is nothing to hold a tape against, so the report
    is the whole answer and the window is never opened — which is also what
    keeps this callable from the default test suite.
    """
    report = ruler_report(rig, size_dva)
    if rig.display.backend == "simulated":
        return report

    from alhazen.display.psychopy_backend import PsychoPyDisplay

    display = PsychoPyDisplay(rig.monitor, windowed=windowed)
    display.open()
    try:
        draw_ruler_on(display, rig, size_dva)
    finally:
        display.close()
    return report
