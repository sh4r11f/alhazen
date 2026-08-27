"""The PsychoPy display backend.

PsychoPy is imported lazily, inside ``open()`` — importing this module (as
every headless test and analysis machine transitively does) must never
require the renderer to be installed. A missing PsychoPy raises DisplayError
naming the extra to install, mirroring how vendor SDKs are handled
everywhere in this package.
"""

from __future__ import annotations

from typing import Any

from alhazen.config.models import MonitorConfig
from alhazen.display.monitors import resolve as resolve_monitor
from alhazen.errors import DisplayError


class PsychoPyDisplay:
    kind = "psychopy"

    def __init__(self, monitor: MonitorConfig, windowed: bool = False) -> None:
        self._monitor = monitor
        self._windowed = windowed
        self.window: Any = None
        # None until a calibration is applied.
        self.gamma: float | None = None

    def open(self) -> None:
        try:
            from psychopy import visual
        except ImportError as e:
            raise DisplayError(
                "the PsychoPy display backend needs psychopy installed — "
                "pip install 'alhazen[psychopy]'"
            ) from e

        # The rig's monitor as PsychoPy knows it: the registered record when
        # `alhazen monitor register` has written one (so the window inherits
        # whatever calibration it carries), the config's geometry alone when
        # it has not, and a loud error when the two disagree — see
        # display.monitors. Passing no `gamma=` to the Window is deliberate:
        # PsychoPy then applies the gamma stored on this monitor, and the
        # session builder applies alhazen's own measured fit on top of it as
        # the same absolute value, never a second correction.
        mon = resolve_monitor(self._monitor)
        # Units are pixels on purpose: alhazen owns all deg<->px conversion in
        # display.screen.Screen, exactly once per value, so recorded positions
        # invert back to configured ones bit-for-bit. Letting the renderer
        # also convert would create a second, subtly different model.
        self.window = visual.Window(
            size=(self._monitor.width_px, self._monitor.height_px),
            fullscr=self._monitor.fullscreen and not self._windowed,
            screen=self._monitor.screen_index,
            monitor=mon,
            units="pix",
            color=(0, 0, 0),
            allowGUI=self._windowed,
        )

    def close(self) -> None:
        if self.window is not None:
            self.window.close()
            self.window = None

    def flip(self, clear: bool = True) -> None:
        self._require_open()
        self.window.flip(clearBuffer=clear)

    def measure_refresh_rate(self, n_flips: int) -> float:
        self._require_open()
        # PsychoPy's own frame-interval machinery, over freshly-recorded
        # intervals only (nIdentical <= what we record), no smoothing.
        rate = self.window.getActualFrameRate(
            nIdentical=min(10, n_flips), nMaxFrames=n_flips, nWarmUpFrames=10
        )
        if rate is None:
            raise DisplayError(
                "could not measure a stable refresh rate — the display is dropping frames "
                "at rest; close other applications / check the video mode before running"
            )
        return float(rate)

    def show_message(self, text: str) -> None:
        self._require_open()
        from psychopy import visual

        # A fresh TextStim per call: messages appear a handful of times per
        # session, nowhere near the per-frame hot path.
        msg = visual.TextStim(
            self.window, text=text, color=(1, 1, 1), wrapWidth=self._monitor.width_px * 0.8
        )
        msg.draw()
        self.window.flip()

    def set_gamma(self, gamma: float) -> None:
        """Apply a measured gamma correction to the open window.

        psychopy applies it through the window's own gamma ramp, which is
        what every stimulus then inherits — rather than each stimulus
        correcting itself and one of them forgetting.
        """
        self._require_open()
        if gamma <= 0:
            raise DisplayError(f"gamma must be positive, got {gamma}")
        self.window.gamma = gamma
        self.gamma = gamma

    def _require_open(self) -> None:
        if self.window is None:
            raise DisplayError("display is not open — call open() first")
