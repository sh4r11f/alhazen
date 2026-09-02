"""Eye-tracker backends and the one place a backend name is resolved.

``make_tracker`` is shared by session build and ``check-rig`` on purpose: a
clean check-rig is only predictive of a real session if it constructs the
exact same objects that session would.
"""

from __future__ import annotations

from alhazen.config.models import EyeTrackerConfig
from alhazen.core.clock import Clock
from alhazen.devices.eyetracker.eyelink import EyeLinkTracker, is_missing_gaze
from alhazen.devices.eyetracker.messages import TrackerMessageSubscriber
from alhazen.devices.eyetracker.mouse_sim import MouseSimTracker
from alhazen.devices.eyetracker.protocol import (
    CalibrationResult,
    CameraFrame,
    EyeTracker,
    GazeSample,
    HostShape,
    ProgressHook,
)
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.devices.eyetracker.viewpixx import ViewPixxTracker
from alhazen.display.backend import DisplayBackend
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError


def make_tracker(
    cfg: EyeTrackerConfig,
    display: DisplayBackend | None,
    screen: Screen,
    clock: Clock,
) -> EyeTracker:
    """Construct the tracker a rig config names.

    ``scripted`` is rejected here rather than quietly falling back: it is a
    test double whose trajectory can only be supplied in code, so a rig YAML
    naming it is a broken config, and a session that "ran" on replayed gaze
    would be worse than one that refused to start.
    """
    if cfg.backend == "eyelink":
        return EyeLinkTracker(cfg, display, screen, clock)
    if cfg.backend == "viewpixx":
        return ViewPixxTracker(cfg, display, screen, clock)
    if cfg.backend == "mouse_sim":
        if display is None:
            raise ConfigError(
                "eyetracker backend 'mouse_sim' needs an open display to read the mouse from"
            )
        return MouseSimTracker(display, screen, clock)
    raise ConfigError(
        f"eyetracker backend {cfg.backend!r} is test-only: it replays a gaze trajectory that "
        f"only test code can supply. Use 'eyelink' or 'viewpixx' on the rig, or 'mouse_sim' "
        f"for development."
    )


__all__ = [
    "CalibrationResult",
    "CameraFrame",
    "EyeLinkTracker",
    "EyeTracker",
    "GazeSample",
    "HostShape",
    "MouseSimTracker",
    "ProgressHook",
    "ScriptedTracker",
    "TrackerMessageSubscriber",
    "ViewPixxTracker",
    "is_missing_gaze",
    "make_tracker",
]
