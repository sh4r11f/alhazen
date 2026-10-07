"""Calibration target appearance and motion (eyetracker.calibration_target).

What these hold, in the order a calibration meets them:

- the config: the default is the standard target, still — exactly what a rig
  that says nothing has always drawn — and every choice that cannot be drawn
  is refused when the rig loads, naming the fix;
- the pictures: shipped as package data, read only by manifest name, each
  checked against its SHA-256, mode and size before it is shown, and present
  in a built wheel;
- the decisions, pure: the pulse's size over time (a function of elapsed time,
  so the same at any refresh rate), the picture order (an isolated, seeded
  stream; a shuffled deck with no immediate repeat), whether the largest
  target fits at the outermost point;
- the drawing, through both backends' real calibration code with PsychoPy,
  pylink and pypixxlib stood in for: what is drawn where, at what size, when
  it is redrawn, and that the walk's sampling, acceptance and timing are the
  same whether the target is still or pulsating.

Everything here is a software contract with SDK fakes. Nothing in it is
evidence about a real EyeLink, a real TRACKPixx3 or calibration accuracy.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest

from alhazen.config import calibration_images as images_module
from alhazen.config.calibration_images import (
    catalog,
    image_names,
    image_path,
    manifest,
    verified_bytes,
)
from alhazen.config.loader import load_rig
from alhazen.config.models import (
    CalibrationPulseConfig,
    CalibrationTargetConfig,
    EyeTrackerConfig,
    RigConfig,
    with_calibration_target,
)
from alhazen.config.rigs import shared_rig_files
from alhazen.core.rng import STREAMS, named_stream, spawn_streams
from alhazen.devices.eyetracker import ViewPixxTracker
from alhazen.devices.eyetracker.calibration import make_calibration_graphics
from alhazen.devices.eyetracker.calibration_targets import (
    STANDARD_TARGET_DIAMETER_PX,
    CalibrationTargets,
    PictureDeck,
    check_fits,
    decode_picture,
    largest_extent_px,
    picture_size_px,
    pulse_scale,
)
from alhazen.devices.eyetracker.viewpixx import (
    AUTO_SETTLE_S,
    AUTO_STEADY_REFRESHES,
    STATUS_REFRESH_S,
)
from alhazen.display.screen import Screen
from alhazen.errors import ConfigError, TrackerError
from alhazen.modes import Mode, flag_refusal
from alhazen.testing import FakeClock
from fake_sdk import install_fake_pypixxlib

ROOT = Path(__file__).parents[2]
SCREEN = Screen(width_px=1920, height_px=1080, px_per_deg=40.0)
PULSE = CalibrationPulseConfig()


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------


class TestTheDefaultIsWhatCalibrationAlwaysDrew:
    def test_a_tracker_that_says_nothing_draws_the_standard_target_still(self):
        target = EyeTrackerConfig(backend="eyelink").calibration_target
        assert target.is_default
        assert (target.appearance, target.motion, target.images) == ("standard", "still", ())
        assert target.describe() == "standard target, still"

    @pytest.mark.parametrize("name", ["lab", "vpixx", "laptop", "mac", "lab-rehearsal"])
    def test_every_shared_rig_keeps_the_standard_still_target(self, name):
        rig = load_rig(shared_rig_files()[name])
        tracker = rig.devices.eyetracker
        if tracker is not None and tracker.backend in ("eyelink", "viewpixx"):
            assert tracker.calibration_target.is_default
            assert "calibration_target" not in tracker.model_fields_set

    def test_the_standard_still_target_needs_no_pictures_and_no_room(self):
        targets = CalibrationTargets(CalibrationTargetConfig())
        # Not even at calibration_area 1.0, where its half hangs off the edge:
        # it is drawn exactly as before, so nothing new is checked about it.
        targets.prepare(SCREEN, 1.0)
        assert targets.pool == ()

    def test_a_round_trip_through_the_snapshot_is_exact(self):
        chosen = CalibrationTargetConfig(
            appearance="random_images",
            images=("monkey_1", "food_3"),
            image_size_dva=3.0,
            motion="pulse",
            pulse={"rate_hz": 0.5, "min_scale": 0.8, "max_scale": 1.2},
        )
        tracker = EyeTrackerConfig(backend="viewpixx", calibration_target=chosen)
        # As the snapshot writes it (JSON), and as a rig file restating it.
        dumped = json.loads(json.dumps(tracker.model_dump(mode="json")))
        assert dumped["calibration_target"]["images"] == ["monkey_1", "food_3"]
        assert CalibrationTargetConfig.model_validate(dumped["calibration_target"]) == chosen
        restated = json.loads(json.dumps(tracker.model_dump(mode="json", exclude_unset=True)))
        assert EyeTrackerConfig.model_validate(restated) == tracker


class TestRefusedWhenTheRigLoads:
    @pytest.mark.parametrize(
        "settings, words",
        [
            ({"appearance": "images"}, "needs the pictures to show"),
            ({"appearance": "images", "images": ["monkey_99"]}, "not among alhazen"),
            ({"appearance": "random_images", "images": ["../rigs/rig-lab"]}, "not among"),
            ({"appearance": "images", "images": ["food_1", "food_1"]}, "more than once"),
            ({"images": ["food_1"]}, "the standard target ignores images"),
            ({"image_size_dva": 2.0}, "the standard target ignores image_size_dva"),
            ({"pulse": {"rate_hz": 1.0}}, "a still target ignores `pulse`"),
            ({"appearance": "images", "images": ["food_1"], "image_size_dva": 0}, "image_size"),
            ({"appearance": "images", "images": ["food_1"], "image_size_dva": 12}, "image_size"),
            ({"motion": "pulse", "pulse": {"rate_hz": 3.0}}, "rate_hz 3 must be in 0.1-2"),
            ({"motion": "pulse", "pulse": {"rate_hz": 0}}, "rate_hz"),
            ({"motion": "pulse", "pulse": {"max_scale": 2.5}}, "max_scale 2.5 must be in"),
            ({"motion": "pulse", "pulse": {"min_scale": 0.2}}, "min_scale 0.2 must be in"),
            ({"motion": "pulse", "pulse": {"min_scale": 1.4, "max_scale": 1.0}}, "must exceed"),
            ({"motion": "pulse", "pulse": {"min_scale": 1.0, "max_scale": 1.01}}, "motion: still"),
            ({"motion": "pulse", "pulse": {"rate_hz": float("nan")}}, "rate_hz"),
            ({"appearance": "pictures"}, "appearance"),
            ({"motion": "flash"}, "motion"),
        ],
    )
    def test_a_choice_that_cannot_be_drawn_is_refused_by_name(self, settings, words):
        with pytest.raises(ValueError, match=words.replace("(", r"\(").replace("`", "`")):
            EyeTrackerConfig(backend="eyelink", calibration_target=settings)

    @pytest.mark.parametrize("backend", ["mouse_sim", "scripted"])
    def test_a_stand_in_that_draws_no_target_refuses_one(self, backend):
        with pytest.raises(ValueError, match="ignores calibration_target"):
            EyeTrackerConfig(backend=backend, calibration_target={"motion": "pulse"})

    def test_one_picture_and_any_shipped_picture_are_valid(self):
        one = CalibrationTargetConfig(appearance="images", images=("tree_1",))
        assert one.describe() == "pictures tree_1 in turn (2.5 deg), still"
        every = CalibrationTargetConfig(appearance="random_images", images=image_names())
        assert len(every.images) == 38


class TestChosenForOneRun:
    @staticmethod
    def rig(**target) -> RigConfig:
        return RigConfig(
            monitor={
                "width_px": 1920,
                "height_px": 1080,
                "width_cm": 52.1,
                "distance_cm": 57.0,
                "refresh_rate_hz": 120.0,
            },
            data_root="data",
            devices={"eyetracker": {"backend": "eyelink", **target}},
        )

    def test_nothing_asked_is_the_same_rig(self):
        rig = self.rig()
        assert with_calibration_target(rig) is rig

    def test_the_choice_is_laid_over_the_rig_and_validated(self):
        chosen = with_calibration_target(
            self.rig(), appearance="images", images=["monkey_2", "food_7"], motion="pulse"
        )
        target = chosen.devices.eyetracker.calibration_target
        assert (target.appearance, target.images, target.motion) == (
            "images",
            ("monkey_2", "food_7"),
            "pulse",
        )
        # The rest of the rig, and the tracker's other settings, untouched.
        assert chosen.monitor == self.rig().monitor
        assert chosen.devices.eyetracker.calibration_type == "HV5"

    def test_standard_drops_the_pictures_and_still_drops_the_pulse(self):
        rig = self.rig(
            calibration_target={
                "appearance": "images",
                "images": ["monkey_1"],
                "image_size_dva": 3.0,
                "motion": "pulse",
                "pulse": {"rate_hz": 0.5},
            }
        )
        chosen = with_calibration_target(rig, appearance="standard", motion="still")
        assert chosen.devices.eyetracker.calibration_target.is_default

    def test_a_changed_appearance_starts_from_no_names(self):
        rig = self.rig(calibration_target={"appearance": "images", "images": ["monkey_1"]})
        chosen = with_calibration_target(rig, appearance="random_images")
        assert chosen.devices.eyetracker.calibration_target.images == ()
        with pytest.raises(ConfigError, match="needs the pictures to show"):
            with_calibration_target(self.rig(), appearance="images")

    def test_a_rig_with_no_drawing_tracker_refuses_the_choice(self):
        no_tracker = self.rig().model_copy(
            update={"devices": self.rig().devices.model_copy(update={"eyetracker": None})}
        )
        with pytest.raises(ConfigError, match="this rig has no eye tracker"):
            with_calibration_target(no_tracker, motion="pulse")
        mouse = no_tracker.model_copy(
            update={
                "devices": no_tracker.devices.model_copy(
                    update={"eyetracker": EyeTrackerConfig(backend="mouse_sim")}
                )
            }
        )
        with pytest.raises(ConfigError, match="the mouse_sim stand-in"):
            with_calibration_target(mouse, appearance="random_images")

    def test_an_unknown_picture_is_a_config_error(self):
        with pytest.raises(ConfigError, match="not among alhazen"):
            with_calibration_target(self.rig(), appearance="images", images=["giraffe_1"])

    @pytest.mark.parametrize("mode", [Mode.SIMULATE, Mode.DEMO, Mode.MOVIE, Mode.MEASURE])
    def test_only_run_and_test_take_the_flags(self, mode):
        assert "only run and test calibrate" in flag_refusal(mode, calibration=True)

    def test_not_with_the_mouse_as_gaze(self):
        assert "replaces the rig's eye tracker" in flag_refusal(
            Mode.TEST, mouse=True, calibration=True
        )
        assert flag_refusal(Mode.TEST, calibration=True) is None
        assert flag_refusal(Mode.RUN, calibration=True) is None


# ---------------------------------------------------------------------------
# The pictures
# ---------------------------------------------------------------------------


class TestTheShippedPictures:
    def test_thirty_eight_originals_with_their_provenance(self):
        data = manifest()
        assert data["source"] == {
            "repository": "sh4r11f/realtime-rdk",
            "commit": "0fe02e1361c9a0293450934443358102c6efa6eb",
            "directory": "assets/calibration",
        }
        names = image_names()
        assert len(names) == 38
        kinds = {
            kind: sum(n.startswith(kind + "_") for n in names)
            for kind in ("monkey", "food", "animal", "tree")
        }
        assert kinds == {"monkey": 10, "food": 24, "animal": 3, "tree": 1}

    def test_every_file_matches_its_manifest_entry(self):
        for name, entry in catalog().items():
            data = image_path(name).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry.sha256
            picture = decode_picture(name)
            assert picture.mode == "RGBA"
            assert picture.size == (entry.width_px, entry.height_px)
            alpha = np.asarray(picture)[..., 3]
            # Transparent outside the figure: the corners are clear.
            assert alpha[0, 0] == 0 and alpha.max() == 255

    @pytest.mark.parametrize("bad", ["../rigs/rig-lab", "monkey_1.png", "a/b", "", "MONKEY_1"])
    def test_only_a_manifest_name_reaches_a_file(self, bad):
        with pytest.raises(ConfigError, match="not one of alhazen's calibration pictures"):
            image_path(bad)

    @pytest.fixture
    def copied(self, tmp_path, monkeypatch):
        """The shipped folder copied, so a test can damage a file."""
        folder = tmp_path / "calibration_images"
        shutil.copytree(images_module.IMAGE_DIR, folder)
        monkeypatch.setattr(images_module, "IMAGE_DIR", folder)
        images_module.manifest.cache_clear()
        images_module.catalog.cache_clear()
        yield folder
        images_module.manifest.cache_clear()
        images_module.catalog.cache_clear()

    def test_a_changed_file_is_refused_before_it_is_shown(self, copied):
        (copied / "food_1.png").write_bytes((copied / "food_1.png").read_bytes()[:-10])
        with pytest.raises(ConfigError, match="does not match alhazen's manifest"):
            verified_bytes("food_1")
        targets = CalibrationTargets(
            CalibrationTargetConfig(appearance="images", images=("food_2", "food_1"))
        )
        with pytest.raises(TrackerError, match="'food_1'.*does not match"):
            targets.prepare(SCREEN, 0.6)

    def test_a_missing_file_is_refused(self, copied):
        (copied / "tree_1.png").unlink()
        with pytest.raises(ConfigError, match="cannot be read"):
            verified_bytes("tree_1")

    def test_a_picture_that_does_not_decode_is_refused(self, copied):
        manifest_path = copied / "manifest.json"
        data = json.loads(manifest_path.read_text())
        broken = b"not a png at all"
        (copied / "food_2.png").write_bytes(broken)
        for entry in data["images"]:
            if entry["name"] == "food_2":
                entry["sha256"] = hashlib.sha256(broken).hexdigest()
        manifest_path.write_text(json.dumps(data))
        images_module.manifest.cache_clear()
        images_module.catalog.cache_clear()
        with pytest.raises(TrackerError, match="'food_2' cannot be decoded"):
            decode_picture("food_2")

    def test_a_picture_without_transparency_is_refused(self, copied):
        from PIL import Image

        path = copied / "food_3.png"
        Image.open(path).convert("RGB").save(path)
        manifest_path = copied / "manifest.json"
        data = json.loads(manifest_path.read_text())
        for entry in data["images"]:
            if entry["name"] == "food_3":
                entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(data))
        images_module.manifest.cache_clear()
        images_module.catalog.cache_clear()
        with pytest.raises(TrackerError, match="'food_3' is RGB, not RGBA"):
            decode_picture("food_3")

    def test_a_missing_manifest_is_an_incomplete_installation(self, copied):
        (copied / "manifest.json").unlink()
        images_module.manifest.cache_clear()
        with pytest.raises(ConfigError, match="installation is incomplete"):
            manifest()


@pytest.mark.slow
def test_a_built_wheel_carries_every_picture_and_reads_them_offline(tmp_path):
    """The wheel, not the source tree: built with pip into a scratch folder
    (no network, no build isolation: setuptools from this environment), then
    unpacked and imported from there alone, as an installed rig would."""
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path / "dist"),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    wheel = next((tmp_path / "dist").glob("alhazen_vision-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        files = set(archive.namelist())
        for entry in manifest()["images"]:
            member = f"alhazen/calibration_images/{entry['file']}"
            assert member in files
            assert hashlib.sha256(archive.read(member)).hexdigest() == entry["sha256"]
        assert "alhazen/calibration_images/manifest.json" in files
        assert "alhazen/calibration_images/README.md" in files
        assert "alhazen/cli/assets/workspace_calibration.js" in files
        archive.extractall(tmp_path / "installed")
    check = (
        "from alhazen.config.calibration_images import IMAGE_DIR, image_names, verified_bytes\n"
        "names = image_names(); [verified_bytes(n) for n in names]\n"
        "print(len(names), IMAGE_DIR)\n"
    )
    ran = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path / 'installed')!r})\n" + check,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert ran.returncode == 0, ran.stderr[-2000:]
    count, folder = ran.stdout.split(maxsplit=1)
    assert count == "38" and folder.strip().startswith(str(tmp_path / "installed"))


# ---------------------------------------------------------------------------
# The decisions: the pulse, the picture order, the fit
# ---------------------------------------------------------------------------


class TestThePulse:
    def test_it_starts_at_its_smallest_and_peaks_half_a_cycle_later(self):
        assert pulse_scale(0.0, PULSE) == pytest.approx(1.0)
        assert pulse_scale(0.25, PULSE) == pytest.approx(1.2)
        assert pulse_scale(0.5, PULSE) == pytest.approx(1.4)
        assert pulse_scale(1.0, PULSE) == pytest.approx(1.0)
        assert pulse_scale(-0.01, PULSE) == pytest.approx(1.0)  # a clock read a hair early

    @pytest.mark.parametrize("rate", [0.1, 0.5, 1.0, 2.0])
    def test_it_never_leaves_its_limits_and_repeats_at_its_rate(self, rate):
        pulse = CalibrationPulseConfig(rate_hz=rate, min_scale=0.8, max_scale=1.3)
        times = np.linspace(0.0, 5.0 / rate, 5001)
        scales = np.array([pulse_scale(t, pulse) for t in times])
        assert scales.min() >= 0.8 - 1e-12 and scales.max() <= 1.3 + 1e-12
        assert scales.min() == pytest.approx(0.8) and scales.max() == pytest.approx(1.3)
        for t in times[::97]:
            assert pulse_scale(t + 1.0 / rate, pulse) == pytest.approx(pulse_scale(t, pulse))

    @pytest.mark.parametrize("hz", [59.94, 60.0, 120.0, 144.0, 240.0])
    def test_the_size_depends_on_time_not_on_frames(self, hz):
        # Sampled once per frame at any refresh rate, every sample is the
        # value at that moment: the rate cannot drift with the display, and
        # the change from one frame to the next is small (no jump).
        period = 1.0 / hz
        frames = [pulse_scale(i * period, PULSE) for i in range(int(3 * hz))]
        assert max(frames) <= 1.4 + 1e-12
        biggest_step = max(abs(b - a) for a, b in zip(frames, frames[1:], strict=False))
        assert biggest_step <= 0.4 * math.pi * PULSE.rate_hz * period + 1e-12
        peak = int(np.argmax(frames))
        assert abs(peak * period - 0.5) <= period

    def test_a_late_frame_shows_its_own_moment(self):
        # A frame drawn late is not a step of a cumulative animation: it is
        # simply the size for the time it is drawn at.
        on_time = pulse_scale(0.30, PULSE)
        assert pulse_scale(0.30, PULSE) == on_time
        assert pulse_scale(0.30 + 1 / 60, PULSE) != on_time


class TestThePictureOrder:
    def test_chosen_pictures_go_in_their_listed_order_round_and_round(self):
        deck = PictureDeck(["monkey_1", "food_2", "tree_1"], "listed")
        assert [deck.deal() for _ in range(7)] == [
            "monkey_1",
            "food_2",
            "tree_1",
            "monkey_1",
            "food_2",
            "tree_1",
            "monkey_1",
        ]

    def test_one_chosen_picture_is_shown_every_time(self):
        deck = PictureDeck(["tree_1"], "listed")
        assert {deck.deal() for _ in range(5)} == {"tree_1"}

    @pytest.mark.parametrize("size", [2, 3, 5, 38])
    def test_a_random_deck_deals_each_once_and_never_twice_in_a_row(self, size):
        names = list(image_names()[:size])
        deck = PictureDeck(names, "shuffled", np.random.default_rng(11))
        dealt = [deck.deal() for _ in range(size * 40)]
        for start in range(0, len(dealt), size):
            assert sorted(dealt[start : start + size]) == sorted(names)
        assert all(a != b for a, b in zip(dealt, dealt[1:], strict=False))

    def test_a_random_deck_of_one_shows_that_one(self):
        deck = PictureDeck(["food_5"], "shuffled", np.random.default_rng(0))
        assert {deck.deal() for _ in range(4)} == {"food_5"}

    def test_the_order_is_fixed_by_the_session_seed(self):
        def order(seed: int) -> list[str]:
            targets = CalibrationTargets(
                CalibrationTargetConfig(appearance="random_images"),
                named_stream(seed, "calibration_target"),
            )
            return [targets.next_picture() for _ in range(50)]

        assert order(2718) == order(2718)
        assert order(2718) != order(2719)

    def test_its_stream_is_its_own(self):
        # The calibration stream is appended to STREAMS, so every existing
        # stream draws exactly what it did before it existed, and drawing
        # pictures does not move the trial order.
        assert STREAMS[:3] == ("scheduler", "session", "task")
        before = {
            name: np.random.default_rng(child).integers(0, 10**9, 20).tolist()
            for name, child in zip(STREAMS[:3], np.random.SeedSequence(77).spawn(3), strict=True)
        }
        streams = spawn_streams(77)
        targets = CalibrationTargets(
            CalibrationTargetConfig(appearance="random_images"),
            named_stream(77, "calibration_target"),
        )
        for _ in range(100):
            targets.next_picture()
        after = {name: streams[name].integers(0, 10**9, 20).tolist() for name in STREAMS[:3]}
        assert after == before
        assert (
            named_stream(5, "calibration_target").integers(0, 10**9, 10).tolist()
            == spawn_streams(5)["calibration_target"].integers(0, 10**9, 10).tolist()
        )

    def test_without_a_session_stream_a_random_deck_draws_and_names_its_own_seed(self):
        targets = CalibrationTargets(CalibrationTargetConfig(appearance="random_images"))
        targets.next_picture()
        assert targets.seed_note.startswith("own seed ")
        assert "own seed" in targets.style

    def test_the_session_stream_is_taken_before_the_first_deal_and_not_after(self):
        targets = CalibrationTargets(CalibrationTargetConfig(appearance="random_images"))
        targets.use_rng(named_stream(9, "calibration_target"))
        first = [targets.next_picture() for _ in range(5)]
        again = CalibrationTargets(
            CalibrationTargetConfig(appearance="random_images"),
            named_stream(9, "calibration_target"),
        )
        assert [again.next_picture() for _ in range(5)] == first
        assert targets.seed_note == "session seed, stream calibration_target"
        with pytest.raises(RuntimeError, match="already been dealt"):
            targets.use_rng(named_stream(10, "calibration_target"))


class TestTheFit:
    def test_sizes_in_px(self):
        assert largest_extent_px(CalibrationTargetConfig(), SCREEN) == STANDARD_TARGET_DIAMETER_PX
        pulsing = CalibrationTargetConfig(motion="pulse")
        assert largest_extent_px(pulsing, SCREEN) == pytest.approx(24 * 1.4)
        picture = CalibrationTargetConfig(appearance="images", images=("food_1",), motion="pulse")
        assert largest_extent_px(picture, SCREEN) == pytest.approx(2.5 * 40 * 1.4)

    def test_a_picture_keeps_its_aspect(self):
        config = CalibrationTargetConfig(appearance="images", images=("monkey_7",))
        width, height = picture_size_px("monkey_7", config, SCREEN)
        entry = catalog()["monkey_7"]
        assert entry.width_px != entry.height_px
        assert max(width, height) == pytest.approx(100.0)
        assert width / height == pytest.approx(entry.width_px / entry.height_px)

    def test_room_at_the_outermost_point(self):
        picture = CalibrationTargetConfig(appearance="images", images=("food_1",), motion="pulse")
        check_fits(picture, SCREEN, 0.6)  # 70 px half; 216 px room on the short axis
        with pytest.raises(TrackerError, match=r"calibration_area to at most 0\.87"):
            check_fits(picture, SCREEN, 0.9)

    def test_even_a_pulsating_standard_target_is_checked(self):
        with pytest.raises(TrackerError, match="cut off"):
            check_fits(CalibrationTargetConfig(motion="pulse"), SCREEN, 1.0)


# ---------------------------------------------------------------------------
# The drawing: a PsychoPy stand-in that records what was drawn
# ---------------------------------------------------------------------------


class Recorder:
    """What every stimulus drew, in order: (kind, pos, size, image) per draw."""

    def __init__(self) -> None:
        self.draws: list[tuple] = []
        self.made: list = []


class FakeStim:
    def __init__(self, recorder: Recorder, kind: str, window, **kwargs) -> None:
        self.recorder = recorder
        self.kind = kind
        self.window = window
        self.kwargs = kwargs
        self.pos = kwargs.get("pos", (0.0, 0.0))
        self.radius = kwargs.get("radius")
        self.size = kwargs.get("size")
        self.text = kwargs.get("text", "")
        self.radius_sets = 0
        recorder.made.append(self)

    def __setattr__(self, name, value):
        if name == "radius" and "radius_sets" in self.__dict__:
            self.__dict__["radius_sets"] += 1
        super().__setattr__(name, value)

    def draw(self) -> None:
        image = self.kwargs.get("image")
        self.recorder.draws.append(
            (
                self.kind,
                tuple(self.pos),
                self.radius if self.kind == "circle" else self.size,
                getattr(image, "name", None),
                self.window.clock.now(),
            )
        )


class FakeWindow:
    """A window whose flip spends one frame on the clock."""

    def __init__(self, clock: FakeClock, hz: float = 60.0) -> None:
        self.clock = clock
        self.hz = hz
        self.color = (0.0, 0.0, 0.0)
        self.flips = 0
        self._closed = False

    def flip(self) -> None:
        self.flips += 1
        self.clock.advance(1.0 / self.hz)


def fake_visual(recorder: Recorder):
    module = types.ModuleType("psychopy.visual")
    module.Circle = lambda window, **kw: FakeStim(recorder, "circle", window, **kw)
    module.ImageStim = lambda window, **kw: FakeStim(recorder, "image", window, **kw)
    module.TextStim = lambda window, **kw: FakeStim(recorder, "text", window, **kw)
    return module


class Named:
    """A decoded picture as the fake ImageStim sees it: just its name."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture
def named_pictures(monkeypatch):
    """Decoding stood in for by the picture's name, so a draw says which
    picture it showed (real decoding is TestTheShippedPictures')."""
    from alhazen.devices.eyetracker import calibration_targets as module

    monkeypatch.setattr(module, "decode_picture", Named)


def presenter(config: CalibrationTargetConfig, clock: FakeClock, recorder: Recorder, **kw):
    targets = CalibrationTargets(config, **kw)
    targets.prepare(SCREEN, 0.6)
    window = FakeWindow(clock)
    return targets, targets.presenter(fake_visual(recorder), window, SCREEN, clock.now), window


class TestThePresenter:
    def test_the_standard_still_target_is_drawn_as_it_always_was(self):
        clock, recorder = FakeClock(), Recorder()
        _, target, _ = presenter(CalibrationTargetConfig(), clock, recorder)
        outer, inner = recorder.made
        assert (outer.kwargs["radius"], inner.kwargs["radius"]) == (12.0, 4.0)
        assert outer.kwargs["fillColor"] == (-1.0, -1.0, -1.0)
        assert inner.kwargs["fillColor"] == (0.0, 0.0, 0.0)  # the window's own colour
        target.show((300.0, -200.0))
        for _ in range(5):
            target.draw()
            clock.advance(0.1)
        assert {d[1] for d in recorder.draws} == {(300.0, -200.0)}
        assert {d[2] for d in recorder.draws} == {12.0, 4.0}
        assert outer.radius_sets == 0 and inner.radius_sets == 0
        assert not target.animated

    def test_a_pulsating_standard_target_scales_disc_and_hole_together(self):
        clock, recorder = FakeClock(), Recorder()
        _, target, _ = presenter(CalibrationTargetConfig(motion="pulse"), clock, recorder)
        target.show((0.0, 0.0))
        clock.advance(0.5)
        target.draw()
        outer_radius, inner_radius = recorder.draws[-2][2], recorder.draws[-1][2]
        assert outer_radius == pytest.approx(12.0 * 1.4)
        assert inner_radius == pytest.approx(4.0 * 1.4)
        assert target.animated

    def test_a_picture_is_centred_keeps_its_aspect_and_pulses_about_its_centre(
        self, named_pictures
    ):
        clock, recorder = FakeClock(), Recorder()
        config = CalibrationTargetConfig(appearance="images", images=("monkey_7",), motion="pulse")
        _, target, _ = presenter(config, clock, recorder)
        target.show((-400.0, 250.0))
        still = picture_size_px("monkey_7", config, SCREEN)
        for _ in range(12):
            target.draw()
            kind, pos, size, image, t = recorder.draws[-1]
            scale = pulse_scale(t, config.pulse)
            assert (kind, pos, image) == ("image", (-400.0, 250.0), "monkey_7")
            assert size == pytest.approx((still[0] * scale, still[1] * scale))
            clock.advance(1 / 12)

    def test_a_target_keeps_its_picture_and_pulse_while_it_stays_up(self, named_pictures):
        clock, recorder = FakeClock(), Recorder()
        config = CalibrationTargetConfig(
            appearance="images", images=("food_1", "food_2"), motion="pulse"
        )
        targets, target, _ = presenter(config, clock, recorder)
        target.show((10.0, 10.0))
        clock.advance(0.3)
        target.show((10.0, 10.0))  # the same target again: not a new one
        assert target.image == "food_1"
        assert target.scale() == pytest.approx(pulse_scale(0.3, config.pulse))
        target.show((20.0, 10.0))  # a new point: the next picture, a fresh pulse
        assert target.image == "food_2" and target.scale() == pytest.approx(1.0)
        target.hide()
        target.draw()
        assert not target.showing and not target.animated
        target.show((20.0, 10.0))  # shown again after being taken down: new
        assert target.image == "food_1"
        shown = targets.shown_since_begin()
        assert [(s.ordinal, s.image, s.target_px) for s in shown] == [
            (1, "food_1", (10.0, 10.0)),
            (2, "food_2", (20.0, 10.0)),
            (3, "food_1", (20.0, 10.0)),
        ]

    def test_pictures_are_made_once_before_any_target_is_shown(self, named_pictures):
        clock, recorder = FakeClock(), Recorder()
        config = CalibrationTargetConfig(appearance="random_images")
        _, target, _ = presenter(config, clock, recorder)
        made = len(recorder.made)
        assert made == 38
        for i in range(10):
            target.show((float(i), 0.0))
            target.draw()
        assert len(recorder.made) == made  # nothing created while drawing

    def test_a_picture_not_prepared_is_refused_not_drawn_blank(self):
        targets = CalibrationTargets(
            CalibrationTargetConfig(appearance="images", images=("food_1",))
        )
        with pytest.raises(TrackerError, match="was not loaded before the calibration"):
            targets.presenter(
                fake_visual(Recorder()), FakeWindow(FakeClock()), SCREEN, FakeClock().now
            )


# ---------------------------------------------------------------------------
# The EyeLink: pylink's callbacks into the graphics
# ---------------------------------------------------------------------------


@pytest.fixture
def graphics_sdk(monkeypatch):
    """A pylink and a psychopy just deep enough for make_calibration_graphics."""
    recorder = Recorder()
    pylink = types.ModuleType("pylink")
    pylink.EyeLinkCustomDisplay = object
    pylink.KeyInput = lambda code, modifier: (code, modifier)
    pylink.JUNK_KEY = 1
    psychopy = types.ModuleType("psychopy")
    event = types.ModuleType("psychopy.event")
    event.pending = []
    event.getKeys = lambda modifiers=False: [event.pending.pop(0)] if event.pending else []
    event.Mouse = lambda win=None, visible=False: types.SimpleNamespace()
    visual = fake_visual(recorder)
    psychopy.event, psychopy.visual = event, visual
    for name, module in (
        ("pylink", pylink),
        ("psychopy", psychopy),
        ("psychopy.event", event),
        ("psychopy.visual", visual),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return recorder


def graphics(config, clock, hz=60.0):
    targets = CalibrationTargets(config)
    targets.prepare(SCREEN, 0.6)
    window = FakeWindow(clock, hz)
    return make_calibration_graphics(None, window, SCREEN, targets=targets, now=clock.now), window


class TestEyeLinkGraphics:
    def test_a_still_target_is_drawn_once_and_never_redrawn_while_polled(self, graphics_sdk):
        clock = FakeClock()
        display, window = graphics(CalibrationTargetConfig(), clock)
        display.draw_cal_target(960.0, 540.0)  # screen px: the centre
        assert window.flips == 1
        assert [d[1] for d in graphics_sdk.draws] == [(0.0, 0.0), (0.0, 0.0)]
        for _ in range(30):
            assert display.get_input_key() is None
        assert window.flips == 1 and len(graphics_sdk.draws) == 2

    @pytest.mark.parametrize("hz", [60.0, 144.0])
    def test_a_pulsating_target_is_redrawn_at_each_poll_at_its_moment(self, graphics_sdk, hz):
        clock = FakeClock()
        display, window = graphics(CalibrationTargetConfig(motion="pulse"), clock, hz)
        display.draw_cal_target(1460.0, 240.0)
        onset = 0.0
        for _ in range(int(hz)):  # one second of polling, one flip each
            display.get_input_key()
        outer = [d for d in graphics_sdk.draws if d[2] is not None and d[0] == "circle"][::2]
        assert len(outer) == int(hz) + 1
        for _, pos, radius, _, t in outer:
            assert pos == (500.0, 300.0)
            assert radius == pytest.approx(12.0 * pulse_scale(t - onset, PULSE))
        radii = [d[2] for d in outer]
        assert max(radii) == pytest.approx(12.0 * 1.4, rel=0.01)

    def test_erase_clear_exit_and_the_camera_image_take_the_target_down(self, graphics_sdk):
        for take_down in (
            "erase_cal_target",
            "clear_cal_display",
            "exit_cal_display",
            "setup_cal_display",
        ):
            clock = FakeClock()
            display, window = graphics(CalibrationTargetConfig(motion="pulse"), clock)
            display.draw_cal_target(100.0, 100.0)
            getattr(display, take_down)()
            draws = len(graphics_sdk.draws)
            for _ in range(10):
                display.get_input_key()
            assert len(graphics_sdk.draws) == draws, take_down
        clock = FakeClock()
        display, window = graphics(CalibrationTargetConfig(motion="pulse"), clock)
        display.draw_cal_target(100.0, 100.0)
        display.setup_image_display(384, 320)
        draws = len(graphics_sdk.draws)
        display.get_input_key()
        assert len(graphics_sdk.draws) == draws

    def test_each_target_the_host_puts_up_is_recorded_with_its_picture(
        self, graphics_sdk, named_pictures
    ):
        clock = FakeClock()
        config = CalibrationTargetConfig(appearance="images", images=("tree_1", "food_9"))
        targets = CalibrationTargets(config)
        targets.prepare(SCREEN, 0.6)
        window = FakeWindow(clock)
        display = make_calibration_graphics(None, window, SCREEN, targets=targets, now=clock.now)
        targets.begin_procedure()
        display.draw_cal_target(960.0, 540.0)
        display.draw_cal_target(960.0, 540.0)  # drawn again, not erased: same target
        display.erase_cal_target()
        display.draw_cal_target(960.0, 540.0)  # back at the centre (a redo): a new one
        assert [(s.image, s.target_px) for s in targets.shown_since_begin()] == [
            ("tree_1", (0.0, 0.0)),
            ("food_9", (0.0, 0.0)),
        ]
        assert [d[3] for d in graphics_sdk.draws] == ["tree_1", "tree_1", "food_9"]


# ---------------------------------------------------------------------------
# The TRACKPixx3: alhazen's own walk
# ---------------------------------------------------------------------------


class WalkDisplay:
    kind = "fake"

    def __init__(self, clock: FakeClock, hz: float) -> None:
        self.window = FakeWindow(clock, hz)
        self.menus: list = []
        self.messages: list = []

    def show_message(self, text: str) -> None:
        self.messages.append(text)

    def show_menu(self, title: str, body: str, *, color) -> None:
        self.menus.append(title)


@pytest.fixture
def walk_sdk(monkeypatch):
    """A TRACKPixx3 and a psychopy whose keys are pressed at set times on the
    session clock: waitKeys spends its wait (or until the key), getKeys
    returns what is due, flips spend a frame. Same keys at the same times
    for a still and a pulsating run, so their walks can be compared."""
    device = install_fake_pypixxlib(monkeypatch)
    recorder = Recorder()
    state = types.SimpleNamespace(schedule=[], clock=None, window=None, waits=[], polls=0)

    def due(now):
        if state.schedule and state.schedule[0][0] <= now + 1e-9:
            return state.schedule.pop(0)[1]
        return None

    def wait_keys(maxWait=None, keyList=None, clearEvents=True):  # noqa: N803
        state.waits.append(maxWait)
        start = state.clock.now()
        if state.schedule and state.schedule[0][0] <= start + maxWait + 1e-9:
            state.clock.advance(max(state.schedule[0][0] - start, 0.0))
            return [due(state.clock.now())]
        state.clock.advance(maxWait)
        if not state.schedule and state.window is not None:
            state.window._closed = True
        return None

    def get_keys(keyList=None):  # noqa: N803
        state.polls += 1
        key = due(state.clock.now())
        if key is None and not state.schedule and state.window is not None:
            state.window._closed = True
        return [key] if key else []

    event = types.ModuleType("psychopy.event")
    event.waitKeys, event.getKeys = wait_keys, get_keys
    event.clearEvents = lambda eventType=None: None  # noqa: N803
    visual = fake_visual(recorder)
    psychopy = types.ModuleType("psychopy")
    psychopy.event, psychopy.visual = event, visual
    for name, module in (
        ("psychopy", psychopy),
        ("psychopy.event", event),
        ("psychopy.visual", visual),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(device=device, recorder=recorder, state=state)


def walk(walk_sdk, *, hz=60.0, keys, rng=None, advance="manual", **target):
    clock = FakeClock()
    display = WalkDisplay(clock, hz)
    walk_sdk.state.clock, walk_sdk.state.window = clock, display.window
    walk_sdk.state.schedule = list(keys)
    cfg = EyeTrackerConfig(
        backend="viewpixx",
        calibration_type="HV5",
        calibration_advance=advance,
        calibration_target=target,
    )
    tracker = ViewPixxTracker(
        cfg, display, SCREEN, clock, background_gaze=False, calibration_rng=rng
    )
    tracker.connect()
    tracker.configure(SCREEN, clock)
    result = tracker.calibrate()
    return result, display


# SPACE at the guide, then one SPACE per target, each a little after the
# previous: the same presses, at the same session times, in every run.
KEYS = [(0.05, "space")] + [(0.05 + 0.73 * (i + 1), "space") for i in range(5)]


class TestTheViewPixxWalk:
    def test_a_still_target_walks_exactly_as_before(self, walk_sdk):
        result, display = walk(walk_sdk, keys=KEYS)
        assert result.ok and len(walk_sdk.device.calibration_points) == 5
        # One wait per refresh, never a poll, never a resized circle.
        assert set(walk_sdk.state.waits) == {STATUS_REFRESH_S}
        assert walk_sdk.state.polls == 0
        circles = [s for s in walk_sdk.recorder.made if s.kind == "circle"]
        assert [c.radius_sets for c in circles] == [0, 0]
        assert result.target_style == "standard target, still"
        assert [s.ordinal for s in result.shown] == [1, 2, 3, 4, 5]

    @pytest.mark.parametrize("hz", [60.0, 144.0])
    def test_pulsing_changes_what_is_drawn_and_nothing_the_device_is_told(self, walk_sdk, hz):
        still, _ = walk(walk_sdk, keys=KEYS)
        still_points = list(walk_sdk.device.calibration_points)
        still_times = [s.t for s in still.shown]
        walk_sdk.device.calibration_points.clear()
        walk_sdk.recorder.draws.clear()
        pulsed, display = walk(walk_sdk, hz=hz, keys=KEYS, motion="pulse")
        # The same targets, sampled in the same order, accepted at the same
        # moments (each within one frame): only the drawing changed.
        assert walk_sdk.device.calibration_points == still_points
        assert pulsed.ok == still.ok and pulsed.n_targets == still.n_targets
        for a, b in zip(still_times, (s.t for s in pulsed.shown), strict=True):
            assert abs(a - b) <= 1.0 / hz + 1e-9
        # Redrawn every frame between status reads, each at its moment's size.
        outer = [d for d in walk_sdk.recorder.draws if d[0] == "circle"][::2]
        assert len(outer) >= int(3.6 * hz)  # 3.65 s of frames, one per flip
        onsets = {s.target_px: s.t for s in pulsed.shown}
        for _, pos, radius, _, t in outer:
            assert radius == pytest.approx(12.0 * pulse_scale(t - onsets[pos], PULSE))
        assert pulsed.target_style.endswith("pulsating 1-1.4x at 1 Hz")

    @pytest.mark.parametrize("hz", [60.0, 144.0])
    def test_auto_advance_accepts_after_the_same_count_of_refreshes(self, walk_sdk, hz):
        # Only SPACE at the guide: the walk accepts each target by itself
        # once the eye has been in view for its count of status refreshes.
        # Pulsing keeps one status refresh per STATUS_REFRESH_S (to within a
        # frame), so each target is accepted at the same moment within one
        # frame per refresh counted.
        # (An ESC long after the walk would have finished keeps the window open.)
        guide_only = [(0.05, "space"), (30.0, "escape")]
        still, _ = walk(walk_sdk, hz=hz, keys=guide_only, advance="auto")
        still_points = list(walk_sdk.device.calibration_points)
        walk_sdk.device.calibration_points.clear()
        pulsed, _ = walk(walk_sdk, hz=hz, keys=guide_only, advance="auto", motion="pulse")
        assert still.ok and pulsed.ok
        assert walk_sdk.device.calibration_points == still_points
        refreshes = AUTO_STEADY_REFRESHES + math.ceil(AUTO_SETTLE_S / STATUS_REFRESH_S) + 1
        for index, (a, b) in enumerate(zip(still.shown, pulsed.shown, strict=True)):
            assert abs(a.t - b.t) <= (index + 1) * refreshes / hz + 1e-9

    def test_pictures_one_per_target_and_kept_when_a_target_is_retried(
        self, walk_sdk, named_pictures
    ):
        rng = named_stream(99, "calibration_target")
        keys = [
            (0.05, "space"),
            (0.5, "space"),
            (1.0, "backspace"),
            (1.5, "space"),
            (2.0, "space"),
            (2.5, "space"),
            (3.0, "space"),
            (3.5, "space"),
        ]
        result, _ = walk(
            walk_sdk,
            keys=keys,
            rng=rng,
            appearance="random_images",
            images=["monkey_1", "monkey_2", "monkey_3"],
            motion="pulse",
        )
        assert result.ok
        shown = result.shown
        # Seven targets shown: the five points, plus the first stepped back
        # to with BACKSPACE and the second shown again after it.
        assert len(shown) == 7
        assert [s.target_px for s in shown][:4] == [
            (0.0, 0.0),
            (0.0, 324.0),
            (0.0, 0.0),
            (0.0, 324.0),
        ]
        assert all(s.image in {"monkey_1", "monkey_2", "monkey_3"} for s in shown)
        assert all(a.image != b.image for a, b in zip(shown, shown[1:], strict=False))
        drawn = [d for d in walk_sdk.recorder.draws if d[0] == "image"]
        # While a target is up its picture never changes.
        for s, nxt in zip(shown, list(shown[1:]) + [None], strict=True):
            end = nxt.t if nxt else math.inf
            assert {d[3] for d in drawn if s.t <= d[4] < end and d[1] == s.target_px} == {s.image}
        # And the same seed deals the same pictures again.
        walk_sdk.recorder.draws.clear()
        again, _ = walk(
            walk_sdk,
            keys=keys,
            rng=named_stream(99, "calibration_target"),
            appearance="random_images",
            images=["monkey_1", "monkey_2", "monkey_3"],
            motion="pulse",
        )
        assert [s.image for s in again.shown] == [s.image for s in shown]

    def test_a_target_that_does_not_fit_is_refused_when_the_tracker_is_configured(self, walk_sdk):
        clock = FakeClock()
        display = WalkDisplay(clock, 60.0)
        cfg = EyeTrackerConfig(
            backend="viewpixx",
            calibration_area=0.95,
            calibration_target={"appearance": "images", "images": ["food_1"]},
        )
        tracker = ViewPixxTracker(cfg, display, SCREEN, clock, background_gaze=False)
        tracker.connect()
        with pytest.raises(TrackerError, match="cut off"):
            tracker.configure(SCREEN, clock)
        assert walk_sdk.device.recording_folder is None  # refused before recording started
