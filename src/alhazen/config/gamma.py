"""Where a measured gamma lives, how a session finds it, and how one is fitted.

`alhazen calibrate gamma` fits the display's luminance response and writes it
beside the rig config it belongs to. This is the other half of that loop: the
file's location and its reader, in the config layer, so the session builder
can apply a stored correction without reaching up into the CLI.

Beside the rig config and named after it, because a gamma table is a property
of one physical monitor: following the wrong one is worse than having none.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np
import yaml

from alhazen.errors import ConfigError

log = logging.getLogger(__name__)

GAMMA_FILENAME_SUFFIX = "_gamma.yaml"


def gamma_path(rig_path: Path | str) -> Path:
    """Where a fit for this rig config is stored."""
    rig_path = Path(rig_path)
    return rig_path.with_name(rig_path.stem + GAMMA_FILENAME_SUFFIX)


def write_gamma(rig_path: Path | str, fit: dict[str, float]) -> Path:
    path = gamma_path(rig_path)
    path.write_text(yaml.safe_dump({"schema_version": 1, **fit}, sort_keys=False), encoding="utf-8")
    log.info("gamma fit written to %s", path)
    return path


def load_gamma(rig_path: Path | str) -> dict[str, float] | None:
    """The stored fit for this rig, if one has been measured."""
    path = gamma_path(rig_path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: float(value) for key, value in data.items() if key != "schema_version"}


# The photometer CSV reader and the fit live here, in the config layer, beside
# the file they produce: `alhazen calibrate gamma` (cli) and measure mode's
# luminance job (modes) both need them, and modes may not import the cli.
# alhazen.cli.calibrate re-exports both names, where they have always been.


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
    with path.open(encoding="utf-8-sig") as handle:
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
