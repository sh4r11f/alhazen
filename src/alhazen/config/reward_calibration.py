"""The rig's reward calibration: how many µL one pulse of a given width delivers.

Measured, never assumed. The Measure rig's "Juice per pulse" job
(modes/measure_builtin.py) delivers armed pulse trains into a beaker, asks
the operator for the volume they read off it, and records the result here,
in a file beside the rig config, as the gamma fit is kept
(config/gamma.py): ``rig-lab.yaml`` → ``rig-lab.reward.yaml``.

The decision this module hides is what counts as a measured volume for a
pulse: one entry per pulse width, measured on the same line (device/channel)
at the same voltage the rig drives now. A width that was never measured, or
a line or voltage that changed since, has no volume: `ul_per_pulse` returns
None and every caller says "not measured" rather than scale from another
width (the valve is not linear near its opening time).

The file, schema 1::

    schema_version: 1
    widths:
      "200":
        ul_per_pulse: 118.5
        pulse_ms: 200
        line: Dev1/ao0
        voltage: 5.0
        measured_at: "2026-10-08T14:30:00+00:00"
        trains: 10
        pulses_per_train: 10
        inter_pulse_ms: 200
        volume_ml: 11.85
        previous: [ ...earlier entries for this width, newest first... ]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REWARD_CALIBRATION_SUFFIX = ".reward.yaml"
SCHEMA_VERSION = 1


def reward_calibration_path(rig_path: Path | str) -> Path:
    """Where the reward calibration for this rig config is stored."""
    rig_path = Path(rig_path)
    return rig_path.with_name(rig_path.stem + REWARD_CALIBRATION_SUFFIX)


def load_reward_calibration(rig_path: Path | str) -> dict[str, Any] | None:
    """The stored calibration for this rig, or None when none was measured.

    A file that cannot be read is an error, not "no calibration": a session
    that asks for µL must not quietly run on another rig's numbers or none.
    """
    path = reward_calibration_path(rig_path)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{path} is not a schema-{SCHEMA_VERSION} reward calibration; measure it again "
            f"(Measure rig, Reward, Juice per pulse)"
        )
    widths = data.get("widths") or {}
    if not isinstance(widths, dict):
        raise ValueError(f"{path}: 'widths' must be a mapping of pulse width to measurement")
    return {"schema_version": SCHEMA_VERSION, "widths": {int(k): v for k, v in widths.items()}}


def ul_per_pulse(
    calibration: dict[str, Any] | None, *, pulse_ms: int, line: str, voltage: float
) -> dict[str, Any] | None:
    """The measurement for ``pulse_ms`` on this line at this voltage, or None.

    Exact width only, and only when the line and voltage it was measured on
    are the ones given: anything else is a volume nobody measured.
    """
    if not calibration:
        return None
    entry = calibration["widths"].get(int(pulse_ms))
    if not entry:
        return None
    if entry.get("line") != line or float(entry.get("voltage", -1.0)) != float(voltage):
        return None
    return dict(entry)


def record_reward_calibration(rig_path: Path | str, entry: dict[str, Any]) -> Path:
    """Store one width's measurement, keeping the ones it replaces.

    ``entry`` must carry ``pulse_ms``, ``ul_per_pulse``, ``line`` and
    ``voltage``. An earlier measurement of the same width moves into that
    width's ``previous`` list, newest first: a calibration is replaced, never
    lost.
    """
    for key in ("pulse_ms", "ul_per_pulse", "line", "voltage"):
        if key not in entry:
            raise ValueError(f"a reward calibration entry needs {key!r}")
    path = reward_calibration_path(rig_path)
    current = load_reward_calibration(rig_path) or {"widths": {}}
    widths: dict[int, Any] = dict(current["widths"])
    width = int(entry["pulse_ms"])
    old = widths.get(width)
    record = {key: value for key, value in entry.items() if key != "previous"}
    if old:
        earlier = [{k: v for k, v in old.items() if k != "previous"}, *(old.get("previous") or [])]
        record["previous"] = earlier
    widths[width] = record
    body = {
        "schema_version": SCHEMA_VERSION,
        "widths": {str(key): widths[key] for key in sorted(widths)},
    }
    header = (
        "# Reward calibration: µL per pulse, by pulse width, as measured with alhazen's\n"
        "# Measure rig (Reward, Juice per pulse). Written by alhazen; edit only to remove\n"
        "# a measurement that was wrong.\n"
    )
    path.write_text(
        header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    log.info("reward calibration for %d ms pulses written to %s", width, path)
    return path
