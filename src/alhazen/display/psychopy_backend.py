"""The PsychoPy display backend.

PsychoPy is imported lazily, inside ``open()`` — importing this module (as
every headless test and analysis machine transitively does) must never
require the renderer to be installed. A missing PsychoPy raises DisplayError
naming the extra to install, mirroring how vendor SDKs are handled
everywhere in this package.
"""

from __future__ import annotations

import importlib
import logging
import time
from pathlib import Path
from typing import Any

from alhazen.config.models import MonitorConfig
from alhazen.display.monitors import resolve as resolve_monitor
from alhazen.display.palette import TERMINAL_FILL, TERMINAL_GREEN, TERMINAL_TEXT
from alhazen.errors import DisplayError

log = logging.getLogger(__name__)

# The pause menu's heading face: a humanist sans.
HEADING_FONT = "Noto Sans"

# The face for anything laid out in columns or meant to look like a terminal:
# the pause menu's rows (its key column is aligned with spaces) and every
# message box.
MONO_FONT = "DejaVu Sans Mono"

# Neither face is assumed to be installed. A rig is a fresh Windows box more
# often than a desktop Linux, and pyglet draws a face it cannot find in the
# system default WITHOUT A WORD — the key column then drifts and nothing says
# why. So open() registers each missing face from a TTF that every PsychoPy
# install carries (Noto Sans in PsychoPy's own assets, DejaVu Sans Mono in
# matplotlib's, which PsychoPy depends on), and warns, naming the face, when
# even that fails. _bundled_font_files says where the files are.
BUNDLED_FONT_FILES = {
    HEADING_FONT: ("psychopy", "assets/fonts/NotoSans-Regular.ttf"),
    MONO_FONT: ("matplotlib", "fonts/ttf/DejaVuSansMono.ttf"),
}

# What a message box looks like: a terminal. Monospace text in a pale green
# on a near-black panel with a green outline, sized to what it says — a
# one-line "stage: 2" gets a small box, a page of instructions a large one.
# The green is the session's "information" colour (display.palette); the
# pause menu keeps orange and a fault keeps red, so the border colour alone
# says which of the three a panel is.
MESSAGE_FONT = MONO_FONT
MESSAGE_COLOR = TERMINAL_TEXT
MESSAGE_OUTLINE = TERMINAL_GREEN
MESSAGE_PANEL_FILL = TERMINAL_FILL
# Padding between the text and the box's edge, in text heights: two on each
# side, one and a half above and below.
MESSAGE_PADDING = (2.0, 1.5)

# How much of the panel the menu's dark backing covers, and how far inside it
# the text sits. The backing exists so the menu reads as a panel laid over a
# stopped session rather than as text that happens to be orange.
MENU_FONT = MONO_FONT
MENU_PANEL_FRACTION = (0.62, 0.72)
MENU_PANEL_FILL = (-0.55, -0.55, -0.55)


def _bundled_font_files() -> dict[str, Path]:
    """The TTF each face can be registered from, for the packages importable
    here. Resolved at open() rather than at import: neither package is a
    dependency of alhazen itself, only of its psychopy extra, so a headless
    machine has neither and must still import this module."""
    files: dict[str, Path] = {}
    for face, (package, relative) in BUNDLED_FONT_FILES.items():
        try:
            module = importlib.import_module(package)
            # matplotlib keeps its data (fonts included) outside the package
            # and says where; PsychoPy's assets live inside its own.
            if package == "matplotlib":
                base = Path(module.get_data_path())
            elif module.__file__ is not None:
                base = Path(module.__file__).parent
            else:
                # A namespace package has no file of its own, and so no
                # directory to look for assets under.
                raise ImportError(f"{package} has no file on disk")
        except (ImportError, AttributeError) as e:
            log.debug("no bundled copy of the %r face: %s is not importable (%s)", face, package, e)
            continue
        files[face] = base / relative
    return files


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
        self._register_fonts()
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

    def _register_fonts(self) -> None:
        """Make the faces the panels draw with available — loudly when one is not.

        pyglet, which draws PsychoPy's text, substitutes a face it cannot find
        with the system default and logs nothing, so a rig without DejaVu Sans
        Mono would draw the pause menu's key column in a proportional face
        and it would silently stop lining up. Each face the machine lacks is
        registered from the copy a PsychoPy install carries (BUNDLED_FONT_FILES);
        a face that cannot be had even that way gets a warning naming it. Not
        an error: a menu in the wrong face is still a menu, and no session
        should refuse to run over typography.
        """
        import pyglet.font

        bundled: dict[str, Path] | None = None
        for face in (HEADING_FONT, MONO_FONT):
            if pyglet.font.have_font(face):
                continue
            # Looked up once, and only when a face is missing: importing the
            # packages it lives in is not free.
            if bundled is None:
                bundled = _bundled_font_files()
            path = bundled.get(face)
            if path is not None and path.is_file():
                try:
                    pyglet.font.add_file(str(path))
                except Exception:  # a file pyglet cannot parse; the warning below says so
                    log.debug("pyglet refused the font file %s", path, exc_info=True)
                if pyglet.font.have_font(face):
                    log.info("the %r face is not installed; registered it from %s", face, path)
                    continue
            log.warning(
                "the %r face is not installed and no bundled copy could be registered "
                "(looked for %s); the panels will be drawn in the system default face, "
                "so columns aligned with spaces may not line up",
                face,
                path if path is not None else "one in a package that is not importable",
            )

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
        """Draw the message in a terminal-style box over the session, and flip.

        The box is what makes a message read as the session *saying*
        something rather than as a caption left on screen: a near-black panel
        sized to the text, outlined in green, with the text in a monospace
        face — the look of a terminal, on purpose, and in a colour that is
        neither the pause menu's orange nor a fault's red.
        """
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
        #   is a multiple of the text height (a monospace glyph is ~0.6 of it).
        # - **Anchored at its left edge, then moved left by half its width.**
        #   Not anchored at the centre: pyglet centres a wrapped text's WRAP
        #   width, not the text, so a centre-anchored "stage: 2" starts half
        #   a wrap width (hundreds of pixels) left of the middle — and of a
        #   box that hugs the text. With the left edge as the anchor and the
        #   text's own measured width, the text and the box share a centre.
        height = max(18.0, self._monitor.height_px * 0.022)
        wrap_width = min(self._monitor.width_px * 0.8, height * 34)
        msg = visual.TextStim(
            self.window,
            text=text,
            font=MESSAGE_FONT,
            height=height,
            color=MESSAGE_COLOR,
            colorSpace="rgb",
            alignText="left",
            anchorHoriz="left",
            pos=(0, 0),
            wrapWidth=wrap_width,
            units="pix",
        )
        # The box hugs the text: its size comes from the laid-out text's
        # bounding box plus a margin, so a short notice and a page of
        # instructions each get a box of their own size.
        text_w, text_h = self._text_extent(msg, wrap_width=wrap_width, line_height=height)
        msg.pos = (-text_w / 2.0, 0.0)
        panel = self._panel(
            width=text_w + 2 * MESSAGE_PADDING[0] * height,
            height=text_h + 2 * MESSAGE_PADDING[1] * height,
            color=MESSAGE_OUTLINE,
            fill=MESSAGE_PANEL_FILL,
        )
        # The instructions are the first frame after the build, and on the rig
        # the dashboard's browser window arrived at that moment and took the
        # foreground; Windows never presented the frame, and nothing flips
        # again while the runner waits for a key, so the subject's screen kept
        # the previous one. Claim the foreground, and present twice.
        self._bring_to_front()
        for _ in range(2):
            panel.draw()
            msg.draw()
            self.window.flip()
            time.sleep(0.05)

    def _text_extent(
        self, stim: Any, *, wrap_width: float, line_height: float
    ) -> tuple[float, float]:
        """The laid-out text's size in pixels, (width, height).

        PsychoPy reports it as ``boundingBox`` once the text is set, which a
        TextStim does on construction. A renderer that cannot say (an
        unexpected backend, a text object that has not been laid out) gets
        an estimate from the wrap width and the line count instead — and a
        warning, because a box that does not fit its text is worth hearing
        about even though the text itself is still on screen.
        """
        try:
            width, height = stim.boundingBox
            if width > 0 and height > 0:
                return float(width), float(height)
        except Exception:  # a fake or foreign text object without a layout
            log.debug("the text object reported no bounding box", exc_info=True)
        lines = str(stim.text).count("\n") + 1
        log.warning(
            "could not measure the message text; sizing its box from the wrap width and "
            "%d line(s) instead",
            lines,
        )
        return float(wrap_width), float(lines * line_height * 1.2)

    def _panel(
        self,
        *,
        width: float,
        height: float,
        color: tuple[float, float, float],
        fill: tuple[float, float, float],
    ) -> Any:
        """The bordered backing every panel is drawn on: a filled rectangle
        centred on the screen with an outline in the panel's colour. The
        outline's weight scales with the screen so it is a line on a 2160-row
        display and not a hairline."""
        from psychopy import visual

        return visual.Rect(
            self.window,
            width=width,
            height=height,
            fillColor=fill,
            lineColor=color,
            lineWidth=max(2.0, self._monitor.height_px * 0.003),
            units="pix",
        )

    def _bring_to_front(self) -> None:
        """Ask the OS to make this window the foreground one, if it can."""
        activate = getattr(getattr(self.window, "winHandle", None), "activate", None)
        if activate is None:
            return
        try:
            activate()
        except Exception:  # a window that cannot be raised is not a reason to stop
            log.debug("could not bring the window to the front", exc_info=True)

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

        panel = self._panel(
            width=width * MENU_PANEL_FRACTION[0],
            height=height * MENU_PANEL_FRACTION[1],
            color=color,
            fill=MENU_PANEL_FILL,
        )
        # Anchored to the panel's top rather than centred: the heading has to
        # stay put as the body grows and shrinks with the rig's wiring, or the
        # one word an experimenter looks for moves every session.
        panel_top = height * MENU_PANEL_FRACTION[1] / 2.0
        heading = visual.TextStim(
            self.window,
            text=title,
            font=HEADING_FONT,
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
            # Wide enough for the longest row this ever draws, but never wider
            # than the panel: pyglet centres the wrap width, so the rows start
            # half of it left of centre, and on a 4:3 or 5:4 display 46 text
            # heights is wider than the panel — the rows would begin outside
            # it. Not a fraction of the window alone, either: on an ultrawide
            # that is one enormous line.
            wrapWidth=min(text_height * 46, width * MENU_PANEL_FRACTION[0] * 0.9),
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
