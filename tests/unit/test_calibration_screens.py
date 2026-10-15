"""Calibration screens show only targets, and validation draws the calibration's target.

Two user decisions (2026-10-15), held on every tracker path with the SDKs and
PsychoPy stood in for (the fakes of test_calibration_targets):

- **Nothing but targets on the subject's screen during a calibration** — and
  during validation and drift correction, the same screen family: no guide
  panel, no status line, no failure message, no Host PC caption, no heading,
  no key hints. What the experimenter needs goes to the session log and the
  live monitor (the progress hook) instead. Checked for the TRACKPixx3 walk
  (alhazen draws everything), the EyeLink graphics (alhazen mirrors the Host
  PC's procedure), the mouse stand-in, and alhazen's own validation and
  drift correction on each.
- **Validation (and drift correction) draw the calibration's target**:
  whatever the rig picks (standard, a chosen picture set, random pictures;
  still or pulsating; the same size and pulse), through the same presenter,
  with the tracker's own picture deck carrying on — not the 0.5° white disc
  they drew before. Each procedure's event records the style and every
  target shown, as the CALIBRATION event does.

A software contract with fakes; nothing here is evidence about a real tracker.
The pixel-level comparison with a real PsychoPy window is the render check in
the round-4 report (calibration and validation frames of the same settings).
"""

from __future__ import annotations

import logging
import math
import sys
import types
from typing import Any

import pytest
from test_calibration_targets import (  # noqa: F401
    KEYS,
    PULSE,
    SCREEN,
    FakeWindow,
    Recorder,
    fake_visual,
    named_pictures,
    walk,
    walk_sdk,
)

from alhazen.config.models import CalibrationTargetConfig, EyeTrackerConfig
from alhazen.core.rng import named_stream
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.eyetracker.calibration import make_calibration_graphics
from alhazen.devices.eyetracker.calibration_targets import (
    CalibrationTargets,
    picture_size_px,
    pulse_scale,
)
from alhazen.devices.eyetracker.procedures import (
    ProcedureTiming,
    drift_correct,
    validate,
    validation_targets,
)
from alhazen.devices.eyetracker.protocol import GazeSample
from alhazen.devices.eyetracker.scripted import ScriptedTracker
from alhazen.session.eyetracker import EyeTrackerMonitor
from alhazen.testing import FakeClock, FakeDisplay

# Short, so a five-target walk is a few hundred frames.
FAST = ProcedureTiming(settle_s=0.1, sample_s=0.1, stable_deg=1.0, timeout_s=1.0)

# What may ever be made or drawn on the subject's screen during these
# procedures: the standard target's two circles, a picture, and (EyeLink,
# asked for by the experimenter on the setup screen) the camera image.
TARGET_KINDS = {"circle", "image"}


class RenderDisplay:
    """A rendering display (not "simulated"): the procedures draw the real
    presenter into its window. Records any text panel put up, which none of
    these procedures may do."""

    kind = "psychopy"

    def __init__(self, clock: FakeClock, hz: float = 60.0) -> None:
        self.window = FakeWindow(clock, hz)
        self.texts: list[str] = []

    def flip(self, clear: bool = True) -> None:
        self.window.flip()

    def show_message(self, text: str, *, reflow: bool = True) -> None:
        self.texts.append(text)

    def show_menu(self, title: str, body: str, *, color: Any) -> None:
        self.texts.append(f"{title}\n{body}")


class LooksAtTheTarget(ScriptedTracker):
    """A subject who looks at the newest target shown — read off the
    targets' own record, so the walk's measurement completes by itself."""

    def __init__(self, clock: FakeClock, targets: CalibrationTargets) -> None:
        super().__init__([], clock)
        self.calibration_targets = targets

    def get_gaze(self) -> GazeSample | None:
        shown = self.calibration_targets.shown_since_begin()
        x, y = shown[-1].target_px if shown else (0.0, 0.0)
        gx, gy = SCREEN.centered_to_screen(x, y)
        return GazeSample(gx=gx, gy=gy, t=self._clock.now())


@pytest.fixture
def render_psychopy(monkeypatch):
    """psychopy.visual (recording) and psychopy.event (a mouse) stood in for."""
    recorder = Recorder()
    visual = fake_visual(recorder)
    event = types.ModuleType("psychopy.event")
    event.Mouse = lambda win=None, visible=True: types.SimpleNamespace(
        setVisible=lambda v: None, getPos=lambda: (0.0, 0.0)
    )
    psychopy = types.ModuleType("psychopy")
    psychopy.visual, psychopy.event = visual, event
    for name, module in (
        ("psychopy", psychopy),
        ("psychopy.visual", visual),
        ("psychopy.event", event),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return recorder


def tracker_cfg(**target: Any) -> EyeTrackerConfig:
    """A viewpixx config carrying the target (the stand-ins refuse one)."""
    return EyeTrackerConfig(
        backend="viewpixx",
        calibration_type="HV5",
        calibration_advance="auto",
        calibration_target=target,
    )


def run_validation(recorder: Recorder, *, hz: float = 60.0, rng=None, **target: Any):
    clock = FakeClock()
    display = RenderDisplay(clock, hz)
    cfg = tracker_cfg(**target)
    targets = CalibrationTargets(cfg.calibration_target, rng)
    targets.prepare(SCREEN, cfg.calibration_area)
    tracker = LooksAtTheTarget(clock, targets)
    result = validate(
        tracker,
        display,
        SCREEN,
        clock,
        cfg,
        poll_keys=lambda: [],
        calibration_targets=targets,
        timing=FAST,
    )
    return result, display, targets


def no_text(recorder: Recorder) -> None:
    kinds_made = {stim.kind for stim in recorder.made}
    kinds_drawn = {draw[0] for draw in recorder.draws}
    assert kinds_made <= TARGET_KINDS, kinds_made
    assert kinds_drawn <= TARGET_KINDS, kinds_drawn


# ---------------------------------------------------------------------------
# Nothing but targets on the subject's screen
# ---------------------------------------------------------------------------

CONFIGS = [
    {},
    {"motion": "pulse"},
    {"appearance": "images", "images": ["food_1", "monkey_7"], "motion": "pulse"},
    {"appearance": "random_images", "images": ["monkey_1", "monkey_2", "monkey_3"]},
]


class TestNothingButTargetsOnTheSubjectsScreen:
    @pytest.mark.parametrize("target", CONFIGS)
    def test_the_trackpixx3_walk_draws_only_its_targets(
        self,
        walk_sdk,  # noqa: F811
        named_pictures,  # noqa: F811
        target,
    ):
        result, display = walk(
            walk_sdk, keys=KEYS, rng=named_stream(3, "calibration_target"), **target
        )
        assert result.ok
        no_text(walk_sdk.recorder)
        assert display.menus == [] and display.messages == []
        assert walk_sdk.recorder.draws  # the targets themselves were drawn

    def test_a_trackpixx3_calibration_that_did_not_take_says_so_off_the_screen(
        self,
        walk_sdk,  # noqa: F811
    ):
        walk_sdk.device.calibrated_after_finish = False
        result, display = walk(walk_sdk, keys=KEYS)
        assert result.ok is False
        no_text(walk_sdk.recorder)
        assert display.menus == [] and display.messages == []

    def test_a_trackpixx3_walk_aborted_at_the_wait_drew_nothing(self, walk_sdk):  # noqa: F811
        result, display = walk(walk_sdk, keys=[(0.05, "escape")])
        assert result.aborted
        assert walk_sdk.recorder.draws == []
        no_text(walk_sdk.recorder)
        assert display.menus == [] and display.messages == []

    def test_every_eyelink_callback_draws_only_a_target_or_the_camera_image(
        self, monkeypatch, caplog
    ):
        """The Host PC drives the procedure through these callbacks — targets
        for calibration, validation and its own drift check, the camera
        image on the setup screen, its caption. None of it is text."""
        recorder = Recorder()

        class CustomDisplay:  # the part of pylink's base class the adapter calls
            def draw_cross_hair(self) -> None:
                return None

        pylink = types.ModuleType("pylink")
        pylink.EyeLinkCustomDisplay = CustomDisplay
        pylink.KeyInput = lambda code, modifier: (code, modifier)
        pylink.JUNK_KEY = 1
        event = types.ModuleType("psychopy.event")
        event.getKeys = lambda modifiers=False: []
        event.Mouse = lambda win=None, visible=False: types.SimpleNamespace()
        visual = fake_visual(recorder)
        psychopy = types.ModuleType("psychopy")
        psychopy.event, psychopy.visual = event, visual
        for name, module in (
            ("pylink", pylink),
            ("psychopy", psychopy),
            ("psychopy.event", event),
            ("psychopy.visual", visual),
        ):
            monkeypatch.setitem(sys.modules, name, module)
        clock = FakeClock()
        for config in (CalibrationTargetConfig(), CalibrationTargetConfig(motion="pulse")):
            targets = CalibrationTargets(config)
            targets.prepare(SCREEN, 0.6)
            window = FakeWindow(clock)
            graphics = make_calibration_graphics(
                None, window, SCREEN, targets=targets, now=clock.now
            )
            with caplog.at_level(logging.INFO):
                graphics.setup_cal_display()
                graphics.draw_cal_target(960.0, 540.0)
                for _ in range(20):
                    graphics.get_input_key()
                graphics.erase_cal_target()
                graphics.setup_image_display(4, 2)
                graphics.image_title("Left eye  thresholds 70 210")
                graphics.image_title("Left eye  thresholds 70 210")  # unchanged: logged once
                graphics.set_image_palette([0, 255], [0, 255], [0, 255])
                graphics.draw_image_line(4, 1, 2, [0, 1, 0, 1])
                graphics.draw_image_line(4, 2, 2, [1, 0, 1, 0])
                graphics.exit_image_display()
                graphics.clear_cal_display()
                graphics.exit_cal_display()
        no_text(recorder)
        assert {draw[0] for draw in recorder.draws} == TARGET_KINDS
        captions = [
            r.getMessage() for r in caplog.records if "EyeLink camera image" in r.getMessage()
        ]
        assert captions == ["EyeLink camera image: Left eye  thresholds 70 210"] * 2

    @pytest.mark.parametrize("target", CONFIGS)
    def test_validation_draws_only_its_targets(self, render_psychopy, named_pictures, target):  # noqa: F811
        result, display, _ = run_validation(
            render_psychopy, rng=named_stream(3, "calibration_target"), **target
        )
        assert len(result.targets) == 5
        no_text(render_psychopy)
        assert display.texts == []

    def test_drift_correction_draws_only_its_target(self, render_psychopy):
        clock = FakeClock()
        display = RenderDisplay(clock)
        cfg = tracker_cfg(motion="pulse")
        targets = CalibrationTargets(cfg.calibration_target)
        tracker = LooksAtTheTarget(clock, targets)
        from alhazen.devices.eyetracker.procedures import GazeCorrection

        result = drift_correct(
            tracker,
            display,
            SCREEN,
            clock,
            cfg,
            GazeCorrection(),
            poll_keys=lambda: [],
            calibration_targets=targets,
            timing=FAST,
        )
        assert result.applied
        no_text(render_psychopy)
        assert display.texts == []
        assert {draw[1] for draw in render_psychopy.draws} == {(0.0, 0.0)}

    def test_the_mouse_stand_in_calibrates_drawing_nothing_and_validates_with_targets_only(
        self, render_psychopy
    ):
        """The mouse has no calibration to run (it draws nothing at all); the
        validation that follows it draws the calibration's standard target and
        nothing else — the mouse is where the gaze is, so it measures."""
        from alhazen.devices.eyetracker.mouse_sim import MouseSimTracker

        clock = FakeClock()
        display = RenderDisplay(clock)
        tracker = MouseSimTracker(display, SCREEN, clock)
        cfg = EyeTrackerConfig(backend="mouse_sim", calibration_advance="auto")
        monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
        result = monitor.calibrate()  # validates afterwards (validate_after_calibration)
        assert result.ok is None and monitor.validation is not None
        no_text(render_psychopy)
        assert display.texts == []
        kinds = [stim.kind for stim in render_psychopy.made]
        assert kinds == ["circle", "circle"]  # the standard target's disc and hole
        assert monitor.validation.target_style == "standard target, still"


# ---------------------------------------------------------------------------
# Validation draws the calibration's target
# ---------------------------------------------------------------------------


class TestValidationDrawsTheCalibrationTarget:
    def test_the_standard_still_target_not_the_white_disc(self, render_psychopy):
        result, display, _ = run_validation(render_psychopy)
        outer, inner = render_psychopy.made
        assert (outer.kwargs["radius"], inner.kwargs["radius"]) == (12.0, 4.0)
        assert outer.kwargs["fillColor"] == (-1.0, -1.0, -1.0)
        assert inner.kwargs["fillColor"] == display.window.color  # the hole
        assert outer.radius_sets == 0 and inner.radius_sets == 0  # never resized
        drawn = [draw[1] for draw in render_psychopy.draws]
        grid = validation_targets(tracker_cfg(), SCREEN)
        assert list(dict.fromkeys(drawn)) == grid  # each validation point, in order
        assert result.target_style == "standard target, still"
        assert [s.target_px for s in result.shown] == grid

    @pytest.mark.parametrize("hz", [60.0, 144.0])
    def test_a_pulsating_target_pulses_as_it_does_in_calibration(self, render_psychopy, hz):
        result, _, _ = run_validation(render_psychopy, hz=hz, motion="pulse")
        onsets = {s.target_px: s.t for s in result.shown}
        outer = [d for d in render_psychopy.draws if d[0] == "circle"][::2]
        assert len(outer) >= 5 * FAST.settle_s * hz  # redrawn every frame
        for _, pos, radius, _, t in outer:
            assert radius == pytest.approx(12.0 * pulse_scale(t - onsets[pos], PULSE))
        assert max(r for _, _, r, _, _ in outer) <= 12.0 * PULSE.max_scale + 1e-9
        assert result.target_style.endswith("pulsating 1-1.4x at 1 Hz")

    def test_a_picture_has_its_calibration_size_aspect_and_pulse(
        self,
        render_psychopy,
        named_pictures,  # noqa: F811
    ):
        config = {
            "appearance": "images",
            "images": ["monkey_7"],
            "motion": "pulse",
            "image_size_dva": 3.0,
        }
        result, _, _ = run_validation(render_psychopy, **config)
        still = picture_size_px("monkey_7", tracker_cfg(**config).calibration_target, SCREEN)
        onsets = {s.target_px: s.t for s in result.shown}
        drawn = [d for d in render_psychopy.draws if d[0] == "image"]
        assert drawn
        for _, pos, size, image, t in drawn:
            scale = pulse_scale(t - onsets[pos], PULSE)
            assert image == "monkey_7"
            assert size == pytest.approx((still[0] * scale, still[1] * scale))

    def test_chosen_pictures_go_in_the_listed_order_round_and_round(
        self,
        render_psychopy,
        named_pictures,  # noqa: F811
    ):
        result, _, targets = run_validation(
            render_psychopy, appearance="images", images=["food_1", "food_2", "food_3"]
        )
        assert [s.image for s in result.shown] == ["food_1", "food_2", "food_3", "food_1", "food_2"]

    def test_random_pictures_follow_the_seed_and_never_repeat(
        self,
        render_psychopy,
        named_pictures,  # noqa: F811
    ):
        pool = ["monkey_1", "monkey_2", "monkey_3"]
        first, _, _ = run_validation(
            render_psychopy,
            rng=named_stream(7, "calibration_target"),
            appearance="random_images",
            images=pool,
        )
        second, _, _ = run_validation(
            render_psychopy,
            rng=named_stream(7, "calibration_target"),
            appearance="random_images",
            images=pool,
        )
        images = [s.image for s in first.shown]
        assert images == [s.image for s in second.shown]
        assert set(images) <= set(pool)
        assert all(a != b for a, b in zip(images, images[1:], strict=False))
        assert "order from the session seed" in first.target_style

    def test_drift_correction_measured_again_keeps_its_picture(
        self,
        render_psychopy,
        named_pictures,  # noqa: F811
    ):
        """BACKSPACE during a drift correction measures the same centre target
        again: the same target, so the same picture and an unbroken pulse —
        the calibration's rule for a target that stays up."""
        clock = FakeClock()
        display = RenderDisplay(clock)
        cfg = tracker_cfg(appearance="images", images=["tree_1", "food_9"], motion="pulse")
        targets = CalibrationTargets(cfg.calibration_target)
        tracker = LooksAtTheTarget(clock, targets)
        presses = iter([[], [], ["backspace"]])
        from alhazen.devices.eyetracker.procedures import GazeCorrection

        result = drift_correct(
            tracker,
            display,
            SCREEN,
            clock,
            cfg,
            GazeCorrection(),
            poll_keys=lambda: next(presses, []),
            calibration_targets=targets,
            timing=FAST,
        )
        assert result.applied
        assert [(s.image, s.target_px) for s in result.shown] == [("tree_1", (0.0, 0.0))]
        assert {d[3] for d in render_psychopy.draws} == {"tree_1"}
        assert result.payload()["targets_shown"][0]["image"] == "tree_1"

    def test_the_trackpixx3_validation_carries_on_the_calibrations_deck(
        self,
        walk_sdk,  # noqa: F811
        named_pictures,  # noqa: F811
    ):
        """One tracker, one deck: the validation after a TRACKPixx3 calibration
        draws through the tracker's own targets (the monitor takes them from
        the tracker), so the pictures go on in the calibration's order and its
        record continues."""
        pictures = ["food_1", "food_2", "food_3"]
        clock = FakeClock()
        from test_calibration_targets import WalkDisplay

        class Display(WalkDisplay):
            def flip(self, clear: bool = True) -> None:
                self.window.flip()

        display = Display(clock, 60.0)
        walk_sdk.state.clock, walk_sdk.state.window = clock, display.window
        walk_sdk.state.schedule = list(KEYS)
        cfg = EyeTrackerConfig(
            backend="viewpixx",
            calibration_type="HV5",
            calibration_target={"appearance": "images", "images": pictures},
            validate_after_calibration=False,
        )
        tracker = ViewPixxTracker(cfg, display, SCREEN, clock, background_gaze=False)
        tracker.connect()
        tracker.configure(SCREEN, clock)
        monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
        assert monitor.calibration_targets is tracker.calibration_targets
        calibration = monitor.calibrate()
        assert [s.image for s in calibration.shown] == pictures + pictures[:2]
        validation = monitor.validate(advance="auto")
        assert [s.image for s in validation.shown][:3] == ["food_3", "food_1", "food_2"]
        assert [s.ordinal for s in validation.shown][0] == 6  # the record continues
        payload = validation.payload()
        assert payload["target_style"] == calibration.target_style
        assert [t["image"] for t in payload["targets_shown"]][:1] == ["food_3"]
        no_text(walk_sdk.recorder)


class TestEachTrackerPathTakesItsTarget:
    def test_the_eyelink_and_trackpixx3_hand_over_their_own(self, monkeypatch):
        from alhazen.devices.eyetracker import EyeLinkTracker

        clock = FakeClock()
        display = RenderDisplay(clock)
        cfg = EyeTrackerConfig(backend="eyelink", calibration_target={"motion": "pulse"})
        tracker = EyeLinkTracker(cfg, display, SCREEN, clock)
        monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
        assert monitor.calibration_targets is tracker.calibration_targets
        assert monitor.calibration_targets.config.motion == "pulse"

    def test_a_stand_in_gets_the_rigs_choice_kept_for_the_session(self):
        clock = FakeClock()
        display = FakeDisplay(clock)
        cfg = EyeTrackerConfig(backend="scripted")
        monitor = EyeTrackerMonitor(
            ScriptedTracker([], clock), display, SCREEN, clock, cfg, poll_keys=lambda: []
        )
        first = monitor.calibration_targets
        assert first.config == CalibrationTargetConfig()  # standard, still
        monitor.drift_correct()
        assert monitor.calibration_targets is first

    def test_a_simulated_display_draws_and_records_nothing(self):
        """Nothing is drawn on a simulated display, so nothing is recorded:
        its VALIDATION and DRIFT_CORRECTION events are what they always were."""
        clock = FakeClock()
        display = FakeDisplay(clock)
        cfg = EyeTrackerConfig(backend="scripted", calibration_advance="auto")
        tracker = ScriptedTracker([], clock)
        monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
        validation = monitor.validate()
        drift = monitor.drift_correct()
        for result in (validation, drift):
            assert result.target_style == "" and result.shown == ()
            assert "target_style" not in result.payload()
            assert "targets_shown" not in result.payload()
        assert display.menus == [] and display.messages == []


def test_a_simulated_display_decodes_no_picture(monkeypatch):
    """Nothing is drawn there, so nothing is loaded: a simulated run of a rig
    whose target is a picture never needs Pillow or the picture files."""
    from alhazen.devices.eyetracker import calibration_targets as module

    def refuse(name):
        raise AssertionError(f"decoded {name} for a simulated display")

    monkeypatch.setattr(module, "decode_picture", refuse)
    clock = FakeClock()
    cfg = tracker_cfg(appearance="images", images=["food_1"])
    targets = CalibrationTargets(cfg.calibration_target)
    result = validate(
        LooksAtTheTarget(clock, targets),
        FakeDisplay(clock),
        SCREEN,
        clock,
        cfg,
        poll_keys=lambda: [],
        calibration_targets=targets,
        timing=FAST,
    )
    assert len(result.targets) == 5 and result.target_style == ""


def test_the_validation_event_records_its_target(render_psychopy):
    """The VALIDATION event says what was drawn and where, in the
    CALIBRATION event's shape — the session's record of what the subject
    was asked to look at."""
    clock = FakeClock()
    display = RenderDisplay(clock)
    cfg = tracker_cfg(motion="pulse")
    targets = CalibrationTargets(cfg.calibration_target)
    tracker = LooksAtTheTarget(clock, targets)
    monitor = EyeTrackerMonitor(tracker, display, SCREEN, clock, cfg, poll_keys=lambda: [])
    events: list[tuple[str, dict]] = []
    monitor.emit = lambda name, payload: events.append((name, payload))
    monitor.validate()
    monitor.drift_correct()
    (validation,) = [p for n, p in events if n == "VALIDATION"]
    (drift,) = [p for n, p in events if n == "DRIFT_CORRECTION"]
    for payload, n in ((validation, 5), (drift, 1)):
        assert payload["target_style"] == "standard target, pulsating 1-1.4x at 1 Hz"
        shown = payload["targets_shown"]
        assert len(shown) == n
        assert set(shown[0]) == {"ordinal", "target_px", "image", "t"}
    assert [s["ordinal"] for s in drift["targets_shown"]] == [6]
    assert math.isfinite(drift["targets_shown"][0]["t"])
