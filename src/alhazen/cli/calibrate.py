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

import csv
import logging
import math
from pathlib import Path

import numpy as np

from alhazen.config.gamma import (
    GAMMA_FILENAME_SUFFIX,
    gamma_path,
    load_gamma,
    write_gamma,
)
from alhazen.config.models import RigConfig
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

# Re-exported: `alhazen calibrate gamma` is where an experimenter meets these,
# but the session builder has to read the same file and sits below the CLI, so
# they live in the config layer (alhazen.config.gamma).
__all__ = [
    "GAMMA_FILENAME_SUFFIX",
    "draw_ruler",
    "fit_gamma",
    "gamma_path",
    "load_gamma",
    "read_measurements",
    "ruler_report",
    "write_gamma",
]


def ruler_report(rig: RigConfig, size_dva: float = 10.0) -> str:
    """What a bar of ``size_dva`` should measure on this rig, in centimetres.

    The arithmetic is the rig config's own: if the printed number does not
    match a tape measure held against the screen, the config is wrong, not
    the ruler.
    """
    screen = Screen.from_monitor(rig.monitor)
    width_px = screen.deg2px(size_dva)
    cm_per_px = rig.monitor.width_cm / rig.monitor.width_px
    width_cm = width_px * cm_per_px
    # The same length worked out from the geometry directly, as a check on
    # the linear approximation the Screen model uses.
    exact_cm = 2.0 * rig.monitor.distance_cm * math.tan(math.radians(size_dva / 2.0))
    return "\n".join(
        [
            f"a {size_dva:g} dva bar on this rig:",
            f"  {width_px:.1f} px wide",
            f"  {width_cm:.2f} cm on the panel  (measure this with a tape)",
            f"  {exact_cm:.2f} cm by exact trigonometry at {rig.monitor.distance_cm:g} cm",
            f"  px per degree: {screen.px_per_deg:.2f}",
            "",
            "If the tape disagrees with the second line, fix monitor.width_cm or",
            "monitor.distance_cm in the rig config — every stimulus size on this rig",
            "is scaled by that same error until you do.",
        ]
    )


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

    screen = Screen.from_monitor(rig.monitor)
    width_px = screen.deg2px(size_dva)
    display = PsychoPyDisplay(rig.monitor, windowed=windowed)
    display.open()
    try:
        from psychopy import event, visual

        # A white bar of the computed width, plus end ticks, on black: the
        # thing a tape measure is held against. Drawn in pixels, because the
        # px<->cm question is exactly what is being checked.
        bar = visual.Rect(
            display.window,
            units="pix",
            width=width_px,
            height=max(round(screen.height_px * 0.02), 4),
            fillColor="white",
            lineColor="white",
        )
        tick_height = max(round(screen.height_px * 0.10), 20)
        ticks = [
            visual.Rect(
                display.window,
                units="pix",
                width=2,
                height=tick_height,
                pos=(offset, 0),
                fillColor="white",
                lineColor="white",
            )
            for offset in (-width_px / 2.0, width_px / 2.0)
        ]
        label = visual.TextStim(
            display.window,
            units="pix",
            text=(
                f"{size_dva:g} dva = {width_px:.1f} px\n"
                f"measure between the ticks: it should be "
                f"{width_px * rig.monitor.width_cm / rig.monitor.width_px:.2f} cm\n"
                f"any key to close"
            ),
            pos=(0, -tick_height),
            height=max(round(screen.height_px * 0.025), 12),
            color="white",
        )
        while not event.getKeys():
            bar.draw()
            for tick in ticks:
                tick.draw()
            label.draw()
            display.flip()
    finally:
        display.close()
    return report


def read_measurements(path: Path | str) -> tuple[np.ndarray, np.ndarray]:
    """Read a photometer CSV: ``level,luminance`` per row.

    ``level`` is what was displayed (0–1 or 0–255) and ``luminance`` what the
    meter read (any unit — only the shape of the curve matters).
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"measurements file not found: {path}")
    levels: list[float] = []
    luminances: list[float] = []
    with path.open() as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"level", "luminance"} <= set(reader.fieldnames):
            raise ConfigError(
                f"{path} needs 'level' and 'luminance' columns; found {reader.fieldnames}"
            )
        for row in reader:
            levels.append(float(row["level"]))
            luminances.append(float(row["luminance"]))
    if len(levels) < 3:
        raise ConfigError(
            f"{path} has {len(levels)} measurements; a gamma fit needs at least 3 "
            f"(and is worth doing with 10 or more)"
        )
    values = np.asarray(levels, dtype=float)
    # Levels given in 0-255 are normalised, so either convention works.
    if values.max() > 1.0:
        values = values / 255.0
    return values, np.asarray(luminances, dtype=float)


def fit_gamma(levels: np.ndarray, luminances: np.ndarray) -> dict[str, float]:
    """Fit ``luminance = min + (max - min) · level**gamma``.

    Fitted in log space, which turns the power law into a straight line and
    makes the fit a least-squares problem rather than an optimisation that
    could fail to converge on a rig with an experimenter waiting.
    """
    minimum = float(luminances.min())
    maximum = float(luminances.max())
    if maximum <= minimum:
        raise ConfigError(
            "the measured luminances do not increase — check that the meter was "
            "reading the patch and that the levels were displayed in order"
        )
    normalized = (luminances - minimum) / (maximum - minimum)
    # Endpoints carry no information about the exponent (they are 0 and 1 by
    # construction) and log(0) is undefined, so they are excluded.
    usable = (levels > 0) & (normalized > 0) & (levels < 1) & (normalized < 1)
    if usable.sum() < 2:
        raise ConfigError(
            "not enough intermediate measurements to fit a gamma: measure some levels "
            "between black and white"
        )
    gamma, _intercept = np.polyfit(np.log(levels[usable]), np.log(normalized[usable]), 1)
    residuals = normalized[usable] - levels[usable] ** gamma
    return {
        "gamma": float(gamma),
        "min_luminance": minimum,
        "max_luminance": maximum,
        "n_measurements": int(len(levels)),
        "residual_rms": float(np.sqrt(np.mean(residuals**2))),
    }
