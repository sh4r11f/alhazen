"""Screen geometry: the one place pixels and degrees convert.

Two coordinate frames, named explicitly everywhere:

- **screen px**: origin top-left, y grows down — what trackers and window
  systems natively speak.
- **centered px**: origin at screen center, y grows up — what stimulus
  placement and phase logic use.

Degree conversion uses the *linear* small-angle model in both directions
(``px_per_deg = px_per_cm * distance_cm * tan(1°)``). The invariant that
matters is that ``px2deg`` is the *exact inverse* of the ``deg2px`` that
placed the stimulus. Pairing it with an arctangent "more physical" inverse
instead silently mis-measures every eccentric position, by up to a third of
the effect size at the eccentricities a saccade task actually uses. Whatever
model places stimuli must be the model that reads positions back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alhazen.config.models import MonitorConfig

_TAN_ONE_DEG = math.tan(math.radians(1.0))


@dataclass(frozen=True)
class Screen:
    width_px: int
    height_px: int
    px_per_deg: float

    @classmethod
    def from_monitor(cls, monitor: MonitorConfig) -> Screen:
        px_per_cm = monitor.width_px / monitor.width_cm
        return cls(
            width_px=monitor.width_px,
            height_px=monitor.height_px,
            px_per_deg=px_per_cm * monitor.distance_cm * _TAN_ONE_DEG,
        )

    def deg2px(self, deg: float) -> float:
        return deg * self.px_per_deg

    def px2deg(self, px: float) -> float:
        return px / self.px_per_deg

    def screen_to_centered(self, sx: float, sy: float) -> tuple[float, float]:
        return (sx - self.width_px / 2.0, self.height_px / 2.0 - sy)

    def centered_to_screen(self, cx: float, cy: float) -> tuple[float, float]:
        return (cx + self.width_px / 2.0, self.height_px / 2.0 - cy)


def within_radius(point: tuple[float, float], center: tuple[float, float], radius: float) -> bool:
    dx, dy = point[0] - center[0], point[1] - center[1]
    return dx * dx + dy * dy <= radius * radius
