"""The arithmetic behind measure mode's newer jobs, with no hardware in sight.

Like the statistics in :mod:`alhazen.modes.measure`, these are the part that
can be quietly wrong — a unit dropped, a bias reported as precision, a gain
computed from one point — so they are plain functions, unit tested with known
inputs, and the jobs that touch a device only hand them numbers.

Every function refuses what it cannot honestly summarise (no samples, a
non-finite or negative reading) rather than returning a number that looks like
a measurement.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from alhazen.display.screen import Screen


def _finite_positive(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number above 0, not {value!r}")
    return float(value)


# ----------------------------------------------------------------------
# Monitor geometry: declared against measured
# ----------------------------------------------------------------------


def px_per_deg(width_px: float, width_cm: float, distance_cm: float) -> float:
    """Pixels per degree at the centre of the screen, from the panel's width
    and the viewing distance — the arithmetic ``Screen`` uses (one degree
    centred on the line of sight)."""
    cm_per_deg = 2.0 * distance_cm * math.tan(math.radians(0.5))
    return cm_per_deg * width_px / width_cm


def geometry_check(
    *,
    width_px: int,
    declared_width_cm: float,
    declared_distance_cm: float,
    bar_px: float,
    measured_bar_cm: float,
    measured_distance_cm: float,
    tolerance: float = 0.02,
) -> dict[str, Any]:
    """What the tape says about the rig's geometry.

    The bar was drawn ``bar_px`` wide; the tape says how long that is, which
    gives the panel's true centimetres per pixel (so its true width), and the
    tape from the eye to the screen gives the true distance. The declared
    values stay beside the measured ones, never replaced: this measures, the
    rig file decides. ``ok`` is whether pixels per degree — the number every
    stimulus size goes through — is within ``tolerance`` of the declared one.
    """
    measured_bar_cm = _finite_positive("measured bar length", measured_bar_cm)
    measured_distance_cm = _finite_positive("measured viewing distance", measured_distance_cm)
    bar_px = _finite_positive("bar width in px", bar_px)
    measured_width_cm = measured_bar_cm / bar_px * width_px
    declared = px_per_deg(width_px, declared_width_cm, declared_distance_cm)
    measured = px_per_deg(width_px, measured_width_cm, measured_distance_cm)
    error = measured / declared - 1.0
    return {
        "declared": {
            "width_cm": declared_width_cm,
            "distance_cm": declared_distance_cm,
            "px_per_deg": declared,
            "bar_cm": bar_px * declared_width_cm / width_px,
        },
        "measured": {
            "width_cm": measured_width_cm,
            "distance_cm": measured_distance_cm,
            "px_per_deg": measured,
            "bar_cm": measured_bar_cm,
        },
        "bar_px": bar_px,
        "px_per_deg_error": error,
        "distance_error": measured_distance_cm / declared_distance_cm - 1.0,
        "width_error": measured_width_cm / declared_width_cm - 1.0,
        "tolerance": tolerance,
        "ok": abs(error) <= tolerance,
    }


# ----------------------------------------------------------------------
# Eye tracker validation: accuracy, precision and gain, kept apart
# ----------------------------------------------------------------------


def gaze_quality(
    targets_px: Sequence[tuple[float, float]],
    samples_px: Sequence[Sequence[tuple[float, float]]],
    screen: Screen,
) -> dict[str, Any]:
    """Accuracy, precision and gain of a tracker over validation targets.

    Positions are centred px. Per target, from the samples taken while the
    eye was on it:

    - **bias** — the mean sample minus the target, in degrees (x, y): which
      way the calibration is off;
    - **accuracy** — the length of that bias: how far off;
    - **precision (RMS-S2S)** — root mean square of successive sample-to-
      sample distances: the noise between consecutive reads, as polled;
    - **dispersion (SD)** — the spread of the samples around their own mean.

    Over all targets, **gain** is the least-squares slope of mean gaze
    against target position, separately in x and y: 1.0 means a 10° target
    reads as 10°; 0.9 means the calibration compresses the screen. It needs
    at least two distinct target positions on an axis, and is None without.

    Precision here is of the reads this loop polled, at the rate it polled
    them; a device-rate figure needs the device's own recording, and the
    report says which this is.
    """
    if len(targets_px) != len(samples_px):
        raise ValueError(f"{len(targets_px)} targets but {len(samples_px)} sample sets")
    if not targets_px:
        raise ValueError("no targets to judge")
    per_target: list[dict[str, Any]] = []
    means = []
    for target, samples in zip(targets_px, samples_px, strict=True):
        points = np.asarray(samples, dtype=float).reshape(-1, 2)
        if points.size == 0 or not np.all(np.isfinite(points)):
            raise ValueError(f"target {target}: no finite gaze samples to judge")
        mean = points.mean(axis=0)
        means.append(mean)
        bias_px = mean - np.asarray(target, dtype=float)
        steps = np.diff(points, axis=0)
        rms_s2s = float(np.sqrt(np.mean(np.sum(steps**2, axis=1)))) if len(points) > 1 else None
        sd = float(np.sqrt(np.sum(points.var(axis=0)))) if len(points) > 1 else None
        per_target.append(
            {
                "target_px": [float(target[0]), float(target[1])],
                "n_samples": int(len(points)),
                "mean_px": [float(mean[0]), float(mean[1])],
                "bias_dva": [screen.px2deg(float(bias_px[0])), screen.px2deg(float(bias_px[1]))],
                "accuracy_dva": screen.px2deg(float(np.hypot(*bias_px))),
                "precision_rms_s2s_dva": None if rms_s2s is None else screen.px2deg(rms_s2s),
                "dispersion_sd_dva": None if sd is None else screen.px2deg(sd),
            }
        )
    accuracies: list[float] = [float(t["accuracy_dva"]) for t in per_target]
    precisions: list[float] = [
        float(t["precision_rms_s2s_dva"])
        for t in per_target
        if t["precision_rms_s2s_dva"] is not None
    ]
    targets = np.asarray(targets_px, dtype=float)
    measured = np.asarray(means, dtype=float)
    gain: dict[str, float | None] = {}
    offset: dict[str, float | None] = {}
    for axis, name in ((0, "x"), (1, "y")):
        if len(np.unique(targets[:, axis])) < 2:
            gain[name], offset[name] = None, None
            continue
        slope, intercept = np.polyfit(targets[:, axis], measured[:, axis], 1)
        gain[name], offset[name] = float(slope), screen.px2deg(float(intercept))
    return {
        "n_targets": len(per_target),
        "accuracy_mean_dva": float(np.mean(accuracies)),
        "accuracy_max_dva": float(np.max(accuracies)),
        "precision_rms_s2s_median_dva": float(np.median(precisions)) if precisions else None,
        "gain": gain,
        "offset_dva": offset,
        "per_target": per_target,
    }


# ----------------------------------------------------------------------
# Reward: what a pulse delivers, from what the balance says
# ----------------------------------------------------------------------


def reward_volume(
    *, net_mass_g: float, density_g_per_ml: float, n_pulses: int, pulse_ms: float
) -> dict[str, Any]:
    """Volume per pulse from the collected liquid's net mass.

    The mass is what the balance read (tared container); the density is the
    operator's declared conversion (1.00 g/mL for water is a declaration, not
    a measurement, and is recorded as one). Command duration says nothing
    about what came out of the spout — this is the only number here that does.
    """
    net_mass_g = _finite_positive("net mass", net_mass_g)
    density = _finite_positive("density", density_g_per_ml)
    if not isinstance(n_pulses, int) or n_pulses < 1:
        raise ValueError(f"n_pulses must be a whole number of 1 or more, not {n_pulses!r}")
    pulse_ms = _finite_positive("pulse width", pulse_ms)
    volume_ul = net_mass_g / density * 1000.0
    per_pulse = volume_ul / n_pulses
    return {
        "net_mass_g": net_mass_g,
        "density_g_per_ml": density,
        "volume_ul": volume_ul,
        "n_pulses": n_pulses,
        "pulse_ms": pulse_ms,
        "ul_per_pulse": per_pulse,
        "ul_per_ms_open": per_pulse / pulse_ms,
    }


def reward_volume_read(*, volume_ml: float, n_pulses: int, pulse_ms: float) -> dict[str, Any]:
    """Volume per pulse from the volume the operator read off a beaker.

    The reading is the only number here that says what came out of the
    spout; the pulse count and width are what was commanded. A beaker's
    graduations limit it, so collect enough pulses to read well.
    """
    volume_ml = _finite_positive("volume read", volume_ml)
    if not isinstance(n_pulses, int) or n_pulses < 1:
        raise ValueError(f"n_pulses must be a whole number of 1 or more, not {n_pulses!r}")
    pulse_ms = _finite_positive("pulse width", pulse_ms)
    volume_ul = volume_ml * 1000.0
    per_pulse = volume_ul / n_pulses
    return {
        "volume_ml": volume_ml,
        "volume_ul": volume_ul,
        "n_pulses": n_pulses,
        "pulse_ms": pulse_ms,
        "ul_per_pulse": per_pulse,
        "ul_per_ms_open": per_pulse / pulse_ms,
        "source": "read off a beaker by the operator",
    }


# ----------------------------------------------------------------------
# Mouse: pointer travel per physical centimetre
# ----------------------------------------------------------------------


def pointer_gain(
    slow_px: Sequence[float], fast_px: Sequence[float], distance_cm: float
) -> dict[str, Any]:
    """Screen pixels the pointer moved per centimetre the mouse moved.

    That is the whole pointer path — sensor resolution times the OS's gain
    and acceleration — not the sensor's DPI, which only the device's raw
    counts can give. Moving the same distance slowly and quickly separates
    the two halves well enough to say whether acceleration is on: with none,
    the ratio is 1.
    """
    distance_cm = _finite_positive("distance moved", distance_cm)

    def per_cm(values: Sequence[float], label: str) -> dict[str, float]:
        array = np.abs(np.asarray(values, dtype=float))
        if array.size == 0 or not np.all(np.isfinite(array)):
            raise ValueError(f"no finite {label} passes to judge")
        rates = array / distance_cm
        return {
            "n": int(rates.size),
            "mean_px_per_cm": float(rates.mean()),
            "cv": float(rates.std(ddof=1) / rates.mean())
            if rates.size > 1 and rates.mean()
            else 0.0,
        }

    slow = per_cm(slow_px, "slow")
    fast = per_cm(fast_px, "fast")
    ratio = fast["mean_px_per_cm"] / slow["mean_px_per_cm"] if slow["mean_px_per_cm"] else None
    return {
        "distance_cm": distance_cm,
        "slow": slow,
        "fast": fast,
        "fast_over_slow": ratio,
        "acceleration_suspected": ratio is not None and abs(ratio - 1.0) > 0.1,
    }


# ----------------------------------------------------------------------
# Neural stream: is it advancing at its rate, and what does it look like
# ----------------------------------------------------------------------


def stream_rate(
    count_start: int, count_end: int, wall_s: float, reported_hz: float
) -> dict[str, Any]:
    """Samples that arrived over a wall-clock window, against the rate the
    acquisition reports. Wall clock is good to a fraction of a percent over
    a couple of seconds; it cannot see a clock drift of parts per million,
    and the report does not claim to."""
    wall_s = _finite_positive("listening time", wall_s)
    reported_hz = _finite_positive("reported sample rate", reported_hz)
    arrived = int(count_end) - int(count_start)
    observed = arrived / wall_s
    return {
        "samples_arrived": arrived,
        "wall_s": wall_s,
        "observed_hz": observed,
        "reported_hz": reported_hz,
        "rate_error": observed / reported_hz - 1.0,
        "advancing": arrived > 0,
    }


def channel_noise(samples: Any, *, saturation: int = 32767) -> dict[str, Any]:
    """Per-channel RMS (mean removed) of a block of raw int16 samples, in ADC
    counts — not microvolts: converting needs the stream's own gain, which
    this does not read. Flat channels (no variation at all) and samples at
    the converter's rail are counted, since either says more than the RMS."""
    block = np.asarray(samples)
    if block.ndim != 2 or block.shape[0] < 2 or block.shape[1] < 1:
        raise ValueError(f"need at least 2 samples of at least 1 channel, got shape {block.shape}")
    data = block.astype(float)
    rms = np.sqrt(np.mean((data - data.mean(axis=0)) ** 2, axis=0))
    return {
        "n_samples": int(block.shape[0]),
        "n_channels": int(block.shape[1]),
        "rms_counts": [float(v) for v in rms],
        "rms_counts_median": float(np.median(rms)),
        "flat_channels": int(np.sum(rms == 0)),
        "railed_samples": int(np.sum(np.abs(block) >= saturation)),
        "units": "int16 ADC counts",
    }


def luminance_summary(levels: Sequence[float], luminances: Sequence[float]) -> dict[str, Any]:
    """The photometer readings as a table, refused when one is not a finite,
    non-negative luminance; the gamma fit itself is config.gamma.fit_gamma."""
    lv = np.asarray(levels, dtype=float)
    lum = np.asarray(luminances, dtype=float)
    if lv.shape != lum.shape or lv.size < 3:
        raise ValueError("need at least 3 levels, each with one reading")
    if not np.all(np.isfinite(lum)) or np.any(lum < 0):
        raise ValueError("every luminance reading must be a finite number of 0 or more")
    monotonic = bool(np.all(np.diff(lum[np.argsort(lv)]) >= 0))
    return {
        "levels": [float(v) for v in lv],
        "luminance_cd_m2": [float(v) for v in lum],
        "min_cd_m2": float(lum.min()),
        "max_cd_m2": float(lum.max()),
        "contrast_ratio": float(lum.max() / lum.min()) if lum.min() > 0 else None,
        "monotonic": monotonic,
    }
