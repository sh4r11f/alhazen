"""Registering a rig's monitor with PsychoPy.

PsychoPy keeps a per-machine database of monitors — one file per name, under
``~/.psychopy3/monitors`` (``%APPDATA%\\psychopy3\\monitors`` on Windows).
That database is what Monitor Center edits, what ``visual.Window(monitor=...)``
looks a name up in, and where a gamma measured with PsychoPy's own photometer
tools is stored. A rig config describes the same physical panel in alhazen's
terms, so the two have to be told about each other: ``alhazen monitor
register --rig <yaml>`` writes the config (and any gamma alhazen has measured)
into that database under ``monitor.name``.

Two rules keep the pair honest, and they are why this module exists at all
rather than each caller poking at ``psychopy.monitors``:

- **The rig config owns the geometry.** Every degree alhazen converts goes
  through :class:`~alhazen.display.screen.Screen`, which reads the config. A
  registered monitor that disagrees about width, viewing distance or pixel
  size is a registration that has gone stale, so :func:`resolve` refuses it
  instead of opening a window whose deg/px model differs from the one placing
  the stimuli.
- **The registration owns the calibration.** Gamma, luminance grids, DKL/LMS
  matrices — anything measured through PsychoPy — stays on the registered
  monitor, and every window opened against it inherits them.

psychopy is imported lazily, inside functions, exactly as in
``display.psychopy_backend``: importing this module must never require the
renderer, because ``session.checks`` and the CLI both do.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alhazen.config.models import MonitorConfig
from alhazen.errors import DisplayError

log = logging.getLogger(__name__)

# Physical measurements are entered by hand in centimetres, so only a real
# edit (a re-measured distance, a different panel) should read as a
# difference — not the last bit of a float that survived a JSON round trip.
_CM_TOLERANCE = 1e-6

# What `alhazen monitor register` writes on the monitor, so that whoever opens
# Monitor Center later can see where the numbers came from.
NOTES_PREFIX = "registered by alhazen"


@dataclass(frozen=True)
class Registration:
    """What PsychoPy currently has stored under one monitor name.

    Every field is optional because a monitor file written by hand, or by an
    older PsychoPy, may be missing any of them. ``registered`` is False when
    PsychoPy has no file for the name at all — which is not an error, just a
    rig whose monitor has never been registered.
    """

    name: str
    registered: bool
    path: Path | None = None
    width_px: int | None = None
    height_px: int | None = None
    width_cm: float | None = None
    distance_cm: float | None = None
    gamma: float | tuple[float, ...] | None = None
    calibrated: str | None = None
    notes: str | None = None

    def summary(self) -> str:
        """One line: what PsychoPy has, in the units the rig config uses."""
        if not self.registered:
            return f"{self.name}: not registered with PsychoPy"
        size = (
            f"{self.width_px}x{self.height_px}"
            if self.width_px and self.height_px
            else "size unknown"
        )
        width = f"{self.width_cm:g} cm wide" if self.width_cm else "width unknown"
        distance = f"{self.distance_cm:g} cm away" if self.distance_cm else "distance unknown"
        gamma = f", gamma {format_gamma(self.gamma)}" if self.gamma is not None else ""
        return f"{self.name}: {size}, {width}, {distance}{gamma}"


def format_gamma(gamma: float | tuple[float, ...] | None) -> str:
    """A gamma for humans. PsychoPy stores either one value for all three
    guns or one per gun, and both need to print without a numpy repr."""
    if gamma is None:
        return "none"
    if isinstance(gamma, (int, float)):
        return f"{float(gamma):.3f}"
    return "/".join(f"{float(value):.3f}" for value in gamma)


def _psychopy_monitors() -> Any:
    """PsychoPy's monitor module, or a DisplayError naming the extra.

    Imported here rather than at module scope so that a headless analysis
    machine — and the default test suite — can import everything above this
    without the renderer installed.
    """
    try:
        from psychopy import monitors
    except ImportError as e:
        raise DisplayError(
            "registering a monitor needs psychopy installed — pip install 'alhazen[psychopy]'"
        ) from e
    return monitors


def monitor_folder() -> Path:
    """The directory PsychoPy keeps its monitor database in."""
    return Path(_psychopy_monitors().monitorFolder)


def monitor_file(name: str) -> Path | None:
    """The file PsychoPy stores this monitor in, or None if there is none.

    The extension is not hard-coded: current PsychoPy writes JSON, older
    versions wrote pickled ``.calib`` files, and a rig may still be running
    either. Whichever exists is the one an experimenter should be pointed at.
    """
    folder = monitor_folder()
    for extension in (".json", ".calib"):
        candidate = folder / f"{name}{extension}"
        if candidate.exists():
            return candidate
    return None


def registered_names() -> list[str]:
    """Every monitor name PsychoPy knows on this machine, sorted."""
    return sorted(_psychopy_monitors().getAllMonitors())


def _maybe(getter: Any) -> Any:
    """Call a PsychoPy getter, treating "not stored" as None.

    PsychoPy's getters read straight out of the calibration dict, so a field
    an older file never wrote raises KeyError rather than returning None.
    That is a monitor with an unknown width, not a crash for the caller.
    """
    try:
        return getter()
    except (KeyError, TypeError, IndexError):
        return None


def lookup(name: str) -> Registration:
    """Read back what PsychoPy has stored under ``name``.

    Never creates anything: an unknown name comes back as
    ``Registration(registered=False)``. (Constructing ``monitors.Monitor``
    for an unknown name would invent a temporary calibration, which is
    exactly what a read must not do.)
    """
    monitors = _psychopy_monitors()
    if name not in monitors.getAllMonitors():
        return Registration(name=name, registered=False)

    mon = monitors.Monitor(name, verbose=False, autoLog=False)
    size = _maybe(mon.getSizePix)
    gamma = _maybe(mon.getGamma)
    if gamma is not None and not isinstance(gamma, (int, float)):
        # One value per gun: kept as a plain tuple of floats so nothing
        # downstream has to know numpy to print it.
        gamma = tuple(float(value) for value in gamma)
    calibrated = _maybe(mon.getCalibDate)
    return Registration(
        name=name,
        registered=True,
        path=monitor_file(name),
        width_px=int(size[0]) if size else None,
        height_px=int(size[1]) if size else None,
        width_cm=_maybe(mon.getWidth),
        distance_cm=_maybe(mon.getDistance),
        gamma=gamma,
        calibrated=monitors.strFromDate(calibrated) if calibrated else None,
        notes=_maybe(mon.getNotes),
    )


def differences(monitor: MonitorConfig, registration: Registration) -> list[str]:
    """Every geometry field where the rig config and the registration disagree.

    Only the three numbers that decide what a degree of visual angle is are
    compared — pixel size, panel width, viewing distance. Everything else
    PsychoPy stores (gamma, luminance grids) is a *measurement* that the rig
    config has no opinion about, so a difference there is not a conflict.

    An unregistered monitor has nothing to disagree with and yields no
    differences; ask ``registration.registered`` for that case.
    """
    if not registration.registered:
        return []
    found: list[str] = []
    if (registration.width_px, registration.height_px) != (monitor.width_px, monitor.height_px):
        found.append(
            f"size: config {monitor.width_px}x{monitor.height_px} px, "
            f"registered {registration.width_px}x{registration.height_px} px"
        )
    for label, configured, stored in (
        ("width_cm", monitor.width_cm, registration.width_cm),
        ("distance_cm", monitor.distance_cm, registration.distance_cm),
    ):
        if stored is None or not math.isclose(configured, stored, abs_tol=_CM_TOLERANCE):
            found.append(f"{label}: config {configured:g}, registered {stored}")
    return found


def register(
    monitor: MonitorConfig,
    gamma: float | None = None,
    notes: str | None = None,
) -> Path:
    """Write this rig's monitor into PsychoPy's database, and return the file.

    An existing registration is *updated*, not replaced: the geometry is
    overwritten from the config and everything else — a gamma grid, luminance
    measurements, colour matrices someone measured in Monitor Center — is left
    where it is. ``gamma`` is written only when one is given, so registering a
    rig that has never run ``alhazen calibrate gamma`` cannot wipe a
    calibration that PsychoPy's own tools produced.

    The calibration date is stamped, because after this call it is the date
    the stored record was last written.
    """
    monitors = _psychopy_monitors()
    # Monitor() loads the existing record if there is one and creates a fresh
    # (unsaved) calibration if there is not — both end with currentCalib
    # pointing at the record about to be updated.
    mon = monitors.Monitor(monitor.name, verbose=False, autoLog=False)
    mon.setSizePix((monitor.width_px, monitor.height_px))
    mon.setWidth(monitor.width_cm)
    mon.setDistance(monitor.distance_cm)
    if gamma is not None:
        if gamma <= 0:
            raise DisplayError(f"gamma must be positive, got {gamma}")
        mon.setGamma(gamma)
    if notes is not None:
        mon.setNotes(notes)
    mon.setCalibDate()
    try:
        mon.save()
    except OSError as e:
        # PsychoPy writes into the user's own configuration directory, so the
        # thing that goes wrong here is a permission or a full disk — named,
        # rather than surfacing as a bare OSError from inside psychopy.
        raise DisplayError(
            f"could not write monitor {monitor.name!r} to {monitor_folder()}: {e}"
        ) from e

    path = monitor_file(monitor.name)
    if path is None:
        # save() reported nothing and wrote nothing: a read-only home
        # directory, most likely. Silence here would mean every later session
        # quietly running against an unregistered monitor.
        raise DisplayError(
            f"psychopy did not write a monitor file for {monitor.name!r} — "
            f"check that {monitor_folder()} is writable"
        )
    log.info("registered monitor %r with psychopy at %s", monitor.name, path)
    return path


def resolve(monitor: MonitorConfig) -> Any:
    """The PsychoPy ``Monitor`` a window for this rig should be opened against.

    Registered and in agreement with the config: that record, carrying
    whatever calibration it holds. Registered and *disagreeing*: a
    DisplayError, because the two describe the same panel differently and
    only a human knows which one is now true. Never registered: an in-memory
    monitor built from the config, which is what alhazen used before it could
    register at all — sessions on an unregistered rig keep working, they just
    have no stored calibration to inherit.
    """
    monitors = _psychopy_monitors()
    registration = lookup(monitor.name)
    drift = differences(monitor, registration)
    if drift:
        raise DisplayError(
            f"the rig config and PsychoPy disagree about monitor {monitor.name!r}:\n  "
            + "\n  ".join(drift)
            + "\nOne of them has been edited since the monitor was registered. Fix the "
            "rig config if the panel has not changed, then re-register it:\n"
            "  alhazen monitor register --rig <your rig yaml>"
        )
    if not registration.registered:
        log.info(
            "monitor %r is not registered with psychopy — using the rig config's geometry "
            "and no stored calibration (alhazen monitor register --rig <yaml> to add it)",
            monitor.name,
        )

    mon = monitors.Monitor(monitor.name, verbose=False, autoLog=False)
    # Stamped from the config in both branches. For a registered monitor these
    # are the values that were just checked, so it changes nothing; for an
    # unregistered one it is what makes the window agree with Screen. Not
    # saved either way — writing to the database is `monitor register`'s job,
    # and a session must not quietly change what other experiments read.
    mon.setSizePix((monitor.width_px, monitor.height_px))
    mon.setWidth(monitor.width_cm)
    mon.setDistance(monitor.distance_cm)
    return mon


__all__ = [
    "NOTES_PREFIX",
    "Registration",
    "differences",
    "format_gamma",
    "lookup",
    "monitor_file",
    "monitor_folder",
    "register",
    "registered_names",
    "resolve",
]
