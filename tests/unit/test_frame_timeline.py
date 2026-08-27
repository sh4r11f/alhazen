"""FrameTimeline: a stimulus schedule that means frames, not milliseconds."""

from __future__ import annotations

import pytest

from alhazen.display.frames import FrameTimeline


class TestBuilding:
    def test_a_timeline_needs_at_least_one_frame(self):
        with pytest.raises(ValueError, match="at least one frame"):
            FrameTimeline(0)

    def test_frames_outside_the_timeline_are_rejected(self):
        # Silently clamping would move a stimulus somewhere the task did not
        # ask for, and nothing downstream could tell.
        with pytest.raises(ValueError, match="outside this timeline"):
            FrameTimeline(5).at(9, "dot", "pos", (0.0, 0.0))

    def test_a_ramp_must_end_after_it_starts(self):
        with pytest.raises(ValueError, match="must end after"):
            FrameTimeline(10).ramp("dot", "pos", 0.0, 1.0, 5, 5)

    def test_building_reads_as_a_sequence_of_statements(self):
        timeline = FrameTimeline(4).at(0, "dot", "opacity", 1.0).event(1, "STIM_ON")
        assert isinstance(timeline, FrameTimeline)


class TestPlayback:
    def timeline(self) -> FrameTimeline:
        return (
            FrameTimeline(10)
            .at(0, "dot", "opacity", 1.0)
            .show("dot", 2, 8)
            .ramp("dot", "pos", (0.0, 0.0), (90.0, 0.0), 2, 8)
            .event(2, "STIM_ON")
            .event(7, "STIM_OFF")
        )

    def test_settings_hold_from_their_frame_onwards(self):
        timeline = self.timeline()
        assert ("dot", "opacity", 1.0) in timeline.settings_at(0)
        assert ("dot", "opacity", 1.0) in timeline.settings_at(9)

    def test_a_ramp_interpolates_between_its_endpoints(self):
        timeline = self.timeline()
        assert dict(_pos(timeline, 2)) == {"dot": (0.0, 0.0)}
        assert dict(_pos(timeline, 5))["dot"][0] == pytest.approx(45.0)
        assert dict(_pos(timeline, 8))["dot"] == (90.0, 0.0)

    def test_a_ramp_holds_its_end_value_afterwards(self):
        # Not extrapolated past its span: a stimulus that kept moving after
        # its ramp ended would leave the screen without anything saying so.
        assert dict(_pos(self.timeline(), 9))["dot"] == (90.0, 0.0)

    def test_visibility_spans_are_half_open(self):
        timeline = self.timeline()
        assert timeline.visible_at(1) == []
        assert timeline.visible_at(2) == ["dot"]
        assert timeline.visible_at(7) == ["dot"]
        assert timeline.visible_at(8) == []

    def test_events_land_on_their_own_frame_only(self):
        timeline = self.timeline()
        assert timeline.events_at(2) == ["STIM_ON"]
        assert timeline.events_at(3) == []
        assert timeline.events_at(7) == ["STIM_OFF"]

    def test_playback_is_deterministic(self):
        first = [self.timeline().settings_at(frame) for frame in range(10)]
        second = [self.timeline().settings_at(frame) for frame in range(10)]
        assert first == second

    def test_two_stimuli_keep_their_declaration_order(self):
        timeline = FrameTimeline(4).show("frame", 0).show("probe", 0)
        assert timeline.visible_at(1) == ["frame", "probe"]

    def test_scalar_ramps_interpolate_too(self):
        timeline = FrameTimeline(5).ramp("dot", "opacity", 0.0, 1.0, 0, 4)
        settings = {attr: value for _, attr, value in timeline.settings_at(2)}
        assert settings["opacity"] == pytest.approx(0.5)


def _pos(timeline: FrameTimeline, frame: int):
    return [(key, value) for key, attr, value in timeline.settings_at(frame) if attr == "pos"]
