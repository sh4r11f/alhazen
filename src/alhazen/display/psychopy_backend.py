"""The PsychoPy display backend.

PsychoPy is imported lazily, inside ``open()`` — importing this module (as
every headless test and analysis machine transitively does) must never
require the renderer to be installed. A missing PsychoPy raises DisplayError
naming the extra to install, mirroring how vendor SDKs are handled
everywhere in this package.
"""

from __future__ import annotations

import logging
from typing import Any

from alhazen.config.models import MonitorConfig
from alhazen.display.monitors import resolve as resolve_monitor
from alhazen.errors import DisplayError

# What on-screen messages look like. A humanist sans at slightly-off-white:
# pure white on the mid-grey background is a harsher edge than a subject
# reading a paragraph needs. PsychoPy ships Open Sans, so no rig has to
# install anything, and it falls back to the system sans if it is missing.
log = logging.getLogger(__name__)

MESSAGE_FONT = "Open Sans"
MESSAGE_COLOR = (0.82, 0.82, 0.86)

# The pause menu's key column is aligned with spaces, so it needs a monospace
# face or the alignment it depends on is lost. DejaVu Sans Mono is on every
# desktop Linux and ships with matplotlib, so no rig has to install it; the
# fallback is whatever the system calls monospace.
MENU_FONT = "DejaVu Sans Mono"
# How much of the panel the menu's dark backing covers, and how far inside it
# the text sits. The backing exists so the menu reads as a panel laid over a
# stopped session rather than as text that happens to be orange.
MENU_PANEL_FRACTION = (0.62, 0.72)
MENU_PANEL_FILL = (-0.55, -0.55, -0.55)


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
        self._check_pixels_are_what_the_config_says()

    def _check_pixels_are_what_the_config_says(self) -> None:
        """Refuse to run if the drawing surface is not the size the rig claims.

        Everything downstream is degrees of visual angle computed from
        ``width_px / width_cm`` (display.screen.Screen), and stimuli are drawn
        in ``units="pix"``. Those pixels are the **framebuffer's**, so if the
        framebuffer is not the size the config describes, every stimulus is
        the wrong physical size and every recorded position is wrong by the
        same factor — with nothing at runtime to say so.

        The case this exists for is a **Retina Mac**, where the framebuffer is
        two device pixels per point in each direction. PsychoPy is explicit
        that ``units="pix"`` then refers to the small Retina pixels and that
        ``frameBufferSize`` is where to read them, and pyglet has forced
        Retina on Retina-capable screens since 1.3, so it cannot be opted out
        of. A config carrying a Mac's *logical* resolution silently halves
        every size on that machine. But the check is general: a fullscreen
        window that landed on the wrong monitor, or an OS display-scaling
        setting, fail it the same way and for the same reason.

        Only enforced fullscreen, because that is the only case where the
        framebuffer is supposed to be the whole panel. A deliberately windowed
        run is smaller by definition; it gets a warning if the window cannot
        hold what the config describes, since a clipped stimulus is worth
        hearing about even in a dev session.
        """
        buffer_size = self._frame_buffer_size()
        if buffer_size is None:
            log.warning(
                "this PsychoPy backend does not report a framebuffer size, so the "
                "drawing surface could not be checked against the rig config"
            )
            return

        configured = (self._monitor.width_px, self._monitor.height_px)
        client = tuple(int(v) for v in self.window.clientSize)
        scale = buffer_size[0] / client[0] if client[0] else 1.0
        if scale != 1.0:
            log.info(
                "display is scaled %.3gx: %dx%d framebuffer pixels over a %dx%d point "
                "window (a Retina or HiDPI screen)",
                scale,
                *buffer_size,
                *client,
            )

        fullscreen = self._monitor.fullscreen and not self._windowed
        if not fullscreen:
            if buffer_size[0] < configured[0] or buffer_size[1] < configured[1]:
                log.warning(
                    "windowed run: the %dx%d framebuffer is smaller than the %dx%d the rig "
                    "config describes, so a stimulus near the edge of the screen will be "
                    "clipped. Sizes in degrees are still correct.",
                    *buffer_size,
                    *configured,
                )
            return

        if buffer_size != configured:
            raise DisplayError(
                f"the fullscreen drawing surface is {buffer_size[0]}x{buffer_size[1]} pixels "
                f"but the rig config says the monitor is {configured[0]}x{configured[1]}. "
                f"Every stimulus size is computed from that number, so running would make "
                f"each one wrong by {buffer_size[0] / configured[0]:.3g}x.\n"
                f"  - On a Retina/HiDPI screen, width_px and height_px must be the panel's "
                f"NATIVE pixel count, not its logical resolution — here, "
                f"{buffer_size[0]} and {buffer_size[1]} — with width_cm the physical width "
                f"of that same panel.\n"
                f"  - Otherwise check screen_index ({self._monitor.screen_index}) and the "
                f"desktop's display-scaling setting."
            )

    def _frame_buffer_size(self) -> tuple[int, int] | None:
        """The drawing surface in device pixels, or None if unreported.

        ``frameBufferSize`` is the attribute that differs from the window size
        on a Retina screen; ``size`` is the viewport and agrees with it in
        every case alhazen opens a window for. Reading the first and falling
        back to the second keeps this working on a backend that does not
        implement it.
        """
        for attribute in ("frameBufferSize", "size"):
            value = getattr(self.window, attribute, None)
            if value is not None and len(value) >= 2:
                return (int(value[0]), int(value[1]))
        return None

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
        #
        # Three departures from TextStim's defaults, because the defaults were
        # chosen for a much smaller screen than a modern rig has:
        #
        # - **Size scales with the panel.** The default height in pixel units
        #   is 20 px — a legible paragraph on a 768-line CRT, an unreadable
        #   smear on a 2160-line display. As a fraction of the panel's height
        #   instead, instructions are the same physical size on every rig.
        # - **Lines are left-aligned; the block stays centred.** Centred prose
        #   has a ragged LEFT edge, so the eye hunts for the start of each
        #   line. Fine for one word, bad for instructions a subject is asked
        #   to read and follow.
        # - **Line length is bounded by the text, not the monitor.** A
        #   wrapWidth of 80% of the window is 6000 px on an ultrawide, i.e.
        #   one enormous line. The readable measure is ~60 characters, which
        #   is a multiple of the text height.
        height = max(18.0, self._monitor.height_px * 0.022)
        msg = visual.TextStim(
            self.window,
            text=text,
            font=MESSAGE_FONT,
            height=height,
            color=MESSAGE_COLOR,
            alignText="left",
            anchorHoriz="center",
            pos=(0, 0),
            wrapWidth=min(self._monitor.width_px * 0.8, height * 34),
            units="pix",
        )
        msg.draw()
        self.window.flip()

    def show_menu(self, title: str, body: str, *, color: tuple[float, float, float]) -> None:
        """Draw the menu over whatever is on screen, and flip.

        Three parts, in one flip: a dark panel with a border in the menu's
        colour, the heading, and the rows. The panel is what makes this read
        as a modal state rather than as a caption — a session's own background
        is mid-grey, and coloured text alone on mid-grey does not say "stopped"
        from across the room the way a bordered panel does.

        Sizes come off the panel's height, exactly as ``show_message`` does, so
        the menu is the same physical size on a 768-line CRT and a 2160-line
        display instead of shrinking to nothing on the second.
        """
        self._require_open()
        from psychopy import visual

        width, height = self._monitor.width_px, self._monitor.height_px
        text_height = max(16.0, height * 0.019)

        panel = visual.Rect(
            self.window,
            width=width * MENU_PANEL_FRACTION[0],
            height=height * MENU_PANEL_FRACTION[1],
            fillColor=MENU_PANEL_FILL,
            lineColor=color,
            lineWidth=max(2.0, height * 0.003),
            units="pix",
        )
        # Anchored to the panel's top rather than centred: the heading has to
        # stay put as the body grows and shrinks with the rig's wiring, or the
        # one word an experimenter looks for moves every session.
        panel_top = height * MENU_PANEL_FRACTION[1] / 2.0
        heading = visual.TextStim(
            self.window,
            text=title,
            font=MESSAGE_FONT,
            height=text_height * 1.6,
            color=color,
            colorSpace="rgb",
            alignText="center",
            anchorHoriz="center",
            anchorVert="top",
            pos=(0, panel_top - text_height * 1.2),
            wrapWidth=width * MENU_PANEL_FRACTION[0] * 0.9,
            units="pix",
        )
        rows = visual.TextStim(
            self.window,
            text=body,
            font=MENU_FONT,
            height=text_height,
            color=color,
            colorSpace="rgb",
            alignText="left",
            anchorHoriz="center",
            anchorVert="top",
            pos=(0, panel_top - text_height * 4.2),
            # Wide enough for the longest row this ever draws. Not a fraction
            # of the window: on an ultrawide that is one enormous line.
            wrapWidth=text_height * 46,
            units="pix",
        )
        panel.draw()
        heading.draw()
        rows.draw()
        self.flip()

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
