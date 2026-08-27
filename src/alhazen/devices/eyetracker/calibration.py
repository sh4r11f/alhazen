"""EyeLink calibration graphics, drawn with the session's own window.

pylink runs calibration by *calling back* into a display object the host
program supplies: it asks for a target to be drawn at a screen position, hands
over the Host PC's camera image one palette-indexed line at a time, and polls
for key presses, which it maps to its own key codes. SR Research publishes an
example implementation of that callback surface; this module is alhazen's own,
written against the same documented interface so that nothing GPL-licensed has
to live inside an MIT-licensed package.

Two design choices that make this testable on a machine with no SDK at all:

- everything that is real work — assembling the camera image, drawing the
  crosshair overlays into it, translating key names and colour indices — lives
  in plain module-level functions and classes with no pylink and no psychopy
  in sight, and is unit-tested directly;
- the pylink subclass itself is defined *inside* :func:`make_calibration_graphics`,
  because subclassing ``pylink.EyeLinkCustomDisplay`` requires pylink at class
  definition time. It is a thin adapter: each callback forwards to the helpers
  above or to a few psychopy stimuli.

Coordinates: pylink speaks screen px (origin top-left, y down) for calibration
targets; the window draws in centered px (origin centre, y up). ``Screen`` does
that conversion, exactly as it does for gaze.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from alhazen.display.screen import Screen

log = logging.getLogger(__name__)

# Beep tones, in Hz, for the three things the tracker asks us to sound. A
# generated tone rather than a shipped sound file: alhazen ships no assets, and
# the beeps only need to be distinguishable by ear across the room.
TARGET_TONE_HZ = 1000.0
DONE_TONE_HZ = 1200.0
ERROR_TONE_HZ = 400.0
BEEP_SECONDS = 0.1

# How much the camera image is magnified on screen. The Host PC sends a small
# image (typically 384x320); shown 1:1 it is too small to judge a pupil by.
CAMERA_IMAGE_SCALE = 2

# Overlay colours for the camera image, by pylink colour index (RGB, 0-255).
# The Host PC uses these to mark what it has found in the eye image.
CROSSHAIR_COLORS: dict[str, tuple[int, int, int]] = {
    "CR_HAIR_COLOR": (255, 255, 255),  # corneal reflection crosshair
    "PUPIL_HAIR_COLOR": (255, 255, 255),  # pupil centre crosshair
    "PUPIL_BOX_COLOR": (0, 255, 0),  # box around the detected pupil
    "SEARCH_LIMIT_BOX_COLOR": (255, 0, 0),  # the region the tracker searches
    "MOUSE_CURSOR_COLOR": (255, 0, 0),
}
_DEFAULT_OVERLAY_COLOR = (255, 255, 255)

# psychopy key name -> the name of the pylink constant it maps to. Anything not
# listed is passed through as its own character code (letters, digits, space).
_NAMED_KEYS: dict[str, str] = {
    "f1": "F1_KEY",
    "f2": "F2_KEY",
    "f3": "F3_KEY",
    "f4": "F4_KEY",
    "f5": "F5_KEY",
    "f6": "F6_KEY",
    "f7": "F7_KEY",
    "f8": "F8_KEY",
    "f9": "F9_KEY",
    "f10": "F10_KEY",
    "pageup": "PAGE_UP",
    "pagedown": "PAGE_DOWN",
    "up": "CURS_UP",
    "down": "CURS_DOWN",
    "left": "CURS_LEFT",
    "right": "CURS_RIGHT",
    "return": "ENTER_KEY",
    "num_enter": "ENTER_KEY",
}

# Keys that are one character to the tracker but have a multi-character name.
_CHARACTER_KEYS: dict[str, str] = {
    "space": " ",
    "tab": "\t",
    "backspace": "\b",
    "escape": "\x1b",
    "minus": "-",
    "num_subtract": "-",
    "equal": "+",  # the tracker's CR-threshold keys are '+' and '-'
    "plus": "+",
    "num_add": "+",
    "period": ".",
    "comma": ",",
}

# Modifier bits pylink expects alongside a key code.
MOD_SHIFT = 1
MOD_CTRL = 64
MOD_ALT = 256


def resolve_key(key_name: str, modifiers: dict[str, bool], pylink_module: Any) -> tuple[int, int]:
    """Translate one psychopy key press into pylink's ``(code, modifier)``.

    Unknown keys become ``JUNK_KEY`` rather than being dropped: the tracker's
    own camera-setup screen tells the operator which keys it accepts, and
    silently swallowing the rest would make a mistyped key look like a frozen
    calibration.
    """
    if key_name in _NAMED_KEYS:
        code = int(getattr(pylink_module, _NAMED_KEYS[key_name]))
    elif key_name in _CHARACTER_KEYS:
        code = ord(_CHARACTER_KEYS[key_name])
    elif len(key_name) == 1:
        code = ord(key_name)
    else:
        code = int(getattr(pylink_module, "JUNK_KEY", 0))

    modifier = 0
    if modifiers.get("alt"):
        modifier = MOD_ALT
    elif modifiers.get("ctrl"):
        modifier = MOD_CTRL
    elif modifiers.get("shift"):
        modifier = MOD_SHIFT
    return code, modifier


def overlay_color(color_index: int, pylink_module: Any) -> tuple[int, int, int]:
    """RGB for one of pylink's camera-image colour indices."""
    for name, rgb in CROSSHAIR_COLORS.items():
        if color_index == getattr(pylink_module, name, object()):
            return rgb
    return _DEFAULT_OVERLAY_COLOR


class CameraImageBuilder:
    """Assembles the Host PC's eye image from the lines pylink hands over.

    The Host sends a palette (three lists of channel values) once, then the
    image one row at a time as indices into that palette. The row numbered
    ``totlines`` is the last one, and only then is a full frame available.

    Row 0 is the top of the image, and stays that way through to the screen —
    an accidentally flipped camera view would have the operator adjusting the
    wrong end of the eye.
    """

    def __init__(self) -> None:
        self._palette: np.ndarray | None = None
        self._rows: list[np.ndarray] = []

    def set_palette(self, red: list[int], green: list[int], blue: list[int]) -> None:
        self._palette = np.array(
            [red, green, blue], dtype=np.uint8
        ).T  # (n_colors, 3), row = one palette entry
        self._rows = []  # a new palette starts a new image

    def add_line(self, width: int, line: int, total_lines: int, buffer: Any) -> np.ndarray | None:
        """Add one row; return the finished ``(h, w, 3)`` uint8 image on the
        last row, else None."""
        if self._palette is None:
            raise RuntimeError("camera image line arrived before its palette")
        indices = np.asarray(buffer[:width], dtype=np.intp)
        # Clip rather than let an out-of-range index raise: the Host can send
        # an index past a short palette on the first frame after a mode change,
        # and a raise there would abort calibration over one bad pixel row.
        np.clip(indices, 0, len(self._palette) - 1, out=indices)
        self._rows.append(self._palette[indices])
        if line < total_lines:
            return None
        image = np.array(self._rows, dtype=np.uint8)
        self._rows = []
        return image


def draw_line_into(
    image: np.ndarray, x1: float, y1: float, x2: float, y2: float, color: tuple[int, int, int]
) -> None:
    """Draw a 1 px line into an image array, clipped to its bounds.

    Sampled rather than stepped (Bresenham): one sample per pixel of the
    longer axis is enough for a crosshair, and it keeps this readable.
    """
    height, width = image.shape[:2]
    steps = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
    xs = np.rint(np.linspace(x1, x2, steps)).astype(int)
    ys = np.rint(np.linspace(y1, y2, steps)).astype(int)
    inside = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
    image[ys[inside], xs[inside]] = color


def draw_lozenge_into(
    image: np.ndarray,
    x: float,
    y: float,
    width: float,
    height: float,
    color: tuple[int, int, int],
) -> None:
    """Draw the outline of a lozenge — a rectangle with semicircular ends —
    which is how the tracker marks the region it searches for the pupil."""
    if width <= 0 or height <= 0:
        return
    radius = min(width, height) / 2.0
    if width >= height:  # long axis horizontal: flat top and bottom, round ends
        left, right = x + radius, x + width - radius
        centre_y = y + radius
        draw_line_into(image, left, y, right, y, color)
        draw_line_into(image, left, y + height, right, y + height, color)
        _draw_arc_into(image, left, centre_y, radius, 90, 270, color)
        _draw_arc_into(image, right, centre_y, radius, -90, 90, color)
    else:  # long axis vertical
        top, bottom = y + radius, y + height - radius
        centre_x = x + radius
        draw_line_into(image, x, top, x, bottom, color)
        draw_line_into(image, x + width, top, x + width, bottom, color)
        _draw_arc_into(image, centre_x, top, radius, 180, 360, color)
        _draw_arc_into(image, centre_x, bottom, radius, 0, 180, color)


def _draw_arc_into(
    image: np.ndarray,
    cx: float,
    cy: float,
    radius: float,
    start_deg: float,
    end_deg: float,
    color: tuple[int, int, int],
) -> None:
    # One sample per pixel of arc length, so the outline has no gaps.
    steps = max(int(abs(end_deg - start_deg) / 360.0 * 2 * np.pi * radius), 2)
    angles = np.radians(np.linspace(start_deg, end_deg, steps))
    xs = np.rint(cx + radius * np.cos(angles)).astype(int)
    ys = np.rint(cy + radius * np.sin(angles)).astype(int)
    img_h, img_w = image.shape[:2]
    inside = (xs >= 0) & (xs < img_w) & (ys >= 0) & (ys < img_h)
    image[ys[inside], xs[inside]] = color


def make_calibration_graphics(
    tracker: Any,
    window: Any,
    screen: Screen,
    target_size_px: int = 24,
    foreground: tuple[float, float, float] = (-1.0, -1.0, -1.0),
) -> Any:
    """Build the object pylink calibrates through, for this window.

    Returns an instance of a ``pylink.EyeLinkCustomDisplay`` subclass defined
    here (the class body needs pylink, so it cannot exist at import time).
    Register it with ``pylink.openGraphicsEx(...)`` before calibrating.
    """
    import pylink
    from psychopy import event, visual

    class AlhazenCalibrationGraphics(pylink.EyeLinkCustomDisplay):
        """Adapter: pylink's callbacks in, this window's drawing out."""

        def __init__(self) -> None:
            super().__init__()
            self._window = window
            self._screen = screen
            self._tracker = tracker
            background = window.color
            # The standard EyeLink target: a disc with a hole, so the subject
            # has an unambiguous point to look at rather than a blob's centre.
            self._target_outer = visual.Circle(
                window,
                radius=target_size_px / 2.0,
                units="pix",
                fillColor=foreground,
                lineColor=foreground,
            )
            self._target_inner = visual.Circle(
                window,
                radius=target_size_px / 6.0,
                units="pix",
                fillColor=background,
                lineColor=background,
            )
            self._title = visual.TextStim(
                window, text="", units="pix", height=20, color=foreground, pos=(0, 0)
            )
            self._camera_image: Any = None
            self._builder = CameraImageBuilder()
            self._frame: np.ndarray | None = None
            self._image_size = (0, 0)
            self._mouse = event.Mouse(win=window, visible=False)
            self._beeps: dict[float, Any] = {}
            self._audio_failed = False

        # -- calibration display -------------------------------------------

        def setup_cal_display(self) -> None:
            self._window.flip()

        def clear_cal_display(self) -> None:
            self._window.flip()

        def exit_cal_display(self) -> None:
            self._window.flip()

        def record_abort_hide(self) -> None:
            return  # nothing of ours stays on screen between trials

        def erase_cal_target(self) -> None:
            self._window.flip()

        def draw_cal_target(self, x: float, y: float) -> None:
            # pylink gives the target in screen px; the window draws centered.
            pos = self._screen.screen_to_centered(x, y)
            self._target_outer.pos = pos
            self._target_inner.pos = pos
            self._target_outer.draw()
            self._target_inner.draw()
            self._window.flip()

        def alert_printf(self, msg: str) -> None:
            # The tracker's own warnings (bad calibration, lost camera setup)
            # go to the session log, where they stay with the run's data.
            log.warning("EyeLink: %s", msg)

        # -- camera image ---------------------------------------------------

        def setup_image_display(self, width: int, height: int) -> int:
            self._image_size = (width, height)
            return 1

        def image_title(self, text: str) -> None:
            self._title.text = text
            self._title.pos = (
                0,
                -(self._image_size[1] * CAMERA_IMAGE_SCALE) / 2.0 - 20,
            )

        def set_image_palette(self, r: list[int], g: list[int], b: list[int]) -> None:
            self._builder.set_palette(r, g, b)

        def draw_image_line(self, width: int, line: int, totlines: int, buff: Any) -> None:
            frame = self._builder.add_line(width, line, totlines, buff)
            if frame is None:
                return
            # The base class's draw_cross_hair() calls back into draw_line()
            # and draw_lozenge() below, which paint into this frame — so it has
            # to run while the frame is the current one, before presenting.
            self._frame = frame
            self.draw_cross_hair()
            self._present_frame(frame)
            self._frame = None

        def _present_frame(self, frame: np.ndarray) -> None:
            from PIL import Image

            height, width = frame.shape[:2]
            # Nearest-neighbour, not a smooth resample: this is a camera
            # image the operator judges a pupil edge from, and interpolating
            # would invent detail that is not in the data. Resampling.NEAREST
            # rather than the old Image.NEAREST alias, which modern Pillow
            # has moved.
            image = Image.fromarray(frame).resize(
                (width * CAMERA_IMAGE_SCALE, height * CAMERA_IMAGE_SCALE),
                Image.Resampling.NEAREST,
            )
            if self._camera_image is None:
                self._camera_image = visual.ImageStim(self._window, image=image, units="pix")
            else:
                self._camera_image.image = image
            self._camera_image.draw()
            self._title.draw()
            self._window.flip()

        def exit_image_display(self) -> None:
            self._camera_image = None
            self._window.flip()

        def draw_line(self, x1: float, y1: float, x2: float, y2: float, colorindex: int) -> None:
            if self._frame is None:
                return  # the tracker can ask outside a frame; nothing to mark
            draw_line_into(self._frame, x1, y1, x2, y2, overlay_color(colorindex, pylink))

        def draw_lozenge(
            self, x: float, y: float, width: float, height: float, colorindex: int
        ) -> None:
            if self._frame is None:
                return
            draw_lozenge_into(self._frame, x, y, width, height, overlay_color(colorindex, pylink))

        # -- operator input --------------------------------------------------

        def get_input_key(self) -> Any:
            keys = []
            for key_name, modifiers in event.getKeys(modifiers=True):
                code, modifier = resolve_key(key_name, modifiers, pylink)
                keys.append(pylink.KeyInput(code, modifier))
            return keys or None

        def get_mouse_state(self) -> tuple[tuple[float, float], int]:
            # Reported in camera-image px, which is the frame the Host PC uses
            # to move its search limits.
            x, y = self._mouse.getPos()
            width, height = self._image_size
            image_x = x / CAMERA_IMAGE_SCALE + width / 2.0
            image_y = height / 2.0 - y / CAMERA_IMAGE_SCALE
            pressed = int(any(self._mouse.getPressed()))
            return (
                (float(np.clip(image_x, 0, width)), float(np.clip(image_y, 0, height))),
                pressed,
            )

        # -- sound ------------------------------------------------------------

        def play_beep(self, beepid: int) -> None:
            if beepid in (
                getattr(pylink, "CAL_ERR_BEEP", -101),
                getattr(pylink, "DC_ERR_BEEP", -102),
            ):
                tone = ERROR_TONE_HZ
            elif beepid in (
                getattr(pylink, "CAL_GOOD_BEEP", -103),
                getattr(pylink, "DC_GOOD_BEEP", -104),
            ):
                tone = DONE_TONE_HZ
            else:
                tone = TARGET_TONE_HZ
            self._play_tone(tone)

        def _play_tone(self, tone_hz: float) -> None:
            """Beeps are a convenience, not data: a rig with no working audio
            device still calibrates. Logged once, then silence — a warning per
            target would bury the session log."""
            if self._audio_failed:
                return
            try:
                from psychopy import sound

                if tone_hz not in self._beeps:
                    self._beeps[tone_hz] = sound.Sound(tone_hz, secs=BEEP_SECONDS)
                self._beeps[tone_hz].play()
            except Exception:
                self._audio_failed = True
                log.warning(
                    "calibration beeps unavailable on this machine; calibrating silently",
                    exc_info=True,
                )

    return AlhazenCalibrationGraphics()
