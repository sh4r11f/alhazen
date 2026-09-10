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
# A message's box must fit on the screen. When the text is too tall for that
# at its usual size, the letters shrink until the box fits in this share of
# the screen's height, leaving a margin so the border is not on the bezel...
MESSAGE_SCREEN_FRACTION = 0.95
# ...but never below this share of their usual size. Instructions are read
# from a chin rest a metre or less away, and past this the text stops being
# something a subject reads comfortably, so shrinking further trades one
# failure for another. Beyond it the message is shown from its start and the
# overflow is logged as an error: the text has to be shortened.
MESSAGE_MIN_SCALE = 0.6

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


def split_menu_title(title: str) -> tuple[str, str | None]:
    """A menu heading's headline and the instruction after it, if it has one.

    Fault headings are written "WHAT HAPPENED — what to do about it"
    (session/runner.py, session/pause.py). Drawn whole at heading size, a
    long one wraps onto several big lines; drawn apart, the part a person
    reads across a room stays big and short, and the instruction sits under
    it at a size meant for reading from the rig. Only the first dash splits,
    so an instruction may contain dashes of its own. A title with no dash, or
    nothing after it, comes back whole.
    """
    headline, separator, instruction = title.partition(" — ")
    if not separator or not instruction.strip():
        return title, None
    return headline.strip(), instruction.strip()


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
        usual = max(18.0, self._monitor.height_px * 0.022)
        screen_height = float(self._monitor.height_px)
        target = screen_height * MESSAGE_SCREEN_FRACTION
        smallest = usual * MESSAGE_MIN_SCALE

        # The box has to fit on the screen. A message taller than the window
        # used to be drawn centred and cropped at both ends, with nothing said:
        # the first and last sentences of a subject's instructions went off the
        # top and bottom of the rig's screen. So the text is laid out, and if
        # its box is taller than the target the letters shrink and it is laid
        # out again. The measure shrinks with the letters, so a line keeps the
        # same number of characters and only the size changes.
        height = usual
        usual_box_height = None
        for _ in range(8):
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
            box_height = text_h + 2 * MESSAGE_PADDING[1] * height
            if usual_box_height is None:
                usual_box_height = box_height
            if box_height <= target or height <= smallest:
                break
            # Height is close to proportional to letter size at a fixed number
            # of characters per line, so one step usually lands; the loop is
            # for a measure capped by the screen's width, where it is not.
            height = max(smallest, height * target / box_height * 0.98)

        if height < usual:
            log.warning(
                "a message is too tall for the screen at its usual size (%.0f px on a %.0f px "
                "screen), so its letters were shrunk to %.0f%% of usual to fit. Shorten the "
                "text to show it at full size.",
                usual_box_height,
                screen_height,
                100 * height / usual,
            )

        # Centred on the screen, unless even the smallest letters do not fit.
        # Then the box's top goes at the screen's top, so the message is read
        # from its start and only its end is lost, and that is an error: a
        # subject cannot read all of it.
        centre_y = 0.0
        if box_height > screen_height:
            centre_y = screen_height / 2.0 - box_height / 2.0
            log.error(
                "a message does not fit on the screen even at %.0f%% of its usual letter size: "
                "%.0f px of it are below the bottom of the screen. Its start is shown. Shorten "
                "the text.",
                100 * MESSAGE_MIN_SCALE,
                box_height - screen_height,
            )
        msg.pos = (-text_w / 2.0, centre_y)
        panel = self._panel(
            width=text_w + 2 * MESSAGE_PADDING[0] * height,
            height=box_height,
            color=MESSAGE_OUTLINE,
            fill=MESSAGE_PANEL_FILL,
        )
        if centre_y:
            panel.pos = (0.0, centre_y)
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

        The parts are stacked by their MEASURED heights. They used to sit at
        fixed distances below the panel's top, which left room for one line of
        heading. A fault heading is a sentence — "6 TRIALS FAILED IN A ROW —
        last NO_SACCADE; check the calibration (V), the subject, and the
        stimulus before resuming" — and at heading size it wrapped onto more
        lines that were drawn straight over the rows, so the screen an
        experimenter most needed to read was unreadable. Now a heading too
        long for one line is drawn as its headline at heading size with the
        instruction beneath it at reading size (:func:`split_menu_title`),
        every part starts below the one above it, and the panel grows when
        the content needs more room than its usual share of the screen.
        """
        self._require_open()
        from psychopy import visual

        width, height = self._monitor.width_px, self._monitor.height_px
        text_height = max(16.0, height * 0.019)
        heading_size = text_height * 1.6
        instruction_size = text_height * 1.15
        panel_width = width * MENU_PANEL_FRACTION[0]
        text_width = panel_width * 0.9
        # The rows' measure: wide enough for the longest row this ever draws,
        # but never wider than the panel. pyglet centres the wrap width, so the
        # rows start half of it left of centre, and on a 4:3 or 5:4 display 46
        # text heights is wider than the panel — the rows would begin outside
        # it. Not a fraction of the window alone, either: on an ultrawide that
        # is one enormous line.
        rows_width = min(text_height * 46, text_width)

        def centred(text: str, size: float) -> Any:
            # Placed at the origin for now: a text's height is only known once
            # it has been laid out, and the layout below needs every height.
            return visual.TextStim(
                self.window,
                text=text,
                font=HEADING_FONT,
                height=size,
                color=color,
                colorSpace="rgb",
                alignText="center",
                anchorHoriz="center",
                anchorVert="top",
                pos=(0, 0),
                wrapWidth=text_width,
                units="pix",
            )

        # The whole heading first, split only if it does not fit on one line.
        # A short heading such as "BLOCK 1 OF 2 COMPLETE — REST" reads best
        # whole, and its last word is the point of it. One laid-out line is
        # about 1.2 text heights tall, so anything past 1.8 has wrapped.
        heading = centred(title, heading_size)
        _, whole_height = self._text_extent(
            heading, wrap_width=text_width, line_height=heading_size
        )
        instruction = None
        headline, rest_of_title = split_menu_title(title)
        if rest_of_title is not None and whole_height > heading_size * 1.8:
            heading = centred(headline, heading_size)
            instruction = centred(rest_of_title, instruction_size)
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
            pos=(0, 0),
            wrapWidth=rows_width,
            units="pix",
        )

        # Every part measured, then stacked top to bottom with fixed gaps.
        padding = text_height * 1.2
        gap_under_heading = text_height * 0.5
        gap_above_rows = text_height * 1.3
        _, heading_height = self._text_extent(
            heading, wrap_width=text_width, line_height=heading_size
        )
        instruction_height = 0.0
        if instruction is not None:
            _, instruction_height = self._text_extent(
                instruction, wrap_width=text_width, line_height=instruction_size
            )
        _, rows_height = self._text_extent(rows, wrap_width=rows_width, line_height=text_height)
        needed = padding + heading_height + gap_above_rows + rows_height + padding
        if instruction is not None:
            needed += gap_under_heading + instruction_height

        # The usual share of the screen, unless the content needs more. A menu
        # taller than the screen is still drawn, from the top, but it is said:
        # rows off the bottom of the screen are keys nobody can see.
        panel_height = max(height * MENU_PANEL_FRACTION[1], needed)
        if panel_height > height:
            log.warning(
                "the pause menu needs %.0f px and the screen is %.0f px tall: its last rows "
                "are off the bottom of the screen",
                panel_height,
                height,
            )
            panel_height = float(height)

        # Anchored to the panel's top rather than centred: the heading stays
        # put as the body grows and shrinks with the rig's wiring, or the one
        # word an experimenter looks for would move every session. It moves
        # only when the menu outgrows its usual panel.
        y = panel_height / 2.0 - padding
        heading.pos = (0, y)
        y -= heading_height
        if instruction is not None:
            y -= gap_under_heading
            instruction.pos = (0, y)
            y -= instruction_height
        rows.pos = (0, y - gap_above_rows)

        panel = self._panel(
            width=panel_width, height=panel_height, color=color, fill=MENU_PANEL_FILL
        )
        panel.draw()
        heading.draw()
        if instruction is not None:
            instruction.draw()
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
