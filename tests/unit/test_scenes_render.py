"""The scene loader and renderer: what is drawn, where, and what is refused."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from alhazen.errors import ConfigError
from alhazen.scenes import headless_render, load_scene, scene_param_names
from alhazen.scenes.render import SceneStimulus, parse_color
from alhazen.testing import FakeClock, FakeDisplay
from support import SCREEN

# The studio checkout is a sibling of the alhazen checkout (the convention the
# phase specs use); tests that need it skip when it is absent.
STUDIO_SCENES = (
    Path(__file__).resolve().parents[2].parent / "illusion-studio" / "examples" / "scenes"
)


def scene_of(*elements, background="#000000"):
    return load_scene(
        {
            "version": 1,
            "background": background,
            "layers": [
                element if "element" in element else {"element": element} for element in elements
            ],
        }
    )


def render(*elements, width=200, height=150, background="#000000", time=0.0, params=None):
    return headless_render(
        scene_of(*elements, background=background),
        params=params or {},
        time=time,
        width=width,
        height=height,
    )


class TestLoader:
    def test_a_future_version_says_to_migrate_in_the_studio(self):
        # alhazen deliberately does not fork the format's migrations.
        with pytest.raises(ConfigError, match="illusion-studio"):
            load_scene({"version": 2, "layers": []})

    def test_out_of_subset_primitives_are_named_with_their_path(self):
        # A renderer that quietly skipped a text layer would produce a
        # stimulus that looks almost right, which is worse than none.
        with pytest.raises(ConfigError, match=r"layers\[0\].element is a 'text'"):
            load_scene({"version": 1, "layers": [{"element": {"type": "text", "text": "x"}}]})

    def test_a_nested_group_is_checked_too(self):
        scene = {
            "version": 1,
            "layers": [
                {
                    "element": {
                        "type": "group",
                        "children": [{"element": {"type": "wedge", "cx": 1}}],
                    }
                }
            ],
        }
        with pytest.raises(ConfigError, match=r"children\[0\]"):
            load_scene(scene)

    def test_blend_modes_and_clips_are_refused(self):
        with pytest.raises(ConfigError, match="blend modes"):
            load_scene(
                {
                    "version": 1,
                    "layers": [{"blend": "multiply", "element": {"type": "rect", "x": 0}}],
                }
            )

    def test_an_unknown_type_is_refused(self):
        with pytest.raises(ConfigError, match="unknown type"):
            load_scene({"version": 1, "layers": [{"element": {"type": "sprite"}}]})

    def test_json_text_and_paths_both_load(self, tmp_path):
        data = {"version": 1, "layers": []}
        assert load_scene(json.dumps(data)).version == 1
        path = tmp_path / "scene.json"
        path.write_text(json.dumps(data))
        assert load_scene(path).version == 1

    def test_the_studios_own_example_scenes_load_and_render(self):
        """Load AND render. Loading alone proves only that the JSON parses:
        `follower.json`'s `ref()` and the dot field's wrongness both lived
        past the loader, in code that only runs when a frame is drawn."""
        if not STUDIO_SCENES.exists():
            pytest.skip("illusion-studio checkout not present")
        rendered, rejected = [], []
        for path in sorted(STUDIO_SCENES.glob("*.json")):
            try:
                scene = load_scene(path)
            except ConfigError as error:
                # An out-of-subset scene must be refused BY NAME, which is
                # the contract; it is not a test failure.
                rejected.append((path.name, str(error)))
                continue
            # Whatever the scene asks for; the smoke test is that it draws,
            # not that these are the right values for the illusion.
            params = dict.fromkeys(scene_param_names(scene), 0.5)
            for time in (0.0, 0.4):
                frame = headless_render(scene, params=params, time=time, width=160, height=120)
                assert frame.shape == (120, 160, 3), path.name
                assert frame.dtype == np.uint8
            rendered.append(path.name)
        assert rendered, "no example scene was in the subset"
        for name, message in rejected:
            assert "outside the subset" in message or "does not render" in message, (
                f"{name}: {message}"
            )

    def test_a_scene_using_ref_is_among_the_refusals(self):
        """A regression guard for the studio's own `follower.json`, which used
        to load cleanly and die generically on its first draw."""
        if not STUDIO_SCENES.exists():
            pytest.skip("illusion-studio checkout not present")
        follower = STUDIO_SCENES / "follower.json"
        if not follower.exists():
            pytest.skip("follower.json is no longer in the studio's examples")
        with pytest.raises(ConfigError, match=r"ref\(\)"):
            load_scene(follower)


class TestExpressionsAreResolvedAtLoad:
    """Spec 7.1: expressions are checked at *parse* time. A scene using a
    feature alhazen does not implement, or naming something that does not
    exist, must fail when the file is opened — not on the frame that happens
    to take the branch containing it, which may be minutes into a session."""

    def scene(self, expr, where="cx"):
        return {
            "version": 1,
            "background": "#000",
            "layers": [{"element": {"type": "circle", where: {"expr": expr}, "radius": 5}}],
        }

    def test_ref_is_refused_by_name_and_path(self):
        with pytest.raises(ConfigError, match=r"layers\[0\]\.element\.cx uses ref\(\)"):
            load_scene(self.scene("ref('other').cx"))

    def test_ref_inside_a_group_names_the_child(self):
        body = {
            "version": 1,
            "background": "#000",
            "layers": [
                {
                    "element": {
                        "type": "group",
                        "children": [
                            {
                                "element": {
                                    "type": "circle",
                                    "cx": {"expr": "ref('a').cx"},
                                    "radius": 5,
                                }
                            }
                        ],
                    }
                }
            ],
        }
        with pytest.raises(ConfigError, match=r"layers\[0\]\.children\[0\]"):
            load_scene(body)

    def test_a_typo_in_an_untaken_branch_is_still_caught(self):
        # `time > 1e9` is false for the whole of any session, so the
        # evaluator would never reach `widht` at all.
        with pytest.raises(ConfigError, match="widht"):
            load_scene(self.scene("time > 1e9 ? widht : 10"))

    def test_a_known_function_and_builtin_are_accepted(self):
        load_scene(self.scene("sin(time) * width / 2 + 50"))

    def test_a_declared_param_is_accepted_as_a_bare_name(self):
        load_scene(self.scene("contrast * 10"), declared_params=["contrast"])

    def test_an_undeclared_bare_name_is_refused(self):
        with pytest.raises(ConfigError, match="contrast"):
            load_scene(self.scene("contrast * 10"))

    def test_params_member_access_needs_no_declaration(self):
        # `params.foo` reads the identifier `params`, which the host supplies;
        # whether `foo` is there is answered where the params are known.
        load_scene(self.scene("params.freq * 3"))

    def test_an_expression_that_does_not_parse_names_its_field(self):
        with pytest.raises(ConfigError, match=r"layers\[0\]\.element\.cx"):
            load_scene(self.scene("10 +"))

    def test_a_transform_expression_is_checked_too(self):
        body = {
            "version": 1,
            "background": "#000",
            "layers": [
                {
                    "transform": {"translate": [{"expr": "tiem"}, 0]},
                    "element": {"type": "circle", "cx": 10, "cy": 10, "radius": 5},
                }
            ],
        }
        with pytest.raises(ConfigError, match="tiem"):
            load_scene(body)


class TestColours:
    def test_the_forms_scenes_use(self):
        assert np.array_equal(parse_color("#ffffff")[0], [255, 255, 255])
        assert np.array_equal(parse_color("#f00")[0], [255, 0, 0])
        rgb, alpha = parse_color("rgba(10, 20, 30, 0.5)")
        assert np.array_equal(rgb, [10, 20, 30]) and alpha == 0.5

    def test_an_unrecognised_colour_is_an_error(self):
        # A stimulus drawn in the wrong colour is worse than one that
        # refused to draw.
        with pytest.raises(ConfigError, match="unrecognised colour"):
            parse_color("cornflowerblue")


class TestPrimitiveGeometry:
    def test_a_rect_covers_exactly_its_own_box(self):
        image = render(
            {"type": "rect", "x": 50, "y": 40, "width": 60, "height": 30, "fill": "#ffffff"}
        )
        assert image[41, 51].tolist() == [255, 255, 255]  # just inside
        assert image[68, 108].tolist() == [255, 255, 255]
        assert image[38, 51].tolist() == [0, 0, 0]  # just outside
        assert image[41, 115].tolist() == [0, 0, 0]

    def test_a_circle_is_round(self):
        image = render({"type": "circle", "cx": 100, "cy": 75, "radius": 40, "fill": "#ffffff"})
        # On the axes at r-2 it is inside; on the diagonal at r-2 it is not.
        assert image[75, 100 + 38].tolist() == [255, 255, 255]
        diagonal = int(40 / np.sqrt(2)) + 3
        assert image[75 + diagonal, 100 + diagonal].tolist() == [0, 0, 0]

    def test_edges_are_anti_aliased_rather_than_stepped(self):
        image = render({"type": "circle", "cx": 100, "cy": 75, "radius": 40, "fill": "#ffffff"})
        # The boundary is a ring of partial mixes — which is what
        # coverage-based rasterisation is for. Counted over the whole image
        # rather than one row: where the edge runs straight along an axis it
        # can fall exactly on a pixel boundary and be fully in or out, and a
        # single row through the centre is exactly such a place.
        partial = (image[:, :, 0] > 10) & (image[:, :, 0] < 245)
        assert partial.sum() > 50

    def test_an_ellipse_respects_both_radii(self):
        image = render(
            {
                "type": "ellipse",
                "cx": 100,
                "cy": 75,
                "radiusX": 60,
                "radiusY": 20,
                "fill": "#ffffff",
            }
        )
        assert image[75, 155].tolist() == [255, 255, 255]  # wide axis
        assert image[50, 100].tolist() == [0, 0, 0]  # short axis

    def test_a_polygon_fills_its_interior(self):
        image = render(
            {
                "type": "polygon",
                "points": [[20, 20], [180, 20], [100, 130]],
                "fill": "#ffffff",
            }
        )
        assert image[30, 100].tolist() == [255, 255, 255]
        assert image[120, 30].tolist() == [0, 0, 0]

    def test_a_line_is_drawn_along_its_points(self):
        image = render(
            {
                "type": "line",
                "points": [[10, 75], [190, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 5,
            }
        )
        assert image[75, 100].tolist() == [255, 255, 255]
        assert image[60, 100].tolist() == [0, 0, 0]

    def test_stripes_are_not_clipped_in_the_direction_they_repeat(self):
        """The studio's loop starts a bar at ``x - shift`` and keeps stepping
        while the bar's own start is inside the box, so bars overhang the box
        at both ends. Clipping to the box narrowed the first and last bar of
        every stripe field."""
        image = render(
            {
                "type": "stripes",
                "x": 60,
                "y": 40,
                "width": 40,
                "height": 40,
                "period": 20,
                "thickness": 14,
                "offset": 7,
                "color": "#ffffff",
            }
        )

        # The first bar starts at x = 53, before the box's own left edge.
        assert image[60, 55, 0] == 255
        # Vertical extent is still clipped: bars span the box's height only.
        assert image[35, 55, 0] == 0

    def test_stripes_default_to_a_one_pixel_bar_every_pixel(self):
        """The studio's defaults are period 1 and thickness 1. Defaulting to a
        20 px period with half-period bars drew a different stimulus entirely
        for any scene that left them out."""
        image = render(
            {"type": "stripes", "x": 40, "y": 40, "width": 40, "height": 20, "color": "#ffffff"}
        )

        assert image[50, 40:80, 0].min() == 255

    def test_stripes_alternate_at_their_period(self):
        image = render(
            {
                "type": "stripes",
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 150,
                "period": 20,
                "thickness": 10,
                "color": "#ffffff",
            }
        )
        row = image[75, :, 0]
        assert row[5] == 255 and row[15] == 0 and row[25] == 255


class TestCanvasStrokeSemantics:
    """What a 2-D canvas does at the edges of a stroke, which is what the
    studio's rasteriser does and what a scene's author saw when they drew it."""

    def test_a_line_ends_flush_with_its_last_point(self):
        """Butt caps are the canvas default. A capsule per segment — which is
        what this was — is a ROUND cap, so every line ran half a stroke width
        past both ends."""
        image = render(
            {
                "type": "line",
                "points": [[50, 75], [150, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 20,
            }
        )

        assert image[75, 51, 0] == 255  # just inside the first point
        assert image[75, 45, 0] == 0  # 5 px before it: nothing

    def test_a_round_cap_extends_past_the_end(self):
        image = render(
            {
                "type": "line",
                "points": [[50, 75], [150, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 20,
                "cap": "round",
            }
        )

        assert image[75, 45, 0] == 255

    def test_a_square_cap_extends_by_half_the_width(self):
        image = render(
            {
                "type": "line",
                "points": [[50, 75], [150, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 20,
                "cap": "square",
            }
        )

        assert image[75, 45, 0] == 255  # within half the width
        assert image[75, 35, 0] == 0  # beyond it

    def test_a_corner_is_mitred_not_rounded(self):
        image = render(
            {
                "type": "line",
                "points": [[40, 40], [100, 40], [100, 100]],
                "stroke": "#ffffff",
                "strokeWidth": 20,
                "join": "miter",
            }
        )
        rounded = render(
            {
                "type": "line",
                "points": [[40, 40], [100, 40], [100, 100]],
                "stroke": "#ffffff",
                "strokeWidth": 20,
                "join": "round",
            }
        )

        # The outer corner of the turn: filled by a miter, cut by a round join.
        assert image[31, 109, 0] == 255
        assert rounded[31, 109, 0] < 255

    def test_a_dash_leaves_gaps_along_the_line(self):
        image = render(
            {
                "type": "line",
                "points": [[20, 75], [180, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 10,
                "dash": [20, 20],
            }
        )

        row = image[75, :, 0]
        assert row[25] == 255  # inside the first dash
        assert row[50] == 0  # inside the first gap
        assert row[65] == 255  # and on again

    def test_a_dash_carries_across_a_corner(self):
        """The pattern runs along the whole path's arc length, not restarting
        at each segment."""
        straight = render(
            {
                "type": "line",
                "points": [[20, 75], [180, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 8,
                "dash": [17, 11],
            }
        )
        split = render(
            {
                "type": "line",
                "points": [[20, 75], [100, 75], [180, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 8,
                "dash": [17, 11],
            }
        )

        assert np.array_equal(straight, split)

    def test_an_empty_dash_is_a_solid_line(self):
        dashed = render(
            {
                "type": "line",
                "points": [[20, 75], [180, 75]],
                "stroke": "#ffffff",
                "strokeWidth": 8,
                "dash": [],
            }
        )
        solid = render(
            {"type": "line", "points": [[20, 75], [180, 75]], "stroke": "#ffffff", "strokeWidth": 8}
        )

        assert np.array_equal(dashed, solid)

    def test_a_self_crossing_polygon_fills_its_centre(self):
        """Canvas `fill()` is NONZERO winding. Under even-odd — which this
        was — a five-pointed star has a hole where its body should be."""
        star = render(
            {
                "type": "polygon",
                "points": [[100, 15], [40, 130], [175, 55], [25, 55], [160, 130]],
                "fill": "#ffffff",
            }
        )

        assert star[70, 100, 0] == 255

    def test_a_rounded_rects_stroke_straddles_its_edge(self):
        """It used to route through an inward erosion, putting the whole
        outline inside the shape — a 4 px stroke landing 2 px off."""
        image = render(
            {
                "type": "rect",
                "x": 50,
                "y": 40,
                "width": 100,
                "height": 70,
                "fill": "#000000",
                "stroke": "#ffffff",
                "strokeWidth": 8,
                "cornerRadius": 10,
            },
            background="#000000",
        )

        # The edge is at x=50; half the stroke lies each side of it.
        assert image[75, 48, 0] == 255
        assert image[75, 52, 0] == 255
        assert image[75, 40, 0] == 0  # well outside
        assert image[75, 60, 0] == 0  # well inside

    def test_an_ellipse_stroke_straddles_its_edge_too(self):
        image = render(
            {
                "type": "ellipse",
                "cx": 100,
                "cy": 75,
                "radiusX": 60,
                "radiusY": 30,
                "fill": "#000000",
                "stroke": "#ffffff",
                "strokeWidth": 8,
            },
            background="#000000",
        )

        assert image[75, 43, 0] == 255  # outside the rim at x = 40
        assert image[75, 37, 0] == 255  # inside it
        assert image[75, 55, 0] == 0


class TestProceduralPrimitives:
    def test_a_gratings_amplitude_does_not_depend_on_its_base(self):
        """The studio's amplitude is ``127 × contrast`` — a fixed fraction of
        the 8-bit range, so contrast means the same thing at every base.
        Scaling by the base instead gave a dim patch half the modulation it
        asked for, which the old goldens never sampled."""
        dim = render(
            {
                "type": "grating",
                "cx": 100,
                "cy": 75,
                "width": 100,
                "height": 100,
                "spatialFreq": 0.05,
                "contrast": 1.0,
                "baseLuminance": 64,
            }
        )

        patch = dim[40:110, 60:140, 0].astype(float)
        # base 64 ± 127, clipped at 0: peaks reach 191, troughs bottom out.
        assert patch.max() == pytest.approx(191, abs=1)
        assert patch.min() == 0

    def test_the_default_base_luminance_is_the_studios(self):
        image = render(
            {
                "type": "grating",
                "cx": 100,
                "cy": 75,
                "width": 60,
                "height": 60,
                "spatialFreq": 0.05,
                "contrast": 0.0,
            }
        )

        assert image[75, 100, 0] == 128

    def test_a_sine_grating_has_the_right_period_and_contrast(self):
        image = render(
            {
                "type": "grating",
                "cx": 100,
                "cy": 75,
                "width": 200,
                "height": 150,
                "spatialFreq": 0.05,  # a cycle every 20 px
                "contrast": 1.0,
                "baseLuminance": 127.5,
            },
            background="#808080",
        )
        row = image[75, :, 0].astype(float)
        # Peak-to-peak spans the full range at contrast 1.
        assert row.max() > 250 and row.min() < 5
        # And the period is 20 px: the autocorrelation peaks there.
        centred = row - row.mean()
        correlation = [float(np.dot(centred[:-lag], centred[lag:])) for lag in range(5, 40)]
        assert int(np.argmax(correlation)) + 5 == pytest.approx(20, abs=1)

    def test_a_square_grating_is_two_levels(self):
        image = render(
            {
                "type": "grating",
                "cx": 100,
                "cy": 75,
                "width": 200,
                "height": 150,
                "spatialFreq": 0.05,
                "shape": "square",
                "contrast": 1.0,
            },
            background="#808080",
        )
        values = np.unique(image[75, :, 0])
        assert len(values) <= 3  # two levels, plus possible rounding

    def test_an_envelope_makes_a_gabor(self):
        image = render(
            {
                "type": "grating",
                "cx": 100,
                "cy": 75,
                "width": 200,
                "height": 150,
                "spatialFreq": 0.05,
                "contrast": 1.0,
                "envelopeSigma": 20,
            },
            background="#808080",
        )
        row = image[75, :, 0].astype(float)
        centre_swing = row[80:120].max() - row[80:120].min()
        edge_swing = row[0:20].max() - row[0:20].min()
        # The contrast falls off with distance, which is the envelope.
        assert centre_swing > 200
        assert edge_swing < 20

    def test_a_dot_field_is_a_pure_function_of_time(self):
        element = {
            "type": "dotField",
            "cx": 100,
            "cy": 75,
            "width": 160,
            "height": 120,
            "count": 40,
            "dotRadius": 3,
            "speed": 30,
            "seed": 7,
        }
        # Seeking to a moment gives the same picture as any other route to
        # it: no incremental state to get out of step.
        first = render(element, time=1.5)
        again = render(element, time=1.5)
        later = render(element, time=1.6)
        assert np.array_equal(first, again)
        assert not np.array_equal(first, later)

    def test_a_dot_fields_seed_changes_the_pattern(self):
        base = {
            "type": "dotField",
            "cx": 100,
            "cy": 75,
            "width": 160,
            "height": 120,
            "count": 30,
            "dotRadius": 3,
            "seed": 1,
        }
        assert not np.array_equal(render(base), render({**base, "seed": 2}))

    def test_noise_is_reproducible_from_its_seed(self):
        element = {
            "type": "noise",
            "x": 20,
            "y": 20,
            "width": 100,
            "height": 80,
            "mean": 128,
            "sigma": 40,
            "seed": 3,
        }
        assert np.array_equal(render(element), render(element))
        assert not np.array_equal(render(element), render({**element, "seed": 4}))

    def test_gaussian_noise_has_the_requested_mean(self):
        image = render(
            {
                "type": "noise",
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 150,
                "mean": 120,
                "sigma": 20,
                "seed": 5,
                "distribution": "gaussian",
            }
        )
        assert image[:, :, 0].mean() == pytest.approx(120, abs=3)


class TestLayers:
    def test_opacity_mixes_toward_the_background(self):
        image = render(
            {
                "opacity": 0.5,
                "element": {
                    "type": "rect",
                    "x": 0,
                    "y": 0,
                    "width": 200,
                    "height": 150,
                    "fill": "#ffffff",
                },
            }
        )
        assert image[75, 100, 0] == pytest.approx(128, abs=2)

    def test_an_invisible_layer_draws_nothing(self):
        image = render(
            {
                "visible": 0,
                "element": {
                    "type": "rect",
                    "x": 0,
                    "y": 0,
                    "width": 200,
                    "height": 150,
                    "fill": "#ffffff",
                },
            }
        )
        assert image.max() == 0

    def test_layers_draw_back_to_front(self):
        image = render(
            {"type": "rect", "x": 0, "y": 0, "width": 200, "height": 150, "fill": "#ff0000"},
            {"type": "rect", "x": 50, "y": 50, "width": 50, "height": 50, "fill": "#00ff00"},
        )
        assert image[75, 75].tolist() == [0, 255, 0]  # the later layer wins
        assert image[10, 10].tolist() == [255, 0, 0]

    def test_a_transform_moves_what_it_contains(self):
        plain = render(
            {"type": "rect", "x": 20, "y": 20, "width": 30, "height": 30, "fill": "#ffffff"}
        )
        moved = render(
            {
                "transform": {"translate": [60, 0]},
                "element": {
                    "type": "rect",
                    "x": 20,
                    "y": 20,
                    "width": 30,
                    "height": 30,
                    "fill": "#ffffff",
                },
            }
        )
        assert plain[30, 30].tolist() == [255, 255, 255]
        assert moved[30, 30].tolist() == [0, 0, 0]
        assert moved[30, 90].tolist() == [255, 255, 255]

    def test_a_group_shares_its_transform(self):
        image = render(
            {
                "element": {
                    "type": "group",
                    "children": [
                        {
                            "element": {
                                "type": "rect",
                                "x": 10,
                                "y": 10,
                                "width": 20,
                                "height": 20,
                                "fill": "#ffffff",
                            }
                        },
                        {
                            "element": {
                                "type": "rect",
                                "x": 40,
                                "y": 10,
                                "width": 20,
                                "height": 20,
                                "fill": "#ffffff",
                            }
                        },
                    ],
                }
            }
        )
        assert image[20, 20].tolist() == [255, 255, 255]
        assert image[20, 50].tolist() == [255, 255, 255]


class TestTransformedContentIsCompositedByCoverage:
    """A transformed layer is drawn into a scratch canvas and resampled. The
    scratch used to be black and "was anything drawn here" was `rgb.sum() !=
    0` — so black content vanished entirely, and every anti-aliased edge had
    been premultiplied against black, leaving a dark fringe."""

    def test_a_black_shape_under_a_transform_survives(self):
        image = render(
            {
                "transform": {"translate": [40, 0]},
                "element": {
                    "type": "rect",
                    "x": 20,
                    "y": 20,
                    "width": 40,
                    "height": 40,
                    "fill": "#000000",
                },
            },
            background="#ffffff",
        )

        # The rect lands at x 60..100. Its interior must be black, not the
        # white background it disappeared into.
        assert image[40, 80].tolist() == [0, 0, 0]
        assert image[40, 10].tolist() == [255, 255, 255]

    def test_a_transformed_edge_has_no_dark_fringe(self):
        """On a grey background, a white shape's anti-aliased rim must sit
        between the background and the fill — never below the background."""
        image = render(
            {
                "transform": {"rotate": 0.3},
                "element": {
                    "type": "circle",
                    "cx": 100,
                    "cy": 75,
                    "radius": 30,
                    "fill": "#ffffff",
                },
            },
            background="#808080",
        )

        background = image[0, 0, 0]
        assert image.min() >= background - 1
        assert image.max() == 255

    def test_a_transformed_shape_still_lands_where_it_should(self):
        moved = render(
            {
                "transform": {"translate": [60, 0]},
                "element": {
                    "type": "rect",
                    "x": 20,
                    "y": 20,
                    "width": 30,
                    "height": 30,
                    "fill": "#ffffff",
                },
            }
        )
        assert moved[30, 90].tolist() == [255, 255, 255]
        assert moved[30, 30].tolist() == [0, 0, 0]


class TestGroupTransforms:
    def group(self, **layer):
        return {
            **layer,
            "element": {
                "type": "group",
                "children": [
                    {
                        "element": {
                            "type": "rect",
                            "x": 10,
                            "y": 10,
                            "width": 20,
                            "height": 20,
                            "fill": "#ffffff",
                        }
                    },
                    {
                        "element": {
                            "type": "rect",
                            "x": 40,
                            "y": 10,
                            "width": 20,
                            "height": 20,
                            "fill": "#ffffff",
                        }
                    },
                ],
            },
        }

    def test_a_group_transform_moves_the_whole_group(self):
        """The group branch returned before the transform was even looked up,
        so a transform on a group was silently ignored — the two images were
        pixel-identical."""
        plain = render(self.group())
        moved = render(self.group(transform={"translate": [0, 60]}))

        assert not np.array_equal(plain, moved)
        assert moved[80, 20].tolist() == [255, 255, 255]
        assert moved[80, 50].tolist() == [255, 255, 255]
        assert moved[20, 20].tolist() == [0, 0, 0]

    def test_an_untransformed_group_is_unchanged(self):
        image = render(self.group())
        assert image[20, 20].tolist() == [255, 255, 255]
        assert image[20, 50].tolist() == [255, 255, 255]

    def test_a_group_scales_about_its_origin(self):
        image = render(self.group(transform={"scale": 2, "origin": [10, 10]}))
        # The first child's box 10..30 becomes 10..50 about (10, 10).
        assert image[45, 45].tolist() == [255, 255, 255]


class TestLayerOpacityReplaces:
    """Canvas semantics: `ctx.globalAlpha = layer.opacity` REPLACES the
    inherited alpha inside a save/restore, so the innermost declared opacity
    wins. alhazen multiplied them, which made a 0.5 layer inside a 0.5 group
    draw at 0.25."""

    def test_a_childs_opacity_replaces_its_parents(self):
        image = render(
            {
                "opacity": 0.5,
                "element": {
                    "type": "group",
                    "children": [
                        {
                            "opacity": 1.0,
                            "element": {
                                "type": "rect",
                                "x": 0,
                                "y": 0,
                                "width": 200,
                                "height": 150,
                                "fill": "#ffffff",
                            },
                        }
                    ],
                },
            }
        )

        assert image[75, 100, 0] == 255

    def test_a_child_without_an_opacity_inherits_its_parents(self):
        image = render(
            {
                "opacity": 0.5,
                "element": {
                    "type": "group",
                    "children": [
                        {
                            "element": {
                                "type": "rect",
                                "x": 0,
                                "y": 0,
                                "width": 200,
                                "height": 150,
                                "fill": "#ffffff",
                            }
                        }
                    ],
                },
            }
        )

        assert image[75, 100, 0] == pytest.approx(128, abs=2)


class TestExpressionsInScenes:
    def test_a_field_can_be_an_expression_of_time(self):
        element = {
            "type": "circle",
            "cx": {"expr": "50 + 40*time"},
            "cy": 75,
            "radius": 10,
            "fill": "#ffffff",
        }
        at_zero = render(element, time=0.0)
        at_one = render(element, time=1.0)
        assert at_zero[75, 50].tolist() == [255, 255, 255]
        assert at_one[75, 90].tolist() == [255, 255, 255]
        assert at_one[75, 50].tolist() == [0, 0, 0]

    def test_params_reach_the_scene(self):
        element = {
            "type": "circle",
            "cx": {"expr": "params.x"},
            "cy": 75,
            "radius": 8,
            "fill": "#ffffff",
        }
        image = render(element, params={"x": 150})
        assert image[75, 150].tolist() == [255, 255, 255]

    def test_a_canvas_size_is_required(self):
        with pytest.raises(ConfigError, match="canvas size"):
            headless_render(scene_of({"type": "rect", "x": 0}), width=None, height=None)


class TestSceneStimulus:
    def test_it_records_every_frame_on_a_simulated_display(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = scene_of(
            {
                "type": "circle",
                "cx": {"expr": "20 + 30*time"},
                "cy": 20,
                "radius": 5,
                "fill": "#fff",
            }
        )
        stimulus = SceneStimulus(display, SCREEN, scene, width=80, height=40)
        for _ in range(3):
            stimulus.update(1 / 60)
            stimulus.draw()
        assert len(stimulus.frames) == 3
        # It moved: scene time advanced with the trial's dt, not a wall clock.
        assert not np.array_equal(stimulus.frames[0], stimulus.frames[2])

    def test_a_scene_declares_its_own_size_in_the_formats_own_fields(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {"version": 1, "background": "#000", "width": 400, "height": 300, "layers": []}
        )

        stimulus = SceneStimulus(display, SCREEN, scene)

        assert (stimulus._width, stimulus._height) == (400, 300)

    def test_a_legacy_canvas_block_still_works(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {
                "version": 1,
                "background": "#000",
                "canvas": {"width": 320, "height": 240},
                "layers": [],
            }
        )

        stimulus = SceneStimulus(display, SCREEN, scene)

        assert (stimulus._width, stimulus._height) == (320, 240)

    def test_the_scene_is_letterboxed_onto_the_screen(self):
        """Spec 7.3: one scale factor for both axes, so a 4:3 scene on a 16:9
        screen gets bars at the sides rather than being stretched into a
        stimulus whose spatial frequency differs by axis."""
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {"version": 1, "background": "#000", "width": 400, "height": 300, "layers": []}
        )

        stimulus = SceneStimulus(display, SCREEN, scene)

        # SCREEN is 1920x1080; 1080/300 = 3.6 is the binding constraint.
        assert stimulus.scale == pytest.approx(3.6)
        assert stimulus.size_px == pytest.approx((1440.0, 1080.0))

    def test_the_scale_factor_is_recorded_not_implicit(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene({"version": 1, "background": "#000", "layers": []})

        # A scene with no declared size is rendered at the screen's own size,
        # so its letterbox factor is exactly 1.
        assert SceneStimulus(display, SCREEN, scene).scale == pytest.approx(1.0)

    def test_an_explicit_scale_is_honoured(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {"version": 1, "background": "#000", "width": 400, "height": 300, "layers": []}
        )

        assert SceneStimulus(display, SCREEN, scene, scale=2.0).scale == 2.0

    def test_scene_coordinates_map_into_centered_y_up_screen_space(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {"version": 1, "background": "#000", "width": 400, "height": 300, "layers": []}
        )
        stimulus = SceneStimulus(display, SCREEN, scene)

        # The scene's centre is the screen's centre.
        assert stimulus.scene_to_screen(200, 150) == pytest.approx((0.0, 0.0))
        # y is DOWN in a scene and UP on screen, so the scene's top edge is
        # at positive screen y.
        assert stimulus.scene_to_screen(200, 0)[1] == pytest.approx(540.0)
        assert stimulus.scene_to_screen(200, 300)[1] == pytest.approx(-540.0)
        # x grows the same way in both, scaled by the letterbox factor.
        assert stimulus.scene_to_screen(400, 150)[0] == pytest.approx(720.0)

    def test_the_mapping_follows_an_explicit_position(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        scene = load_scene(
            {"version": 1, "background": "#000", "width": 400, "height": 300, "layers": []}
        )
        stimulus = SceneStimulus(display, SCREEN, scene, scale=1.0, pos=(100.0, -50.0))

        assert stimulus.scene_to_screen(200, 150) == pytest.approx((100.0, -50.0))

    def test_scene_time_comes_from_the_trials_dt(self):
        display = FakeDisplay(FakeClock(), 1 / 60)
        stimulus = SceneStimulus(
            display,
            SCREEN,
            scene_of({"type": "rect", "x": 0, "y": 0, "width": 5, "height": 5}),
            width=20,
            height=20,
        )
        stimulus.update(0.5)
        stimulus.update(0.25)
        assert stimulus.time == pytest.approx(0.75)
