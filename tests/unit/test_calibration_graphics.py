"""EyeLink calibration graphics: the parts that do real work, tested without
pylink, psychopy, or a tracker.

The pylink subclass itself is a thin adapter built inside a factory (it needs
the SDK to exist at all); everything it delegates to lives here."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from alhazen.devices.eyetracker.calibration import (
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    CameraImageBuilder,
    draw_line_into,
    draw_lozenge_into,
    overlay_color,
    resolve_key,
)

# Stands in for the pylink module: the constants these helpers read, with
# arbitrary-but-distinct values, exactly as the real SDK exposes them.
PYLINK = SimpleNamespace(
    F1_KEY=0x3B00,
    PAGE_UP=0x4900,
    PAGE_DOWN=0x5100,
    CURS_UP=0x4800,
    CURS_DOWN=0x5000,
    CURS_LEFT=0x4B00,
    CURS_RIGHT=0x4D00,
    ENTER_KEY=13,
    JUNK_KEY=1,
    CR_HAIR_COLOR=1,
    PUPIL_HAIR_COLOR=2,
    PUPIL_BOX_COLOR=3,
    SEARCH_LIMIT_BOX_COLOR=4,
    MOUSE_CURSOR_COLOR=5,
)

NO_MODIFIERS: dict[str, bool] = {}


class TestKeyTranslation:
    def test_named_keys_use_the_trackers_own_codes(self):
        assert resolve_key("up", NO_MODIFIERS, PYLINK) == (PYLINK.CURS_UP, 0)
        assert resolve_key("pagedown", NO_MODIFIERS, PYLINK) == (PYLINK.PAGE_DOWN, 0)
        assert resolve_key("return", NO_MODIFIERS, PYLINK) == (PYLINK.ENTER_KEY, 0)

    def test_single_characters_pass_through_as_themselves(self):
        assert resolve_key("c", NO_MODIFIERS, PYLINK) == (ord("c"), 0)
        assert resolve_key("v", NO_MODIFIERS, PYLINK) == (ord("v"), 0)
        assert resolve_key("5", NO_MODIFIERS, PYLINK) == (ord("5"), 0)

    def test_multi_character_names_that_are_really_one_character(self):
        assert resolve_key("space", NO_MODIFIERS, PYLINK) == (ord(" "), 0)
        assert resolve_key("escape", NO_MODIFIERS, PYLINK) == (27, 0)
        # The tracker adjusts its pupil threshold with '+' and '-', which
        # arrive under several names depending on keyboard and layout.
        assert resolve_key("equal", NO_MODIFIERS, PYLINK) == (ord("+"), 0)
        assert resolve_key("num_add", NO_MODIFIERS, PYLINK) == (ord("+"), 0)
        assert resolve_key("minus", NO_MODIFIERS, PYLINK) == (ord("-"), 0)

    def test_unknown_key_is_junk_not_dropped(self):
        # Dropped keys would make a mistyped key look like a frozen
        # calibration; JUNK_KEY tells the tracker "something, but not yours".
        assert resolve_key("f24", NO_MODIFIERS, PYLINK) == (PYLINK.JUNK_KEY, 0)

    def test_modifiers_map_to_the_expected_bits(self):
        assert resolve_key("c", {"ctrl": True}, PYLINK) == (ord("c"), MOD_CTRL)
        assert resolve_key("c", {"alt": True}, PYLINK) == (ord("c"), MOD_ALT)
        assert resolve_key("c", {"shift": True}, PYLINK) == (ord("c"), MOD_SHIFT)
        assert resolve_key("c", {"shift": False}, PYLINK) == (ord("c"), 0)


class TestOverlayColors:
    def test_each_index_has_its_own_color(self):
        assert overlay_color(PYLINK.PUPIL_BOX_COLOR, PYLINK) == (0, 255, 0)
        assert overlay_color(PYLINK.SEARCH_LIMIT_BOX_COLOR, PYLINK) == (255, 0, 0)

    def test_unknown_index_still_draws(self):
        # A colour we do not recognize must still mark the image: an invisible
        # overlay is worse than a wrongly-coloured one.
        assert overlay_color(99, PYLINK) == (255, 255, 255)


class TestCameraImageBuilder:
    def build(self) -> CameraImageBuilder:
        builder = CameraImageBuilder()
        # A three-entry palette: black, red, green.
        builder.set_palette([0, 255, 0], [0, 0, 255], [0, 0, 0])
        return builder

    def test_image_arrives_only_on_the_last_line(self):
        builder = self.build()
        assert builder.add_line(2, 1, 3, [0, 1]) is None
        assert builder.add_line(2, 2, 3, [1, 2]) is None
        image = builder.add_line(2, 3, 3, [2, 0])
        assert image is not None
        assert image.shape == (3, 2, 3)

    def test_palette_indices_become_rgb_rows_top_down(self):
        builder = self.build()
        builder.add_line(2, 1, 2, [1, 1])
        image = builder.add_line(2, 2, 2, [2, 2])
        # Row 0 is the top of the image and stays the top: an accidentally
        # flipped camera view has the operator adjusting the wrong end of the
        # eye.
        assert list(image[0, 0]) == [255, 0, 0]
        assert list(image[1, 0]) == [0, 255, 0]

    def test_line_wider_than_the_buffer_uses_what_arrived(self):
        builder = self.build()
        image = builder.add_line(2, 1, 1, [1, 2, 0, 0])
        assert image.shape == (1, 2, 3)

    def test_out_of_range_index_is_clipped_not_raised(self):
        # The Host can send an index past a short palette right after a mode
        # change; aborting calibration over one bad pixel row would be worse.
        builder = self.build()
        image = builder.add_line(1, 1, 1, [99])
        assert list(image[0, 0]) == [0, 255, 0]  # clamped to the last palette entry

    def test_a_line_without_a_palette_is_a_loud_error(self):
        with pytest.raises(RuntimeError, match="palette"):
            CameraImageBuilder().add_line(1, 1, 1, [0])

    def test_consecutive_frames_do_not_bleed_into_each_other(self):
        builder = self.build()
        builder.add_line(1, 1, 1, [1])
        second = builder.add_line(1, 1, 1, [2])
        assert second.shape == (1, 1, 3)
        assert list(second[0, 0]) == [0, 255, 0]


class TestOverlayDrawing:
    def blank(self, height=20, width=30) -> np.ndarray:
        return np.zeros((height, width, 3), dtype=np.uint8)

    def test_horizontal_line_is_continuous(self):
        image = self.blank()
        draw_line_into(image, 2, 5, 10, 5, (255, 255, 255))
        assert (image[5, 2:11] == 255).all()
        assert (image[6] == 0).all()

    def test_diagonal_line_has_no_gaps(self):
        image = self.blank()
        draw_line_into(image, 0, 0, 9, 9, (255, 255, 255))
        ys, xs = np.where(image[:, :, 0] == 255)
        marked = {(int(y), int(x)) for y, x in zip(ys, xs, strict=True)}
        assert all((i, i) in marked for i in range(10))

    def test_coordinates_outside_the_image_are_clipped(self):
        # The tracker's search box can extend past the image it sent.
        image = self.blank()
        draw_line_into(image, -50, 5, 500, 5, (255, 255, 255))
        assert (image[5] == 255).all()

    def test_lozenge_marks_its_own_outline_and_leaves_the_middle(self):
        image = self.blank(height=40, width=40)
        draw_lozenge_into(image, 5, 10, 30, 12, (255, 0, 0))
        assert image[10, 20, 0] == 255  # flat top edge
        assert image[22, 20, 0] == 255  # flat bottom edge
        assert image[16, 20, 0] == 0  # hollow inside
        assert (image[:, :, 0] == 255).sum() > 40  # arcs drew too, not just the lines

    def test_tall_lozenge_uses_its_other_axis(self):
        image = self.blank(height=40, width=40)
        draw_lozenge_into(image, 10, 5, 12, 30, (255, 0, 0))
        assert image[20, 10, 0] == 255  # straight left edge
        assert image[20, 22, 0] == 255  # straight right edge
        assert image[20, 16, 0] == 0

    def test_empty_lozenge_draws_nothing(self):
        image = self.blank()
        draw_lozenge_into(image, 5, 5, 0, 10, (255, 0, 0))
        assert (image == 0).all()
