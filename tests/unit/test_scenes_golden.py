"""Pixel parity against illusion-studio's own reference images.

Every PNG in ``tests/fixtures/scenes/`` is rendered by the studio itself
(Skia, via ``@napi-rs/canvas``) from the scene beside it in
``reference_scenes.json``. So this compares alhazen's numpy renderer against a
production 2-D rasteriser on the same scenes — never against alhazen's own
earlier output, which would only pin a bug in place.

The tolerances below are **measured, then pinned** — not aspirational. Two
different rasterisers cannot agree pixel-for-pixel on an anti-aliased edge:
where a boundary falls between two pixels one may write 200 and the other 55,
and no amount of care removes that. What CAN agree is everything computed
per-pixel rather than per-edge, and it does — gratings, Gabors, noise,
stripes, dashes and a translated block are all **identical**.

| scene | mean abs difference | pixels beyond 8/255 |
|---|---|---|
| stripes, stripes-defaults, stripes-overhang | 0 (identical) | none |
| grating sine/square/dim/default-base, gabor | 0 (identical) | none |
| noise-uniform | 0 (identical) | none |
| line-dashed | 0 (identical) | none |
| black-under-transform | 0 (identical) | none |
| rect (rounded, stroked) | 0.05/255 | 0.2% |
| line-caps | 0.09/255 | 0.5% |
| polygon-winding | 0.09/255 | 0.3% |
| dotfield-defaults | 0.11/255 | 0.5% |
| polygon | 0.15/255 | 1.3% |
| circle | 0.15/255 | 0.6% |
| ellipse (rotated, stroked) | 0.17/255 | 0.9% |
| dotfield-lifetime | 0.24/255 | 1.0% |
| dotfield-coherent | 0.26/255 | 1.1% |
| transform-origin-scale | 0.29/255 | 1.0% |
| group-transform | 0.36/255 | 1.4% |

Everything that is not identical differs only on a rim: a shape's
anti-aliased boundary, or the edge of a resampled block. A failure here means
the renderer moved, not that the rasterisers disagree — the numbers above ARE
the disagreement, and they are pinned so a change to either shows up.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from alhazen.scenes import headless_render, load_scene

FIXTURES = Path(__file__).parents[1] / "fixtures" / "scenes"
SPEC = json.loads((FIXTURES / "reference_scenes.json").read_text())

# (max mean absolute difference, max fraction of pixels beyond 8/255).
# Each is the measured value with a little headroom, and each is explained by
# the table above rather than being a number someone chose.
EXACT = (0.01, 0.0)
TOLERANCES = {
    # Per-pixel procedural content: nothing about the rasteriser matters, so
    # nothing about it may differ.
    "stripes": EXACT,
    "stripes-defaults": EXACT,
    "stripes-overhang": EXACT,
    "grating-sine": EXACT,
    "grating-square": EXACT,
    "grating-dim": EXACT,  # baseLuminance 64: where the amplitude formula shows
    "grating-default-base": EXACT,
    "gabor": EXACT,
    "noise-uniform": EXACT,
    "line-dashed": EXACT,
    "black-under-transform": EXACT,  # a black block that used to disappear
    # Shapes: only their anti-aliased rims disagree.
    "rect": (0.3, 0.01),
    "ellipse": (0.5, 0.02),
    "circle": (0.5, 0.02),
    "polygon": (0.5, 0.03),
    "polygon-winding": (0.3, 0.01),
    "line-caps": (0.3, 0.02),
    # Dot fields: many small rims, so a slightly larger share of pixels.
    "dotfield-coherent": (0.5, 0.02),
    "dotfield-lifetime": (0.5, 0.02),
    "dotfield-defaults": (0.4, 0.02),
    # Resampled blocks: the rim of the transformed content.
    "group-transform": (0.6, 0.03),
    "transform-origin-scale": (0.5, 0.02),
}


def render_reference(name: str) -> tuple[np.ndarray, np.ndarray]:
    from PIL import Image

    body = dict(SPEC["scenes"][name])
    # A scene whose appearance depends on the clock carries the moment it was
    # rendered at, so the reference PNG and this render describe the same
    # instant.
    time = float(body.pop("__time__", 0.0))
    reference = np.asarray(Image.open(FIXTURES / f"{name}.png").convert("RGB")).astype(float)
    ours = headless_render(
        load_scene({"version": 1, **body}),
        width=SPEC["width"],
        height=SPEC["height"],
        time=time,
    ).astype(float)
    return ours, reference


@pytest.mark.parametrize("name", sorted(TOLERANCES))
def test_matches_the_studios_reference(name):
    pytest.importorskip("PIL", reason="reading the reference PNGs needs Pillow")
    ours, reference = render_reference(name)
    assert ours.shape == reference.shape

    difference = np.abs(ours - reference)
    mean_tolerance, beyond_tolerance = TOLERANCES[name]
    beyond = (difference.max(axis=2) > 8).mean()

    assert difference.mean() <= mean_tolerance, (
        f"{name}: mean difference {difference.mean():.2f}/255 exceeds the pinned "
        f"{mean_tolerance}/255 — the renderer moved"
    )
    assert beyond <= beyond_tolerance, (
        f"{name}: {beyond:.1%} of pixels differ by more than 8/255, above the pinned "
        f"{beyond_tolerance:.1%}"
    )


def test_every_committed_reference_is_compared():
    """A reference PNG nobody compares against is a file, not a test.

    `rect.png` and `ellipse.png` sat in the fixtures directory for a whole
    phase without appearing in the comparison set.
    """
    committed = {path.stem for path in FIXTURES.glob("*.png")}
    assert committed == set(TOLERANCES)


@pytest.mark.parametrize(
    "name", sorted(name for name, tolerance in TOLERANCES.items() if tolerance is EXACT)
)
def test_per_pixel_content_is_identical(name):
    """Where there is no anti-aliasing to disagree about, the match is exact.

    Gratings, noise and stripes have a value at every pixel rather than an
    edge between two; a dash is flat fills on a straight line. Nothing about
    the rasteriser matters, so nothing about it may differ.
    """
    pytest.importorskip("PIL")
    ours, reference = render_reference(name)
    assert np.array_equal(ours, reference)


def test_the_interior_of_a_shape_is_exact():
    """The rim may differ; what the shape is filled with may not."""
    pytest.importorskip("PIL")
    ours, reference = render_reference("circle")
    # Well inside the circle (radius 50 at 100,75): sample a disc of radius 40.
    ys, xs = np.mgrid[0:150, 0:200]
    interior = np.hypot(xs - 100, ys - 75) < 40
    assert np.array_equal(ours[interior], reference[interior])
