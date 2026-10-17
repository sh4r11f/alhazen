"""The mouse stand-in walks the rig's calibration target, as the real trackers do.

User decision (2026-10-15, round 5): ``mouse_sim`` accepts
``eyetracker.calibration_target`` (standard, chosen pictures, random pictures;
still or pulsating; size and pulse) and uses it for calibration and validation
exactly like the real trackers: the same presenter and picture deck
(calibration_targets.py), the same layout positions as the TRACKPixx3's walk,
the same keys and advance modes, the same live-monitor stages, and events that
record ``target_style`` and ``targets_shown``. These mirror the TRACKPixx3 walk
tests in test_calibration_targets.py, with psychopy stood in for (keys pressed
at set session times, a recording visual). A software contract; the pixel
comparison with a real PsychoPy window is the round-5 render check.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from test_calibration_targets import (  # noqa: F401
    KEYS,
    PULSE,
    SCREEN,
    WalkDisplay,
    named_pictures,
    walk,
    walk_sdk,
)

from alhazen.config.loader import load_rig
from alhazen.config.models import EyeTrackerConfig, with_calibration_target
from alhazen.config.rigs import shared_rig_files
from alhazen.core.rng import named_stream
from alhazen.devices.eyetracker import make_tracker
from alhazen.devices.eyetracker.calibration_targets import pulse_scale
from alhazen.devices.eyetracker.mouse_sim import START_LINE, MouseSimTracker
from alhazen.devices.eyetracker.viewpixx import STATUS_REFRESH_S, calibration_targets
from alhazen.errors import ConfigError
from alhazen.session.eyetracker import EyeTrackerMonitor
from alhazen.testing import FakeClock


@pytest.fixture
def mouse_sdk(walk_sdk):  # noqa: F811
    """walk_sdk's psychopy (timed keys, recording visual), with a mouse."""
    event = sys.modules["psychopy.event"]
    event.Mouse = lambda win=None, visible=True: types.SimpleNamespace(
        setVisible=lambda v: None, getPos=lambda: (0.0, 0.0)
    )
    return walk_sdk


def mouse_cfg(advance: str = "manual", **target: Any) -> EyeTrackerConfig:
    return EyeTrackerConfig(
        backend="mouse_sim",
        calibration_type="HV5",
        calibration_advance=advance,
        calibration_target=target,
    )


def mouse_walk(sdk, *, hz=60.0, keys, rng=None, advance="manual", stages=None, **target):
    """The mouse's calibration, as walk() runs the TRACKPixx3's."""
    clock = FakeClock()
    display = WalkDisplay(clock, hz)
    sdk.state.clock, sdk.state.window = clock, display.window
    sdk.state.schedule = list(keys)
    tracker = MouseSimTracker(
        display, SCREEN, clock, mouse_cfg(advance, **target), calibration_rng=rng
    )
    tracker.configure(SCREEN, clock)
    if stages is not None:
        tracker.set_progress_hook(lambda stage, detail: stages.append((stage, detail)))
    return tracker.calibrate(), display, tracker


class TestTheConfig:
    def test_the_mouse_takes_a_calibration_target(self):
        target = {"appearance": "random_images", "motion": "pulse"}
        cfg = EyeTrackerConfig(backend="mouse_sim", calibration_target=target)
        assert cfg.calibration_target.appearance == "random_images"
        assert cfg.calibration_target.motion == "pulse"

    def test_the_scripted_replay_still_refuses_one(self):
        with pytest.raises(ValueError, match="ignores calibration_target"):
            EyeTrackerConfig(backend="scripted", calibration_target={"motion": "pulse"})

    def test_the_mouse_still_refuses_the_hardware_fields(self):
        fields = (("led_intensity", 3), ("host_ip", "1.2.3.4"), ("max_sample_gap_ms", 80))
        for field, value in fields:
            with pytest.raises(ValueError, match=f"ignores {field}"):
                EyeTrackerConfig(backend="mouse_sim", **{field: value})

    def test_a_layout_the_mouse_cannot_lay_out_is_refused_at_load(self):
        with pytest.raises(ValueError, match="for the mouse_sim backend"):
            EyeTrackerConfig(backend="mouse_sim", calibration_type="HV3")

    def test_the_run_flags_choose_the_mouses_target(self):
        rig = load_rig(shared_rig_files()["lab-rehearsal"])
        assert rig.devices.eyetracker.backend == "mouse_sim"
        chosen = with_calibration_target(rig, appearance="images", images=["tree_1"])
        assert chosen.devices.eyetracker.calibration_target.images == ("tree_1",)
        scripted = rig.model_copy(
            update={
                "devices": rig.devices.model_copy(
                    update={"eyetracker": EyeTrackerConfig(backend="scripted")}
                )
            }
        )
        with pytest.raises(ConfigError, match="the scripted stand-in"):
            with_calibration_target(scripted, motion="pulse")

    def test_make_tracker_hands_the_mouse_its_config(self, mouse_sdk):
        clock = FakeClock()
        display = WalkDisplay(clock, 60.0)
        cfg = mouse_cfg(motion="pulse")
        tracker = make_tracker(cfg, display, SCREEN, clock)
        assert isinstance(tracker, MouseSimTracker)
        assert tracker.calibration_targets.style.endswith("pulsating 1-1.4x at 1 Hz")


class TestTheWalk:
    def test_the_standard_target_at_the_trackpixx3s_positions(self, mouse_sdk):
        stages: list[tuple[str, str]] = []
        result, display, _ = mouse_walk(mouse_sdk, keys=KEYS, stages=stages)
        grid = calibration_targets("HV5", SCREEN, 0.6)
        assert [s.target_px for s in result.shown] == grid
        assert [s.ordinal for s in result.shown] == [1, 2, 3, 4, 5]
        assert result.ok is None and not result.aborted and result.n_targets == 5
        assert "nothing was fitted" in result.note
        assert result.target_style == "standard target, still"
        circles = [s for s in mouse_sdk.recorder.made if s.kind == "circle"]
        assert [c.kwargs["radius"] for c in circles] == [12.0, 4.0]
        assert [c.radius_sets for c in circles] == [0, 0]
        # One wait per refresh, never a poll: a still target is not redrawn.
        assert set(mouse_sdk.state.waits) == {STATUS_REFRESH_S}
        assert mouse_sdk.state.polls == 0
        # Nothing but the target is drawn; no text panel.
        assert {stim.kind for stim in mouse_sdk.recorder.made} == {"circle"}
        assert display.menus == [] and display.messages == []
        # The stages the live monitor gets, in the TRACKPixx3's own words.
        assert stages[0] == ("calibration guide", START_LINE)
        assert START_LINE == "press SPACE to start, P or ESC to go back to the pause menu"
        steps = [detail for stage, detail in stages if stage == "calibrating"]
        assert list(dict.fromkeys(steps)) == [f"target {i} of 5" for i in range(1, 6)]

    @pytest.mark.parametrize("hz", [60.0, 144.0])
    def test_a_pulsating_target_is_redrawn_each_frame_at_its_moment(self, mouse_sdk, hz):
        result, _, _ = mouse_walk(mouse_sdk, hz=hz, keys=KEYS, motion="pulse")
        onsets = {s.target_px: s.t for s in result.shown}
        outer = [d for d in mouse_sdk.recorder.draws if d[0] == "circle"][::2]
        assert len(outer) >= int(3.6 * hz)
        for _, pos, radius, _, t in outer:
            assert radius == pytest.approx(12.0 * pulse_scale(t - onsets[pos], PULSE))
        assert result.target_style.endswith("pulsating 1-1.4x at 1 Hz")

    def test_the_same_walk_as_the_trackpixx3_for_the_same_settings_and_seed(
        self,
        mouse_sdk,
        named_pictures,  # noqa: F811
    ):
        """Same keys at the same times, same seed: the same pictures, at the
        same positions, in the same order, accepted at the same moments,
        drawn at the same sizes — the TRACKPixx3's walk without a device."""
        target = {
            "appearance": "random_images",
            "images": ["monkey_1", "monkey_2", "food_1"],
            "motion": "pulse",
        }
        rig, _ = walk(mouse_sdk, keys=KEYS, rng=named_stream(7, "calibration_target"), **target)
        rig_draws = list(mouse_sdk.recorder.draws)
        mouse_sdk.recorder.draws.clear()
        mouse, _, _ = mouse_walk(
            mouse_sdk, keys=KEYS, rng=named_stream(7, "calibration_target"), **target
        )
        assert mouse.target_style == rig.target_style
        assert [(s.image, s.target_px, s.ordinal) for s in mouse.shown] == [
            (s.image, s.target_px, s.ordinal) for s in rig.shown
        ]
        for a, b in zip(rig.shown, mouse.shown, strict=True):
            assert abs(a.t - b.t) <= 1.0 / 60.0 + 1e-9
        # Frame by frame: the same picture, position and size at the same
        # moment on the subject's screen.
        assert mouse_sdk.recorder.draws == rig_draws

    def test_listed_pictures_go_round_in_order(self, mouse_sdk, named_pictures):  # noqa: F811
        result, _, _ = mouse_walk(
            mouse_sdk, keys=KEYS, appearance="images", images=["tree_1", "food_9"]
        )
        assert [s.image for s in result.shown] == ["tree_1", "food_9", "tree_1", "food_9", "tree_1"]

    def test_auto_advance_accepts_every_target_by_itself(self, mouse_sdk):
        result, _, _ = mouse_walk(
            mouse_sdk, keys=[(0.05, "space"), (30.0, "escape")], advance="auto"
        )
        assert not result.aborted and len(result.shown) == 5

    def test_escape_at_the_guide_aborts_with_nothing_drawn(self, mouse_sdk):
        result, display, _ = mouse_walk(mouse_sdk, keys=[(0.05, "escape")])
        assert result.aborted and result.shown == ()
        assert mouse_sdk.recorder.draws == []
        assert result.note == "aborted at the guide"

    @pytest.mark.parametrize(("key", "how"), [("escape", "aborted"), ("p", "stopped")])
    def test_a_stop_key_mid_walk_aborts_there(self, mouse_sdk, key, how):
        keys = [(0.05, "space"), (0.8, "space"), (1.5, key)]
        result, _, _ = mouse_walk(mouse_sdk, keys=keys)
        assert result.aborted and result.note.startswith(how) and "target 2 of 5" in result.note

    def test_backspace_goes_back_one_target(self, mouse_sdk):
        keys = [(0.05, "space"), (0.8, "space"), (1.5, "backspace")] + [
            (1.6 + 0.7 * i, "space") for i in range(5)
        ]
        result, _, _ = mouse_walk(mouse_sdk, keys=keys)
        grid = calibration_targets("HV5", SCREEN, 0.6)
        assert [s.target_px for s in result.shown] == [grid[0], grid[1], grid[0], *grid[1:]]

    def test_a_simulated_display_walks_nothing(self, mouse_sdk):
        clock = FakeClock()
        display = WalkDisplay(clock, 60.0)
        display.kind = "simulated"
        tracker = MouseSimTracker(display, SCREEN, clock, mouse_cfg(motion="pulse"))
        tracker.configure(SCREEN, clock)
        result = tracker.calibrate()
        assert result.shown == () and "simulated display" in result.note
        assert mouse_sdk.recorder.made == []


class TestTheSession:
    def test_the_events_record_the_target_and_validation_carries_the_deck_on(
        self,
        mouse_sdk,
        named_pictures,  # noqa: F811
    ):
        clock = FakeClock()
        display = WalkDisplay(clock, 60.0)
        display.flip = lambda clear=True: display.window.flip()
        mouse_sdk.state.clock, mouse_sdk.state.window = clock, display.window
        mouse_sdk.state.schedule = [(0.05, "space"), (60.0, "escape")]
        cfg = mouse_cfg("auto", appearance="images", images=["tree_1", "food_9", "monkey_7"])
        tracker = MouseSimTracker(display, SCREEN, clock, cfg)
        tracker.configure(SCREEN, clock)
        monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
        assert monitor.calibration_targets is tracker.calibration_targets
        events: list[tuple[str, dict]] = []
        monitor.emit = lambda name, payload: events.append((name, payload))
        monitor.calibrate()
        calibration = dict(events)["CALIBRATION"]
        validation = dict(events)["VALIDATION"]
        assert calibration["target_style"] == validation["target_style"]
        cal = [(s["image"], s["ordinal"]) for s in calibration["targets_shown"]]
        assert cal == [("tree_1", 1), ("food_9", 2), ("monkey_7", 3), ("tree_1", 4), ("food_9", 5)]
        val = validation["targets_shown"]
        assert [s["ordinal"] for s in val] == list(range(6, 6 + len(val)))
        assert val[0]["image"] == "monkey_7"  # the deck carries on


class TestThePageDrivesTheMousesWalk:
    def test_resume_and_pause_from_the_page(self, mouse_sdk):
        pending = ["resume", "resume", "pause"]

        def page(accepted):
            for index, name in enumerate(pending):
                if name in accepted:
                    return [pending.pop(index)]
            return []

        clock = FakeClock()
        display = WalkDisplay(clock, 60.0)
        mouse_sdk.state.clock, mouse_sdk.state.window = clock, display.window
        mouse_sdk.state.schedule = []
        tracker = MouseSimTracker(display, SCREEN, clock, mouse_cfg())
        tracker.configure(SCREEN, clock)
        tracker.set_command_source(page)
        result = tracker.calibrate()
        assert result.aborted and result.note == "stopped for the pause menu at target 2 of 5"
