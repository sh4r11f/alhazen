"""Drawing a scene into an array of pixels.

The primary implementation is headless: ``headless_render`` produces an RGB
array with no display, no renderer and no window. That is deliberate — it
means a scene's appearance is testable in the default suite on a machine with
nothing installed, and it means the display path is a blit rather than a
second implementation that could drift from the first.

How the drawing works. Shapes are rasterised by *coverage*: for each pixel,
how much of it the shape covers, computed on a supersampled grid. Coverage
then mixes the shape's colour into what is already there. That gives
anti-aliased edges from one mechanism rather than per-primitive special cases,
and it composites correctly with layer opacity.

Coordinates. A scene is written in logical pixels with y growing **down** from
the top-left, like a canvas. alhazen's screen is centered px with y growing
**up**. The conversion happens once, where the rendered array is placed on
screen (``SceneStimulus``), and never inside the drawing code — which is why
everything below can be read against the scene format directly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from alhazen.errors import ConfigError
from alhazen.scenes.expr import EvalContext, evaluate_expr, js_round
from alhazen.scenes.model import Layer, Scene
from alhazen.scenes.rng import mulberry32, to_uint32

log = logging.getLogger(__name__)

# Samples per pixel per axis when measuring coverage. Four means sixteen
# samples a pixel: enough that an edge steps in sixteenths rather than
# jumping, cheap enough to render a full frame per trial.
SUPERSAMPLE = 4

# What a field means when the scene leaves it out. These are the format's own
# defaults, kept here so a missing field draws what the studio would draw.
DEFAULT_FILL = "#ffffff"
DEFAULT_BACKGROUND = "#000000"


@dataclass
class RenderContext:
    """Everything a frame needs: the canvas, the clock, and the params."""

    width: int
    height: int
    time: float = 0.0
    dt: float = 0.0
    dpr: float = 1.0
    params: dict[str, Any] | None = None

    def eval_context(self, extra: dict[str, float] | None = None) -> EvalContext:
        return EvalContext(
            time=self.time,
            dt=self.dt,
            width=float(self.width),
            height=float(self.height),
            dpr=self.dpr,
            params=self.params or {},
            vars=extra,
        )


# ---------------------------------------------------------------------------
# Values and colours
# ---------------------------------------------------------------------------


def value(field: Any, context: RenderContext, default: float = 0.0) -> float:
    """A numeric field: a literal, an expression, or absent."""
    if field is None:
        return default
    if isinstance(field, dict):
        source = field.get("expr")
        if source is None:
            raise ConfigError(f"a field object must carry 'expr', got {sorted(field)}")
        result = evaluate_expr(source, context.eval_context())
        return float(result)
    return float(field)


def string_value(field: Any, context: RenderContext, default: str = "") -> str:
    """A string field, which may also be an expression (a computed colour)."""
    if field is None:
        return default
    if isinstance(field, dict):
        source = field.get("expr")
        if source is None:
            raise ConfigError(f"a field object must carry 'expr', got {sorted(field)}")
        return str(evaluate_expr(source, context.eval_context()))
    return str(field)


def parse_color(text: str) -> tuple[np.ndarray, float]:
    """A CSS colour string as (rgb 0-255, alpha 0-1).

    Handles the forms scenes actually use: ``#rgb``, ``#rrggbb``, and
    ``rgba(r, g, b, a)`` — which is what the language's own ``withAlpha``
    produces. An unrecognised colour is an error rather than a default,
    because a stimulus drawn in the wrong colour is worse than one that
    refused to draw.
    """
    text = text.strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(character * 2 for character in digits)
        if len(digits) != 6:
            raise ConfigError(f"unrecognised colour {text!r}")
        return (
            np.array([int(digits[i : i + 2], 16) for i in (0, 2, 4)], dtype=float),
            1.0,
        )
    if text.startswith("rgba(") or text.startswith("rgb("):
        inner = text[text.index("(") + 1 : text.rindex(")")]
        parts = [part.strip() for part in inner.split(",")]
        if len(parts) not in (3, 4):
            raise ConfigError(f"unrecognised colour {text!r}")
        rgb = np.array([float(part) for part in parts[:3]], dtype=float)
        alpha = float(parts[3]) if len(parts) == 4 else 1.0
        return rgb, alpha
    raise ConfigError(f"unrecognised colour {text!r}: scenes use '#rgb', '#rrggbb' or 'rgba(...)'")


# ---------------------------------------------------------------------------
# The canvas
# ---------------------------------------------------------------------------


class Canvas:
    """An RGB float image, and the one place colour is mixed into it.

    A canvas may also track **coverage**: how much of each pixel has been
    painted at all, 0 to 1. The visible canvas does not need it — it starts
    opaque with the scene's background. A scratch canvas for a transformed
    layer does, because "was anything drawn here" cannot be answered from the
    colours: black content on a black scratch is indistinguishable from
    nothing, and an anti-aliased edge is a fraction of the fill mixed with
    whatever the scratch started as.
    """

    def __init__(
        self,
        width: int,
        height: int,
        background: tuple[np.ndarray, float],
        track_coverage: bool = False,
        origin: tuple[int, int] = (0, 0),
    ) -> None:
        self.width = width
        self.height = height
        # Where this canvas's pixel (0, 0) sits in scene coordinates. Zero for
        # the visible canvas; a transformed layer's scratch needs a different
        # one, because its content is drawn in the layer's own coordinates and
        # those routinely run negative (a group whose children are placed
        # around the origin, then translated into view).
        self.origin_x, self.origin_y = origin
        rgb, _alpha = background
        # Float rather than uint8 while drawing: repeated compositing on
        # 8-bit values loses a little each time, and a grating's mid-greys
        # are exactly where that shows.
        self.pixels = np.tile(rgb.astype(float), (height, width, 1))
        self.coverage: np.ndarray | None = (
            np.zeros((height, width), dtype=float) if track_coverage else None
        )
        # Pixel-centre coordinates, in scene space (y down). Every primitive
        # asks its shape questions against these, so none of them needs to
        # know how the grid was built.
        self.xs = np.arange(width, dtype=float) + 0.5 + self.origin_x
        self.ys = np.arange(height, dtype=float) + 0.5 + self.origin_y

    def blend(self, coverage: np.ndarray, rgb: np.ndarray, alpha: float = 1.0) -> None:
        """Mix a colour in, weighted by how much of each pixel it covers."""
        if alpha <= 0:
            return
        weight = np.clip(coverage, 0.0, 1.0) * alpha
        self.pixels = (
            self.pixels * (1.0 - weight[..., None]) + rgb[None, None, :] * weight[..., None]
        )
        self.note_coverage(weight)

    def note_coverage(self, weight: np.ndarray) -> None:
        """Accumulate source-over alpha for a paint of the given weight.

        A no-op on a canvas that does not track it, so every primitive can
        call it without knowing which kind of canvas it was handed.
        """
        if self.coverage is None:
            return
        self.coverage = self.coverage + weight * (1.0 - self.coverage)

    def unpremultiplied(self) -> np.ndarray:
        """The colours as they would be over an opaque backing.

        A scratch canvas starts at zero, so a pixel painted at coverage c
        holds ``c × colour``. Compositing that onto a real background needs
        the colour back, not the premultiplied product — which is exactly the
        dark fringe an anti-aliased edge would otherwise carry.
        """
        if self.coverage is None:
            return self.pixels
        safe = np.where(self.coverage > 0.0, self.coverage, 1.0)[..., None]
        return self.pixels / safe

    def to_uint8(self) -> np.ndarray:
        return np.clip(np.rint(self.pixels), 0, 255).astype(np.uint8)


def _sample_grid(canvas: Canvas) -> tuple[np.ndarray, np.ndarray]:
    """Supersampled coordinates: (n, height*S, width*S) grids of x and y.

    Coverage is measured by asking the shape's own inside-test at each
    subsample and averaging — one mechanism for every primitive, rather than
    per-shape edge maths that would each need their own anti-aliasing.
    """
    step = 1.0 / SUPERSAMPLE
    offsets = (np.arange(SUPERSAMPLE) + 0.5) * step
    xs = (np.arange(canvas.width)[:, None] + canvas.origin_x + offsets[None, :]).ravel()
    ys = (np.arange(canvas.height)[:, None] + canvas.origin_y + offsets[None, :]).ravel()
    grid_x, grid_y = np.meshgrid(xs, ys)
    return grid_x, grid_y


def _reduce_coverage(mask: np.ndarray, canvas: Canvas) -> np.ndarray:
    """Average a supersampled boolean mask back down to per-pixel coverage."""
    reshaped = mask.reshape(canvas.height, SUPERSAMPLE, canvas.width, SUPERSAMPLE)
    return reshaped.mean(axis=(1, 3))


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _draw_rect(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    x = value(element.get("x"), context)
    y = value(element.get("y"), context)
    width = value(element.get("width"), context)
    height = value(element.get("height"), context)
    radius = value(element.get("cornerRadius"), context)

    grid_x, grid_y = _sample_grid(canvas)
    # An exact signed distance for the (rounded) box: negative inside, zero on
    # the edge, positive outside. Fill and stroke both fall out of it, and the
    # stroke lands CENTRED on the edge — half in, half out, as a canvas draws
    # it. A rounded rect previously fell through to an inward erosion, putting
    # its whole outline inside the shape.
    signed = _rounded_box_distance(grid_x, grid_y, x, y, width, height, radius)
    _fill_and_stroke(canvas, element, context, opacity, signed <= 0.0, grid_x, grid_y, signed)


def _rounded_box_distance(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    x: float,
    y: float,
    width: float,
    height: float,
    radius: float,
) -> np.ndarray:
    """Signed distance to a box with rounded corners.

    The standard rounded-box field: shrink the half-extents by the corner
    radius, take the distance to that inner box, then subtract the radius. For
    ``radius == 0`` it is the plain box distance — whose half-width offset has
    square outer corners, i.e. exactly the miter join a canvas puts on a
    stroked rectangle.
    """
    half_w, half_h = abs(width) / 2.0, abs(height) / 2.0
    radius = min(max(radius, 0.0), min(half_w, half_h))
    dx = np.abs(grid_x - (x + width / 2.0)) - (half_w - radius)
    dy = np.abs(grid_y - (y + height / 2.0)) - (half_h - radius)
    outside = np.hypot(np.maximum(dx, 0.0), np.maximum(dy, 0.0))
    inside = np.minimum(np.maximum(dx, dy), 0.0)
    return outside + inside - radius


def _draw_circle(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    cx = value(element.get("cx"), context)
    cy = value(element.get("cy"), context)
    radius = value(element.get("radius"), context)
    grid_x, grid_y = _sample_grid(canvas)
    distance = np.hypot(grid_x - cx, grid_y - cy)
    _fill_and_stroke(
        canvas, element, context, opacity, distance <= radius, grid_x, grid_y, distance - radius
    )


def _draw_ellipse(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    cx = value(element.get("cx"), context)
    cy = value(element.get("cy"), context)
    rx = max(value(element.get("radiusX"), context), 1e-9)
    ry = max(value(element.get("radiusY"), context), 1e-9)
    rotation = value(element.get("rotation"), context)
    grid_x, grid_y = _sample_grid(canvas)
    # Rotate the sample into the ellipse's own frame, then it is a circle
    # test in scaled coordinates.
    dx, dy = grid_x - cx, grid_y - cy
    cos_r, sin_r = math.cos(-rotation), math.sin(-rotation)
    local_x = dx * cos_r - dy * sin_r
    local_y = dx * sin_r + dy * cos_r
    normalized = np.hypot(local_x / rx, local_y / ry)
    # One Newton step on the implicit function turns "how many radii out" into
    # a distance in pixels: d ≈ (q − 1)·q / |∇q|. Exact ellipse distance needs
    # iteration; this is accurate to well under a pixel near the rim, which is
    # where a stroke lives — and it means the stroke is CENTRED on the edge
    # rather than eroded inward, which is what a canvas draws.
    gradient = np.hypot(local_x / (rx * rx), local_y / (ry * ry))
    with np.errstate(divide="ignore", invalid="ignore"):
        signed = np.where(gradient > 0.0, (normalized - 1.0) * normalized / gradient, -min(rx, ry))
    _fill_and_stroke(canvas, element, context, opacity, normalized <= 1.0, grid_x, grid_y, signed)


def _draw_polygon(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    points = _points(element, context)
    if len(points) < 3:
        raise ConfigError("a polygon needs at least three points")
    grid_x, grid_y = _sample_grid(canvas)
    inside = _point_in_polygon(grid_x, grid_y, points)
    _fill_and_stroke(canvas, element, context, opacity, inside, grid_x, grid_y, outline=points)


def _draw_line(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    points = _points(element, context)
    if len(points) < 2:
        raise ConfigError("a line needs at least two points")
    stroke = string_value(element.get("stroke"), context, DEFAULT_FILL)
    thickness = max(value(element.get("strokeWidth"), context, 1.0), 0.1)
    closed = bool(element.get("closed"))
    dash = [float(value(entry, context)) for entry in (element.get("dash") or [])]

    grid_x, grid_y = _sample_grid(canvas)
    covered = _stroke_polyline(
        grid_x,
        grid_y,
        points,
        thickness,
        closed=closed,
        cap=str(element.get("cap") or "butt"),
        join=str(element.get("join") or "miter"),
        dash=dash,
    )
    rgb, alpha = parse_color(stroke)
    canvas.blend(_reduce_coverage(covered, canvas), rgb, alpha * opacity)


def _draw_stripes(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    x = value(element.get("x"), context)
    y = value(element.get("y"), context)
    width = value(element.get("width"), context)
    height = value(element.get("height"), context)
    # The studio's own defaults: a one-pixel bar every pixel. Defaulting to a
    # 20 px period with half-period bars drew a completely different stimulus
    # for any scene that left them out.
    period = max(value(element.get("period"), context, 1.0), 1.0)
    thickness = max(value(element.get("thickness"), context, 1.0), 0.0)
    offset = value(element.get("offset"), context)
    orientation = str(element.get("orientation") or "vertical")
    color = string_value(element.get("color"), context, DEFAULT_FILL)

    grid_x, grid_y = _sample_grid(canvas)

    # The studio steps its bars from `x - offset`, so a positive offset moves
    # the pattern LEFT (and up). Getting this sign backwards puts every bar
    # half a period from where the scene's author placed it.
    shift = math.fmod(math.fmod(offset, period) + period, period)

    def bars(axis: np.ndarray, start: float, span: float) -> np.ndarray:
        """The union of `fillRect`s the studio's loop would draw.

        Its loop is ``for (v = start - shift; v < start + span; v += period)``,
        so the bars begin *before* the box and the last one may extend past
        its far edge — a bar is never trimmed to the box in the direction it
        repeats. Clipping to the box, as this used to, silently narrowed the
        first and last bar of every stripe field.
        """
        first = start - shift
        # Which bar each sample would fall in, and where inside it.
        index = np.floor((axis - first) / period)
        within = (axis - first) - index * period
        # `index >= 0` is the loop starting at `first`; the upper bound is the
        # last iteration whose own start is still below `start + span`.
        last = math.ceil((span + shift) / period) - 1
        return (index >= 0) & (index <= last) & (within < thickness)

    striped = np.zeros_like(grid_x, dtype=bool)
    if orientation in ("vertical", "both"):
        # Vertical bars span the box's full height and repeat along x.
        striped |= bars(grid_x, x, width) & (grid_y >= y) & (grid_y <= y + height)
    if orientation in ("horizontal", "both"):
        striped |= bars(grid_y, y, height) & (grid_x >= x) & (grid_x <= x + width)
    rgb, alpha = parse_color(color)
    canvas.blend(_reduce_coverage(striped, canvas), rgb, alpha * opacity)


def _draw_grating(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    """A sinusoidal or square grating, optionally under a Gaussian envelope.

    Computed per pixel rather than by coverage: a grating has no edges to
    anti-alias, it has a luminance at every point. With an envelope this is a
    Gabor, which is why the format spells "Gabor" as "grating with
    envelopeSigma".
    """
    cx = value(element.get("cx"), context)
    cy = value(element.get("cy"), context)
    width = value(element.get("width"), context)
    height = value(element.get("height"), context)
    spatial_freq = value(element.get("spatialFreq"), context, 0.05)  # cycles per px
    phase = value(element.get("phase"), context)  # 0..1 of a cycle
    orientation = value(element.get("orientation"), context)  # radians
    # Both clamped, as the studio clamps them: a scene driving contrast from an
    # expression that overshoots must not produce a patch brighter than full.
    contrast = min(max(value(element.get("contrast"), context, 1.0), 0.0), 1.0)
    base = min(max(value(element.get("baseLuminance"), context, 128.0), 0.0), 255.0)
    envelope_sigma = value(element.get("envelopeSigma"), context)
    shape = str(element.get("shape") or "sine")

    # Built as a patch buffer of its own and then placed, exactly as the
    # studio does: the wave is sampled at INTEGER offsets from the patch's
    # centre, not at pixel centres. Half a pixel of difference here is a
    # visible phase shift at a high spatial frequency, and matching the
    # studio is the whole point of rendering its scenes.
    columns = max(int(js_round(width)), 1)
    rows = max(int(js_round(height)), 1)
    half_w, half_h = columns / 2.0, rows / 2.0
    local_x = np.arange(columns, dtype=float) - half_w
    local_y = np.arange(rows, dtype=float) - half_h
    grid_x, grid_y = np.meshgrid(local_x, local_y)

    # Distance along the grating's own axis: rotating the coordinate rather
    # than the image keeps this exact at any orientation.
    projected = grid_x * math.cos(orientation) + grid_y * math.sin(orientation)
    cycles = projected * spatial_freq + phase
    wave = np.sin(2.0 * math.pi * cycles)
    if shape == "square":
        # Thresholded on the SINE, not on the cycle fraction. They differ
        # exactly at the half-cycle, where sin is 0 and the studio takes +1.
        wave = np.where(wave >= 0.0, 1.0, -1.0)

    # 127, not the base luminance. The studio's amplitude is a fixed fraction
    # of the 8-bit range, so contrast means the same thing at every base;
    # scaling by `base` instead made a dim patch (baseLuminance 64) carry half
    # the modulation it asked for — precisely the region the old goldens never
    # sampled.
    amplitude = 127.0 * contrast
    if envelope_sigma > 0:
        # The Gaussian window that turns a grating into a Gabor. It scales
        # the CONTRAST, not the luminance, so the patch fades into the
        # background's grey rather than toward black.
        wave = wave * np.exp(-(grid_x**2 + grid_y**2) / (2.0 * envelope_sigma**2))
    patch = np.clip(base + amplitude * wave, 0, 255)

    # Placed with its centre at (cx, cy), clipped to the canvas. Math.round,
    # not Python's: the studio rounds halves up, and a patch of even size
    # centred on a pixel centre lands exactly on .5.
    x0 = int(js_round(cx - half_w)) - canvas.origin_x
    y0 = int(js_round(cy - half_h)) - canvas.origin_y
    x1, y1 = min(x0 + columns, canvas.width), min(y0 + rows, canvas.height)
    if x1 <= max(x0, 0) or y1 <= max(y0, 0):
        return
    source = patch[max(0, -y0) : y1 - y0, max(0, -x0) : x1 - x0]
    target = (slice(max(y0, 0), y1), slice(max(x0, 0), x1))
    grey = np.stack([source] * 3, axis=-1)
    canvas.pixels[target] = canvas.pixels[target] * (1.0 - opacity) + grey * opacity
    _note_block_coverage(canvas, target, opacity)


# Knuth's multiplicative hash constant and the studio's cycle salt: the two
# numbers that turn (seed, dot index, lifetime cycle) into one dot's stream.
_DOT_INDEX_SALT = 2654435761
_DOT_CYCLE_SALT = 40503


def dot_field_positions(element: dict, context: RenderContext) -> list[tuple[float, float] | None]:
    """Where every dot in a dotField is at this instant, in scene pixels.

    A direct port of illusion-studio's ``drawDotField`` (procedural.ts), and
    the load-bearing element for a random-dot experiment — so it is a function
    of its own, pinned against a fixture generated from the studio's maths.

    Three rules, each of which alhazen previously got wrong:

    - **A stream per dot, not one stream shared.** Each dot seeds its own
      generator from ``(seed ^ (i * 2654435761) ^ (cycle * 40503)) >>> 0``.
      Drawing every dot from one sequence gives a different field entirely.
    - **The signal set is the first ``round(count × coherence)`` dots**, by
      index. Rolling a per-dot coin against coherence gives the right
      *expected* coherence and the wrong field: the actual number of signal
      dots varies from frame to frame, which is exactly what a coherence
      threshold measures.
    - **Lifetime and wrap are real.** A dot with a lifetime is reborn each
      cycle at a position its re-seeded stream chooses, and its age restarts;
      without wrap, a dot that leaves the aperture is simply not drawn.

    Returns one entry per dot, ``None`` for a dot that has left an unwrapped
    aperture.
    """
    cx = value(element.get("cx"), context)
    cy = value(element.get("cy"), context)
    width = max(1.0, value(element.get("width"), context, 1.0))
    height = max(1.0, value(element.get("height"), context, 1.0))
    count = max(0, int(math.floor(value(element.get("count"), context, 100.0))))
    coherence = min(max(value(element.get("coherence"), context, 1.0), 0.0), 1.0)
    # 60 px/s, the studio's default. Defaulting to 0 made every dot field in a
    # scene that omits `speed` a still image.
    speed = value(element.get("speed"), context, 60.0)
    direction = value(element.get("direction"), context, 0.0)
    lifetime = max(0.0, value(element.get("lifetime"), context, 0.0))
    wrap = element.get("wrap")
    wrap = True if wrap is None else bool(wrap)
    seed = int(math.floor(value(element.get("seed"), context, 1.0)))
    time = context.time

    left, top = cx - width / 2.0, cy - height / 2.0
    coherent_count = int(js_round(count * coherence))

    positions: list[tuple[float, float] | None] = []
    for index in range(count):
        index_seed = to_uint32(seed) ^ to_uint32(index * _DOT_INDEX_SALT)
        # The phase staggers the dots' birthdays so they do not all respawn on
        # the same frame; it is drawn from a stream that does NOT include the
        # cycle, so a dot's phase is stable across its whole life.
        phase = mulberry32(index_seed)() if lifetime > 0 else 0.0
        cycle = math.floor((time + phase * lifetime) / lifetime) if lifetime > 0 else 0
        generator = mulberry32(index_seed ^ to_uint32(cycle * _DOT_CYCLE_SALT))
        start_x = generator() * width
        start_y = generator() * height
        heading = direction if index < coherent_count else generator() * 2.0 * math.pi
        age = math.fmod(time + phase * lifetime, lifetime) if lifetime > 0 else time

        x = start_x + math.cos(heading) * speed * age
        y = start_y + math.sin(heading) * speed * age
        if wrap:
            x = math.fmod(math.fmod(x, width) + width, width)
            y = math.fmod(math.fmod(y, height) + height, height)
        elif x < 0 or x > width or y < 0 or y > height:
            positions.append(None)
            continue
        positions.append((left + x, top + y))
    return positions


def _draw_dot_field(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    """A field of moving dots, positioned by a pure function of (seed, index, time).

    No incremental state: every dot's position is computed from its index and
    the current time, so seeking to any moment gives the same picture as
    playing there — which is what makes a dot field reproducible in an
    experiment rather than merely repeatable.
    """
    dot_radius = max(0.5, value(element.get("dotRadius"), context, 2.0))
    color = string_value(element.get("color"), context, DEFAULT_FILL)

    grid_x, grid_y = _sample_grid(canvas)
    covered = np.zeros_like(grid_x, dtype=bool)
    for position in dot_field_positions(element, context):
        if position is None:
            continue
        x, y = position
        covered |= np.hypot(grid_x - x, grid_y - y) <= dot_radius
    # NOT clipped to the aperture: the studio fills one path of circles and a
    # dot whose centre sits just inside the edge spills its radius over it.
    # Clipping here would shave every edge dot into a crescent the studio
    # never draws.
    rgb, alpha = parse_color(color)
    canvas.blend(_reduce_coverage(covered, canvas), rgb, alpha * opacity)


def _draw_noise(canvas: Canvas, element: dict, context: RenderContext, opacity: float) -> None:
    """A rectangle of static, generated once per parameter set from its seed."""
    x = value(element.get("x"), context)
    y = value(element.get("y"), context)
    width = value(element.get("width"), context)
    height = value(element.get("height"), context)
    mean = value(element.get("mean"), context, 128.0)
    sigma = max(0.0, value(element.get("sigma"), context, 40.0))
    seed = int(value(element.get("seed"), context, 1))
    distribution = str(element.get("distribution") or "uniform")

    columns = max(int(js_round(width)), 1)
    rows = max(int(js_round(height)), 1)
    generator = mulberry32(seed)
    # Filled in row-major order from the scene's own generator, so the grain
    # is identical to the studio's rather than merely similar.
    values = np.empty(rows * columns, dtype=float)
    if distribution == "gaussian":
        for position in range(rows * columns):
            # Box-Muller from two uniforms, which is what the studio uses:
            # a different transform would give a different (equally
            # Gaussian, but visibly other) pattern.
            u1 = max(generator(), 1e-12)
            u2 = generator()
            values[position] = mean + sigma * math.sqrt(-2.0 * math.log(u1)) * math.cos(
                2.0 * math.pi * u2
            )
    else:
        for position in range(rows * columns):
            values[position] = mean + sigma * (generator() * 2.0 - 1.0)
    patch = np.clip(values.reshape(rows, columns), 0, 255)

    x0 = int(js_round(x)) - canvas.origin_x
    y0 = int(js_round(y)) - canvas.origin_y
    x1, y1 = min(x0 + columns, canvas.width), min(y0 + rows, canvas.height)
    if x1 <= max(x0, 0) or y1 <= max(y0, 0):
        return
    source = patch[max(0, -y0) : y1 - y0, max(0, -x0) : x1 - x0]
    target = (slice(max(y0, 0), y1), slice(max(x0, 0), x1))
    grey = np.stack([source] * 3, axis=-1)
    canvas.pixels[target] = canvas.pixels[target] * (1.0 - opacity) + grey * opacity
    _note_block_coverage(canvas, target, opacity)


def _note_block_coverage(canvas: Canvas, target: Any, opacity: float) -> None:
    """Record a direct pixel write on a coverage-tracking canvas.

    Gratings and noise write their patch straight into the array rather than
    blending a coverage mask, so they have to say what they painted or a
    transformed grating would composite as nothing.
    """
    if canvas.coverage is None:
        return
    weight = np.zeros((canvas.height, canvas.width), dtype=float)
    weight[target] = opacity
    canvas.note_coverage(weight)


DRAWERS = {
    "rect": _draw_rect,
    "circle": _draw_circle,
    "ellipse": _draw_ellipse,
    "polygon": _draw_polygon,
    "line": _draw_line,
    "stripes": _draw_stripes,
    "grating": _draw_grating,
    "dotField": _draw_dot_field,
    "noise": _draw_noise,
}


# ---------------------------------------------------------------------------
# Shared shape helpers
# ---------------------------------------------------------------------------


def _fill_and_stroke(
    canvas: Canvas,
    element: dict,
    context: RenderContext,
    opacity: float,
    inside: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    signed_distance: np.ndarray | None = None,
    outline: list[tuple[float, float]] | None = None,
) -> None:
    """Paint a shape's interior, then its outline if it has one."""
    fill = element.get("fill")
    if fill is not None:
        rgb, alpha = parse_color(string_value(fill, context, DEFAULT_FILL))
        canvas.blend(_reduce_coverage(inside, canvas), rgb, alpha * opacity)

    stroke = element.get("stroke")
    if stroke is None:
        return
    thickness = max(value(element.get("strokeWidth"), context, 1.0), 0.1)
    if signed_distance is not None:
        # A shape that knows its own distance function strokes exactly, and
        # CENTRED on the edge — half the width inside, half outside, which
        # is what a canvas does. Stroking only inward would put a 4 px
        # outline 2 px off from where the studio draws it.
        band = np.abs(signed_distance) <= thickness / 2.0
    elif outline is not None:
        # A closed path (a polygon's own vertices) strokes exactly as a line
        # does: centred on the edge, with miter joins at the corners.
        band = _stroke_polyline(grid_x, grid_y, outline, thickness, closed=True, join="miter")
    else:
        raise ConfigError("a stroked shape must supply either a signed distance or its outline")
    rgb, alpha = parse_color(string_value(stroke, context, DEFAULT_FILL))
    canvas.blend(_reduce_coverage(band, canvas), rgb, alpha * opacity)


def _points(element: dict, context: RenderContext) -> list[tuple[float, float]]:
    raw = element.get("points") or []
    return [(value(point[0], context), value(point[1], context)) for point in raw]


def _point_in_polygon(
    grid_x: np.ndarray, grid_y: np.ndarray, points: list[tuple[float, float]]
) -> np.ndarray:
    """NONZERO winding, vectorised over the whole sample grid.

    Canvas's `fill()` defaults to nonzero, not even-odd, and the difference is
    not academic: a self-intersecting outline — a five-pointed star, any
    figure whose edges cross — has a filled centre under nonzero and a hole
    under even-odd. Getting this wrong draws a different shape, not a
    differently-antialiased one.
    """
    winding = np.zeros_like(grid_x, dtype=int)
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
        # Each edge crossing a horizontal ray from the sample counts +1 when
        # it crosses upward and -1 downward; a nonzero total means inside.
        with np.errstate(divide="ignore", invalid="ignore"):
            crossing_x = (x1 - x0) * (grid_y - y0) / (y1 - y0) + x0
        to_the_right = grid_x < crossing_x
        winding += np.where((y0 <= grid_y) & (y1 > grid_y) & to_the_right, 1, 0)
        winding -= np.where((y0 > grid_y) & (y1 <= grid_y) & to_the_right, 1, 0)
    return winding != 0


def _stroke_polyline(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    points: list[tuple[float, float]],
    thickness: float,
    closed: bool = False,
    cap: str = "butt",
    join: str = "miter",
    dash: list[float] | None = None,
) -> np.ndarray:
    """A stroked polyline as a boolean coverage mask, with canvas semantics.

    A capsule per segment — which is what this used to be — is a stroke with
    ROUND caps and ROUND joins. Canvas defaults to butt caps and miter joins,
    so every line ended half a stroke-width long and every corner came out
    blunt. The three pieces here are the segment bodies, the caps at the two
    free ends, and the wedge each interior corner leaves on its outer side.
    """
    half = thickness / 2.0
    covered = np.zeros_like(grid_x, dtype=bool)

    runs = _dash_runs(points, closed, dash or [])
    for run, run_closed in runs:
        segments = list(zip(run, run[1:], strict=False))
        if run_closed and len(run) > 2:
            segments.append((run[-1], run[0]))
        if not segments:
            continue
        for index, ((x0, y0), (x1, y1)) in enumerate(segments):
            body, direction = _segment_body(grid_x, grid_y, x0, y0, x1, y1, half)
            covered |= body
            if direction is None:
                continue
            # Caps go on the two ends the path does not continue through. A
            # closed run has none.
            if not run_closed:
                if index == 0:
                    backwards = (-direction[0], -direction[1])
                    covered |= _cap(grid_x, grid_y, (x0, y0), backwards, half, cap)
                if index == len(segments) - 1:
                    covered |= _cap(grid_x, grid_y, (x1, y1), direction, half, cap)
        covered |= _joins(grid_x, grid_y, segments, half, join, run_closed)
    return covered


def _segment_body(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    half: float,
) -> tuple[np.ndarray, tuple[float, float] | None]:
    """The rectangle of the stroke along one segment, flush at both ends."""
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        return np.zeros_like(grid_x, dtype=bool), None
    ux, uy = dx / length, dy / length
    along = (grid_x - x0) * ux + (grid_y - y0) * uy
    across = np.abs((grid_x - x0) * -uy + (grid_y - y0) * ux)
    return (along >= 0) & (along <= length) & (across <= half), (ux, uy)


def _cap(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    point: tuple[float, float],
    direction: tuple[float, float],
    half: float,
    cap: str,
) -> np.ndarray:
    """What a free end adds beyond the segment's flush edge."""
    if cap == "round":
        return np.hypot(grid_x - point[0], grid_y - point[1]) <= half
    if cap == "square":
        ux, uy = direction
        beyond = (grid_x - point[0]) * ux + (grid_y - point[1]) * uy
        across = np.abs((grid_x - point[0]) * -uy + (grid_y - point[1]) * ux)
        return (beyond >= 0) & (beyond <= half) & (across <= half)
    return np.zeros_like(grid_x, dtype=bool)  # butt: nothing beyond the end


# Canvas's own default. Past it a miter would shoot away from the corner, so
# the join falls back to a bevel.
MITER_LIMIT = 10.0


def _joins(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    half: float,
    join: str,
    closed: bool,
) -> np.ndarray:
    """Fill the wedge each corner leaves on its outer side."""
    filled = np.zeros_like(grid_x, dtype=bool)
    pairs = list(zip(segments, segments[1:], strict=False))
    if closed and len(segments) > 1:
        pairs.append((segments[-1], segments[0]))
    for (a0, a1), (_b0, b1) in pairs:
        vertex = a1
        if join == "round":
            filled |= np.hypot(grid_x - vertex[0], grid_y - vertex[1]) <= half
            continue
        incoming = _unit(a0, a1)
        outgoing = _unit(vertex, b1)
        if incoming is None or outgoing is None:
            continue
        # Which side is the outside of the turn: the side the cross product
        # points away from.
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        if abs(cross) < 1e-12:
            continue  # straight through: the bodies already meet
        sign = -1.0 if cross > 0 else 1.0
        n_in = (-incoming[1] * sign, incoming[0] * sign)
        n_out = (-outgoing[1] * sign, outgoing[0] * sign)
        corner_in = (vertex[0] + n_in[0] * half, vertex[1] + n_in[1] * half)
        corner_out = (vertex[0] + n_out[0] * half, vertex[1] + n_out[1] * half)
        wedge = [vertex, corner_in, corner_out]
        if join == "miter":
            bisector_x, bisector_y = n_in[0] + n_out[0], n_in[1] + n_out[1]
            norm = math.hypot(bisector_x, bisector_y)
            if norm > 1e-12:
                # Half-angle between the segments; the miter reaches
                # half / sin(theta/2) from the vertex.
                sin_half = norm / 2.0
                if sin_half > 1.0 / MITER_LIMIT:
                    reach = half / sin_half
                    tip = (
                        vertex[0] + bisector_x / norm * reach,
                        vertex[1] + bisector_y / norm * reach,
                    )
                    wedge = [vertex, corner_in, tip, corner_out]
        filled |= _point_in_polygon(grid_x, grid_y, wedge)
    return filled


def _unit(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else None


def _dash_runs(
    points: list[tuple[float, float]], closed: bool, dash: list[float]
) -> list[tuple[list[tuple[float, float]], bool]]:
    """Split a path into the sub-paths a dash pattern leaves drawn.

    The pattern runs along the whole path's arc length, not per segment, which
    is what makes a dash carry across a corner the way a canvas draws it. An
    empty or all-zero pattern is a solid line and returns the path untouched.
    """
    if not dash or all(entry <= 0 for entry in dash) or any(entry < 0 for entry in dash):
        return [(points, closed)]
    pattern = list(dash)
    if len(pattern) % 2:
        # Canvas repeats an odd-length pattern twice, so on and off alternate.
        pattern = pattern + pattern
    path = list(points) + ([points[0]] if closed and len(points) > 2 else [])

    runs: list[tuple[list[tuple[float, float]], bool]] = []
    current: list[tuple[float, float]] = []
    index, remaining, drawing = 0, pattern[0], True
    for start, end in zip(path, path[1:], strict=False):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        travelled = 0.0
        while travelled < length - 1e-12:
            step = min(remaining, length - travelled)
            head = _lerp(start, end, (travelled) / length)
            tail = _lerp(start, end, (travelled + step) / length)
            if drawing:
                if not current:
                    current = [head]
                current.append(tail)
            travelled += step
            remaining -= step
            if remaining <= 1e-12:
                if drawing and current:
                    runs.append((current, False))
                    current = []
                index = (index + 1) % len(pattern)
                remaining, drawing = pattern[index], not drawing
    if current:
        runs.append((current, False))
    return runs


def _lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _distance_to_segment(
    grid_x: np.ndarray, grid_y: np.ndarray, x0: float, y0: float, x1: float, y1: float
) -> np.ndarray:
    """Distance from each sample to a line segment (not its infinite line)."""
    dx, dy = x1 - x0, y1 - y0
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return np.hypot(grid_x - x0, grid_y - y0)
    t = np.clip(((grid_x - x0) * dx + (grid_y - y0) * dy) / length_squared, 0.0, 1.0)
    return np.hypot(grid_x - (x0 + t * dx), grid_y - (y0 + t * dy))


# ---------------------------------------------------------------------------
# Layers, transforms and the frame
# ---------------------------------------------------------------------------


def _layer_transform(layer: Layer) -> Any:
    """The layer's transform, whether the model declares it or carries it as
    an extra field."""
    return getattr(layer, "transform", None) or (layer.model_extra or {}).get("transform")


def _draw_layer(canvas: Canvas, layer: Layer, context: RenderContext, opacity: float) -> None:
    if layer.visible is not None and not value(layer.visible, context, 1.0):
        return
    # Canvas semantics: `ctx.globalAlpha = layer.opacity` inside a
    # save/restore REPLACES the inherited alpha rather than multiplying into
    # it, so the innermost declared opacity wins. A layer that declares none
    # inherits its parent's. Multiplying would make a 0.5 layer inside a 0.5
    # group draw at 0.25 — a quarter of what its author asked for.
    layer_opacity = opacity if layer.opacity is None else value(layer.opacity, context, 1.0)
    if layer_opacity <= 0:
        return

    element = layer.element or {}
    kind = element.get("type")
    transform = _layer_transform(layer)

    if transform:
        # A transformed layer is drawn into its own canvas and then resampled
        # through the inverse transform. Slower than transforming each shape's
        # maths, but it is one implementation for every primitive rather than
        # nine, and it cannot disagree with itself between them.
        #
        # The scratch tracks coverage. Asking "did this pixel change colour"
        # instead made black content vanish (a black shape on a black scratch
        # changes nothing) and left every anti-aliased edge premultiplied
        # against black, i.e. fringed.
        placement = _resolve_transform(transform, context)
        scratch = _scratch_for(canvas, placement)
        _draw_element(scratch, element, kind, context, 1.0)
        _blit_transformed(canvas, scratch, placement, layer_opacity)
        return

    _draw_element(canvas, element, kind, context, layer_opacity)


def _draw_element(
    canvas: Canvas, element: dict, kind: Any, context: RenderContext, opacity: float
) -> None:
    """One element into one canvas: a group's children, or a primitive."""
    if kind == "group":
        for child in element.get("children") or []:
            _draw_layer(canvas, Layer.model_validate(child), context, opacity)
        return
    drawer = DRAWERS.get(str(kind))
    if drawer is None:
        raise ConfigError(f"no renderer for primitive type {kind!r}")
    drawer(canvas, element, context, opacity)


@dataclass(frozen=True)
class Placement:
    """A layer transform with its expressions already evaluated.

    Forward, as the studio's canvas applies it:
    ``dest = T(origin) · T(translate) · R(rotate) · S(scale) · T(-origin)``.
    """

    tx: float
    ty: float
    rotate: float
    sx: float
    sy: float
    ox: float
    oy: float

    def to_source(self, xs: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Where destination points came from — the inverse map.

        Walking the DESTINATION and asking where each pixel came from is what
        avoids the holes a forward mapping leaves between resampled pixels.
        """
        px, py = xs - self.tx - self.ox, ys - self.ty - self.oy
        cos_r, sin_r = math.cos(-self.rotate), math.sin(-self.rotate)
        rx = px * cos_r - py * sin_r
        ry = px * sin_r + py * cos_r
        return (
            rx / (self.sx if self.sx else 1e-9) + self.ox,
            ry / (self.sy if self.sy else 1e-9) + self.oy,
        )


def _resolve_transform(transform: Any, context: RenderContext) -> Placement:
    """Evaluate a layer transform's fields once, before anything is drawn."""
    data = transform if isinstance(transform, dict) else transform.model_dump()
    translate = data.get("translate") or [0, 0]
    scale_field = data.get("scale")
    if isinstance(scale_field, list):
        sx, sy = value(scale_field[0], context, 1.0), value(scale_field[1], context, 1.0)
    else:
        sx = sy = value(scale_field, context, 1.0)
    origin = data.get("origin") or [0, 0]
    return Placement(
        tx=value(translate[0], context),
        ty=value(translate[1], context),
        rotate=value(data.get("rotate"), context),
        sx=sx,
        sy=sy,
        ox=value(origin[0], context),
        oy=value(origin[1], context),
    )


# A transformed layer's scratch is sized to the region that can actually reach
# the canvas. This caps it, so a scene animating `scale` toward zero asks for a
# large but bounded buffer rather than for all the memory in the machine.
MAX_SCRATCH_PIXELS = 64_000_000


def _scratch_for(canvas: Canvas, placement: Placement) -> Canvas:
    """A blank canvas covering exactly the source region this transform reads.

    A same-sized scratch anchored at (0, 0) — which is what this used to be —
    silently loses every part of a layer drawn outside the canvas's own box.
    A group whose children sit around the origin and are then translated into
    view is the ordinary case: most of it is at negative coordinates and
    simply never appeared.
    """
    corners_x = np.array([0.0, canvas.width, 0.0, canvas.width]) + canvas.origin_x
    corners_y = np.array([0.0, 0.0, canvas.height, canvas.height]) + canvas.origin_y
    source_x, source_y = placement.to_source(corners_x, corners_y)
    # A pixel of margin each way: the blit samples nearest-neighbour, so a
    # sample can land just outside the mapped corner.
    x0 = int(math.floor(float(source_x.min()))) - 1
    y0 = int(math.floor(float(source_y.min()))) - 1
    width = max(int(math.ceil(float(source_x.max()))) + 1 - x0, 1)
    height = max(int(math.ceil(float(source_y.max()))) + 1 - y0, 1)
    if width * height > MAX_SCRATCH_PIXELS:
        raise ConfigError(
            f"a layer transform maps a {width}x{height} region onto a "
            f"{canvas.width}x{canvas.height} canvas, which is more than this renderer "
            f"will buffer — a scale near zero is the usual cause"
        )
    return Canvas(width, height, (np.zeros(3), 0.0), track_coverage=True, origin=(x0, y0))


def _blit_transformed(
    canvas: Canvas, scratch: Canvas, placement: Placement, opacity: float
) -> None:
    """Resample a drawn layer through translate/rotate/scale about an origin."""
    xs, ys = np.meshgrid(canvas.xs, canvas.ys)
    source_x, source_y = placement.to_source(xs, ys)

    # Nearest neighbour: the source was drawn anti-aliased already, and
    # interpolating a second time would soften every edge again. The floor is
    # of (source - origin), i.e. which scratch pixel that point falls in.
    ix = np.floor(source_x - scratch.origin_x).astype(int)
    iy = np.floor(source_y - scratch.origin_y).astype(int)
    valid = (ix >= 0) & (ix < scratch.width) & (iy >= 0) & (iy < scratch.height)
    ix = np.clip(ix, 0, scratch.width - 1)
    iy = np.clip(iy, 0, scratch.height - 1)

    # Composite by COVERAGE, never by colour. The scratch holds premultiplied
    # colour (it started at zero), so the colour is recovered before mixing —
    # otherwise a half-covered edge pixel would carry half the fill's colour
    # into the destination on top of its own weight, darkening every rim.
    assert scratch.coverage is not None
    sampled = scratch.unpremultiplied()[iy, ix]
    covered = scratch.coverage[iy, ix] * valid
    weight = covered * opacity
    canvas.pixels = canvas.pixels * (1.0 - weight[..., None]) + sampled * weight[..., None]
    canvas.note_coverage(weight)


def headless_render(
    scene: Scene,
    params: dict[str, Any] | None = None,
    time: float = 0.0,
    width: int | None = None,
    height: int | None = None,
    dt: float = 0.0,
) -> np.ndarray:
    """Render one frame to an ``(height, width, 3)`` uint8 array.

    The primary implementation: no display, no window, no renderer. The
    display path (``SceneStimulus``) blits what this produces, so what an
    experiment shows is what a test can inspect.
    """
    if width is None or height is None:
        # A scene may declare its own canvas; without one the caller must
        # say, because guessing would silently change every geometry in it.
        declared = scene.declared_size()
        if declared is not None:
            width = width or declared[0]
            height = height or declared[1]
    if not width or not height:
        raise ConfigError(
            "headless_render needs a canvas size: pass width and height, or give the "
            "scene a 'canvas' block"
        )

    context = RenderContext(
        width=int(width), height=int(height), time=time, dt=dt, params=params or {}
    )
    background = string_value(scene.background, context, DEFAULT_BACKGROUND)
    canvas = Canvas(int(width), int(height), parse_color(background))
    for layer in scene.layers:
        _draw_layer(canvas, layer, context, 1.0)
    return canvas.to_uint8()


class SceneStimulus:
    """A scene, as a stimulus a phase can draw.

    Satisfies alhazen's ``Stimulus`` protocol, so any phase that draws
    stimulus keys can draw one. ``update(dt)`` advances the scene's own clock;
    ``draw()`` renders that moment and puts it on screen.

    Scene time comes from the trial's dt, never from a wall clock: two runs of
    the same trial must show the same frames, and a stimulus that read the
    time of day could not promise that.

    Coordinates (spec 7.3). A scene is y-**down** logical pixels from its
    top-left corner; the screen is centered px with y **up**. The rendered
    image is placed centred and scaled by one factor in both axes —
    letterboxed to the scene's own aspect ratio, so a 4:3 scene on a 16:9
    screen gets bars at the sides rather than being stretched into a stimulus
    whose spatial frequency differs by axis. That factor is ``scale``, and it
    is recorded rather than implicit: an analysis converting a scene
    coordinate to degrees needs it.

    ``scene_to_screen`` is the one place the conversion happens.
    """

    def __init__(
        self,
        display: Any,
        screen: Any,
        scene: Scene,
        params: dict[str, Any] | None = None,
        width: int | None = None,
        height: int | None = None,
        scale: float | None = None,
        pos: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self._scene = scene
        self._params = dict(params or {})
        declared = scene.declared_size()
        self._width = int(width or (declared[0] if declared else screen.width_px))
        self._height = int(height or (declared[1] if declared else screen.height_px))
        self._screen = screen
        # None means "letterbox onto this screen": the largest single factor
        # that fits the scene's rectangle inside it. A scene rendered at the
        # screen's own size gets exactly 1.0, so the default costs nothing
        # where there is nothing to fit.
        self.scale = (
            float(scale)
            if scale is not None
            else min(screen.width_px / self._width, screen.height_px / self._height)
        )
        self._pos = pos
        self._simulated = display.kind == "simulated"
        self.time = 0.0
        # Every frame's rendered array, on the simulated backend. That trace
        # is how a test asserts what was shown without a renderer installed.
        self.frames: list[np.ndarray] = []
        self._stim: Any = None
        self._display = display
        if not self._simulated:
            from psychopy import visual

            self._stim = visual.ImageStim(
                display.window,
                units="pix",
                size=(self._width * self.scale, self._height * self.scale),
                pos=pos,
            )

    @property
    def size_px(self) -> tuple[float, float]:
        """The drawn rectangle's size on screen, in screen pixels."""
        return (self._width * self.scale, self._height * self.scale)

    def scene_to_screen(self, x: float, y: float) -> tuple[float, float]:
        """A scene coordinate as centered screen px (y up).

        The scene's origin is its top-left corner with y growing down; the
        screen's is its centre with y growing up. So x moves out from the
        scene's centre and y flips sign, both scaled by the letterbox factor,
        and the whole rectangle is offset by ``pos``.
        """
        return (
            self._pos[0] + (x - self._width / 2.0) * self.scale,
            self._pos[1] - (y - self._height / 2.0) * self.scale,
        )

    @property
    def last_frame(self) -> np.ndarray | None:
        return self.frames[-1] if self.frames else None

    def update(self, dt: float) -> None:
        # dt arrives in seconds (the engine's unit); the scene's own clock is
        # in seconds too, while the expression language's `dt` is in
        # milliseconds — the format's quirk, kept for parity.
        self.time += dt
        self._dt_ms = dt * 1000.0

    def render(self) -> np.ndarray:
        return headless_render(
            self._scene,
            params=self._params,
            time=self.time,
            width=self._width,
            height=self._height,
            dt=getattr(self, "_dt_ms", 0.0),
        )

    def draw(self) -> None:
        frame = self.render()
        if self._simulated:
            self.frames.append(frame)
            return
        # psychopy wants floats in [-1, 1] and its own y-up orientation, so
        # the array is scaled and flipped exactly once, here.
        self._stim.image = np.flipud(frame.astype(float) / 127.5 - 1.0)
        self._stim.draw()
