"""The calibration target: what it looks like, how it moves, which picture.

A calibration shows one target at a time and the subject looks at it. Two
choices decide what that target is (``eyetracker.calibration_target``,
config/models.py ``CalibrationTargetConfig``), independent of each other and
of everything else about the calibration:

- **appearance**: the standard disc with a hole, a picture chosen by name, or
  a random picture from a set (alhazen's calibration pictures, package data
  under ``src/alhazen/calibration_images/``);
- **motion**: still, or pulsating — swelling and shrinking smoothly about a
  centre that never moves.

Neither changes where a target goes, how many there are, how long one stays
up, how gaze is sampled on it or how the fit is made: the EyeLink's Host PC
and the TRACKPixx3 walk (viewpixx.py) still decide all of that, and call in
here only to draw.

What lives here, so the two backends draw a target one way:

- pure decisions, testable with nothing installed: the pulse's size at a
  given time (:func:`pulse_scale`), the drawn size in px, whether the largest
  target fits the screen at the outermost calibration point
  (:func:`check_fits`), and the order pictures are dealt in
  (:class:`PictureDeck`);
- the per-tracker state (:class:`CalibrationTargets`): the decoded pictures,
  loaded and checked once before any calibration, and the record of every
  target shown, which the calibration's result carries;
- the drawing (:class:`TargetPresenter`), handed PsychoPy's ``visual`` module
  and the window by the backend that owns them, as the rest of the
  calibration drawing is.

**Timing.** The pulse is a function of the time since the target appeared,
read from the session clock on every draw — never a count of frames or a
scale nudged each frame — so the same rate holds at any refresh rate, and a
late frame shows the size that belongs to its moment rather than pushing the
cycle back.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from alhazen.config.calibration_images import catalog, image_names, verified_bytes
from alhazen.config.models import CalibrationPulseConfig, CalibrationTargetConfig
from alhazen.core.rng import resolve_seed
from alhazen.devices.eyetracker.protocol import TargetShown
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError, TrackerError

log = logging.getLogger(__name__)

# The standard target: a disc 24 px across with a hole a third of that, so
# the subject has an unambiguous point to look at rather than a blob's
# centre. The size both backends have always drawn it at.
STANDARD_TARGET_DIAMETER_PX = 24.0
STANDARD_HOLE_FRACTION = 1.0 / 3.0
TARGET_FOREGROUND = (-1.0, -1.0, -1.0)


def pulse_scale(elapsed_s: float, pulse: CalibrationPulseConfig) -> float:
    """The target's size, as a multiple of its still size, ``elapsed_s``
    after it appeared.

    A raised cosine: ``min_scale`` at onset, ``max_scale`` half a cycle
    later, back to ``min_scale`` at a whole cycle, with no jump anywhere (its
    rate of change is zero at both ends). Before onset (a clock read a hair
    early) it is the onset size.
    """
    t = max(elapsed_s, 0.0)
    swing = (1.0 - math.cos(2.0 * math.pi * pulse.rate_hz * t)) / 2.0
    return pulse.min_scale + (pulse.max_scale - pulse.min_scale) * swing


def picture_size_px(
    name: str, config: CalibrationTargetConfig, screen: Screen
) -> tuple[float, float]:
    """A picture's still size in px: its longer side ``image_size_dva``, the
    other side in proportion, so the picture is never stretched."""
    image = catalog()[name]
    longer = screen.deg2px(config.image_size_dva)
    if image.width_px >= image.height_px:
        return longer, longer * image.height_px / image.width_px
    return longer * image.width_px / image.height_px, longer


def largest_extent_px(config: CalibrationTargetConfig, screen: Screen) -> float:
    """The widest the target is ever drawn, in px, on either axis: the still
    size times the pulse's ``max_scale`` (times 1 when still)."""
    still = (
        STANDARD_TARGET_DIAMETER_PX
        if config.appearance == "standard"
        else screen.deg2px(config.image_size_dva)
    )
    return still * (config.pulse.max_scale if config.motion == "pulse" else 1.0)


def check_fits(config: CalibrationTargetConfig, screen: Screen, area: float) -> None:
    """Refuse a target that would be cut off by the screen edge at the
    outermost calibration point, at its largest.

    The outermost points sit ``area`` of the way from the centre to each edge
    — on every layout, both backends' (the EyeLink's ``calibration_area_
    proportion``, viewpixx.calibration_targets) — so the room left for half a
    target is the rest of the way. Checked before a calibration can start,
    where the fix is a number in the rig file, not with a subject waiting.
    The standard still target is not checked: it is what calibration has
    always drawn, and it is drawn as it always was.
    """
    if config.is_default:
        return
    half = largest_extent_px(config, screen) / 2.0
    room_x = screen.width_px / 2.0 * (1.0 - area)
    room_y = screen.height_px / 2.0 * (1.0 - area)
    room = min(room_x, room_y)
    if half > room:
        largest_area = 1.0 - half / (min(screen.width_px, screen.height_px) / 2.0)
        raise TrackerError(
            f"the calibration target ({config.describe()}) is up to {2 * half:.0f} px across, "
            f"and with calibration_area {area:g} the outermost targets leave only "
            f"{2 * room:.0f} px to the screen edge: it would be cut off there. Make the "
            f"target smaller (image_size_dva, pulse.max_scale) or set calibration_area to "
            f"at most {max(largest_area, 0.0):.2f}."
        )


class PictureDeck:
    """Which picture each target shows, in turn.

    ``listed``: the names in their order, round and round — a chosen set.
    ``shuffled``: a deck of the names shuffled by ``rng`` and dealt without
    replacement, shuffled again when it runs out; when a new deck would start
    with the picture just shown, that card is swapped to the bottom, so no
    picture shows twice in a row unless the deck holds one. One draw of the
    generator per deck, so the sequence is fixed by the generator's seed and
    how many pictures were dealt.
    """

    def __init__(
        self,
        names: Sequence[str],
        order: str,
        rng: np.random.Generator | None = None,
    ) -> None:
        if not names:
            raise ConfigError("a calibration picture deck needs at least one picture")
        if order not in ("listed", "shuffled"):
            raise ValueError(f"unknown picture order {order!r}")
        if order == "shuffled" and rng is None:
            raise ValueError("a shuffled picture deck needs its random generator")
        self._names = tuple(names)
        self._order = order
        self._rng = rng
        self._deck: list[str] = []
        self._last: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def deal(self) -> str:
        if not self._deck:
            self._deck = self._new_deck()
        name = self._deck.pop(0)
        self._last = name
        return name

    def _new_deck(self) -> list[str]:
        if self._order == "listed":
            return list(self._names)
        assert self._rng is not None  # __init__ checked
        deck = [self._names[i] for i in self._rng.permutation(len(self._names))]
        if len(deck) > 1 and deck[0] == self._last:
            deck.append(deck.pop(0))
        return deck


def decode_picture(name: str) -> Any:
    """A shipped picture as a PIL image, checked: its bytes against the
    manifest, its mode (RGBA — the transparency is the picture's outline) and
    its pixel size against what the manifest records. Every failure is a
    TrackerError naming the picture, raised before a calibration starts."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as e:
        raise TrackerError(
            "calibration pictures need Pillow, which PsychoPy installs; this Python has "
            "neither. Install alhazen-vision[psychopy], or use `appearance: standard`."
        ) from e
    try:
        data = verified_bytes(name)
    except ConfigError as e:
        raise TrackerError(str(e)) from e
    expected = catalog()[name]
    try:
        with Image.open(io.BytesIO(data), formats=["PNG"]) as opened:
            opened.load()
            picture = opened.copy()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise TrackerError(f"calibration picture {name!r} cannot be decoded: {e}") from e
    if picture.mode != "RGBA":
        raise TrackerError(
            f"calibration picture {name!r} is {picture.mode}, not RGBA: without its "
            "transparency it would be drawn as a square"
        )
    if picture.size != (expected.width_px, expected.height_px):
        raise TrackerError(
            f"calibration picture {name!r} is {picture.size[0]}x{picture.size[1]} px, not the "
            f"{expected.width_px}x{expected.height_px} px its manifest records"
        )
    return picture


class CalibrationTargets:
    """One tracker's calibration targets, for the whole session.

    Holds the decoded pictures (loaded once, by :meth:`prepare`, before any
    calibration and outside any frame), the picture deck — which carries on
    from one calibration to the next, so a recalibration does not restart
    the sequence — and the record of every target shown.

    ``rng`` is the session's ``calibration_target`` stream (core/rng.py),
    used only for ``random_images``; the session hands it over with
    :meth:`use_rng` once its seed is resolved. A tracker built outside a
    session (a test, check-rig) gets none, and a random deck then draws a seed
    of its own at the first deal, logged and kept in :attr:`seed_note`.
    """

    def __init__(
        self, config: CalibrationTargetConfig, rng: np.random.Generator | None = None
    ) -> None:
        self.config = config
        self.seed_note = "session seed, stream calibration_target"
        self._rng = rng
        self._deck: PictureDeck | None = None
        if config.appearance == "images":
            self._pool: tuple[str, ...] = tuple(config.images)
        elif config.appearance == "random_images":
            self._pool = tuple(config.images) or image_names()
        else:
            self._pool = ()
        self._pictures: dict[str, Any] = {}
        self._shown: list[TargetShown] = []
        self._procedure_start = 0

    def use_rng(self, rng: np.random.Generator) -> None:
        """Deal a random order from ``rng`` (the session's stream), which a
        session hands over once it has resolved its seed. Refused once a
        picture has been dealt: the order is then already someone else's."""
        if self._deck is not None:
            raise RuntimeError("the calibration pictures have already been dealt")
        self._rng = rng

    def _dealer(self) -> PictureDeck | None:
        """The deck, made at the first deal: a random one from the session
        stream, or — for a tracker built outside a session (a test,
        check-rig) — from a seed of its own, logged and named in
        :attr:`seed_note`."""
        if self._deck is None and self._pool:
            if self.config.appearance == "images":
                self._deck = PictureDeck(self._pool, "listed")
            else:
                if self._rng is None:
                    seed = resolve_seed(None)
                    self._rng = np.random.default_rng(seed)
                    self.seed_note = f"own seed {seed} (no session seed was given)"
                    log.info("calibration pictures: no session stream; dealing with seed %d", seed)
                self._deck = PictureDeck(self._pool, "shuffled", self._rng)
        return self._deck

    @property
    def pool(self) -> tuple[str, ...]:
        """The pictures this target can show; empty for the standard one."""
        return self._pool

    def prepare(self, screen: Screen, area: float) -> None:
        """Check the target fits the screen and decode every picture it may
        show, raising TrackerError for the first that cannot be. Called when
        the tracker is configured; idempotent."""
        check_fits(self.config, screen, area)
        for name in self.pool:
            if name not in self._pictures:
                self._pictures[name] = decode_picture(name)

    def picture(self, name: str) -> Any:
        if name not in self._pictures:
            raise TrackerError(
                f"calibration picture {name!r} was not loaded before the calibration; the "
                "tracker's configure() prepares them"
            )
        return self._pictures[name]

    def next_picture(self) -> str | None:
        deck = self._dealer()
        return deck.deal() if deck is not None else None

    def record(self, target_px: tuple[float, float], image: str | None, t: float) -> TargetShown:
        shown = TargetShown(
            len(self._shown) + 1, (float(target_px[0]), float(target_px[1])), image, t
        )
        self._shown.append(shown)
        return shown

    def begin_procedure(self) -> None:
        """Mark where a calibration starts, so its result carries only its
        own targets (:meth:`shown_since_begin`)."""
        self._procedure_start = len(self._shown)

    def shown_since_begin(self) -> tuple[TargetShown, ...]:
        return tuple(self._shown[self._procedure_start :])

    @property
    def style(self) -> str:
        """The target in words, with where a random order came from."""
        text = self.config.describe()
        if self.config.appearance == "random_images":
            text += f"; order from the {self.seed_note}"
        return text

    def presenter(
        self, visual: Any, window: Any, screen: Screen, now: Callable[[], float]
    ) -> TargetPresenter:
        return TargetPresenter(self, visual, window, screen, now)


class TargetPresenter:
    """Draws the calibration target into one window, for one procedure.

    ``show(pos)`` puts up a new target — the next picture, if it shows
    pictures — and starts its pulse; ``draw()`` draws it at the size the
    session clock says (no flip: the caller flips, as it always has);
    ``hide()`` takes it down. Showing the same position again while it is up
    keeps the target, picture and pulse as they are: one target, one picture.

    Every stimulus is made here, when the procedure starts — each picture's
    texture uploaded once — so a frame only moves and resizes what exists.
    """

    def __init__(
        self,
        targets: CalibrationTargets,
        visual: Any,
        window: Any,
        screen: Screen,
        now: Callable[[], float],
    ) -> None:
        self._targets = targets
        self._config = targets.config
        self._now = now
        self._window = window
        self._pos: tuple[float, float] | None = None
        self._image: str | None = None
        self._onset = 0.0
        self._outer: Any = None
        self._inner: Any = None
        self._stims: dict[str, Any] = {}
        self._sizes: dict[str, tuple[float, float]] = {}
        if self._config.appearance == "standard":
            radius = STANDARD_TARGET_DIAMETER_PX / 2.0
            self._outer = visual.Circle(
                window,
                radius=radius,
                units="pix",
                fillColor=TARGET_FOREGROUND,
                lineColor=TARGET_FOREGROUND,
            )
            self._inner = visual.Circle(
                window,
                radius=radius * STANDARD_HOLE_FRACTION,
                units="pix",
                fillColor=window.color,
                lineColor=window.color,
            )
        else:
            for name in targets.pool:
                size = picture_size_px(name, self._config, screen)
                self._sizes[name] = size
                self._stims[name] = visual.ImageStim(
                    window,
                    image=targets.picture(name),
                    units="pix",
                    size=size,
                    # Smooth resampling for a picture being resized every
                    # frame; the camera image (calibration.py) is the one
                    # thing drawn with nearest-neighbour.
                    interpolate=True,
                )

    @property
    def showing(self) -> bool:
        return self._pos is not None

    @property
    def animated(self) -> bool:
        """Whether the target changes between frames (it must be redrawn)."""
        return self._config.motion == "pulse" and self._pos is not None

    @property
    def image(self) -> str | None:
        return self._image

    def show(self, pos: tuple[float, float]) -> None:
        if self._pos is not None and tuple(pos) == self._pos:
            return
        self._pos = (float(pos[0]), float(pos[1]))
        self._image = self._targets.next_picture()
        self._onset = self._now()
        self._targets.record(self._pos, self._image, self._onset)

    def hide(self) -> None:
        self._pos = None
        self._image = None

    def scale(self) -> float:
        if self._config.motion != "pulse":
            return 1.0
        return pulse_scale(self._now() - self._onset, self._config.pulse)

    def draw(self) -> None:
        if self._pos is None:
            return
        if self._image is None:
            self._outer.pos = self._inner.pos = self._pos
            if self._config.motion == "pulse":
                radius = STANDARD_TARGET_DIAMETER_PX / 2.0 * self.scale()
                self._outer.radius = radius
                self._inner.radius = radius * STANDARD_HOLE_FRACTION
            self._outer.draw()
            self._inner.draw()
            return
        stim = self._stims[self._image]
        width, height = self._sizes[self._image]
        scale = self.scale()
        stim.pos = self._pos
        stim.size = (width * scale, height * scale)
        stim.draw()


__all__ = [
    "STANDARD_TARGET_DIAMETER_PX",
    "CalibrationTargets",
    "PictureDeck",
    "TargetPresenter",
    "check_fits",
    "decode_picture",
    "largest_extent_px",
    "picture_size_px",
    "pulse_scale",
]
