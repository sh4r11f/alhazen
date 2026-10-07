"""Where the calibration-target choice enters a session: the command line's
flags, check-rig, the session's CALIBRATION event and the builder's stream.

The drawing itself is test_calibration_targets.py's; these hold the seams.
"""

from __future__ import annotations

import types

import numpy as np
import pytest
import yaml

from alhazen.cli.main import main
from alhazen.config.models import (
    DevicesConfig,
    DisplayConfig,
    EyeTrackerConfig,
    MonitorConfig,
    RigConfig,
)
from alhazen.core.rng import named_stream
from alhazen.devices.eyetracker.protocol import CalibrationResult, TargetShown
from alhazen.display.screen import Screen
from alhazen.session import builder as builder_module
from alhazen.session import checks
from alhazen.session.eyetracker import EyeTrackerMonitor
from alhazen.testing import FakeClock

MONITOR = {
    "width_px": 1920,
    "height_px": 1080,
    "width_cm": 52.1,
    "distance_cm": 57.0,
    "refresh_rate_hz": 120,
}


def rig_file(tmp_path, eyetracker=None):
    body = {
        "monitor": MONITOR,
        "display": {"backend": "simulated"},
        "data_root": str(tmp_path / "data"),
    }
    if eyetracker is not None:
        body["devices"] = {"eyetracker": eyetracker}
    path = tmp_path / "rig-cal.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


class TestTheCommandLine:
    @pytest.mark.parametrize("mode", ["simulate", "demo", "movie", "measure"])
    def test_modes_that_never_calibrate_refuse_the_flags_before_anything_loads(
        self, tmp_path, capsys, mode
    ):
        code = main(
            [
                "run",
                "--mode",
                mode,
                "--rig",
                str(rig_file(tmp_path)),
                "--task",
                "x",
                "--calibration-motion",
                "pulse",
            ]
        )
        assert code == 2
        assert "only run and test calibrate the rig's eye tracker" in capsys.readouterr().err

    def test_test_mode_with_the_mouse_refuses_them(self, tmp_path, capsys):
        code = main(
            [
                "run",
                "--mode",
                "test",
                "--mouse",
                "--rig",
                str(rig_file(tmp_path)),
                "--task",
                "x",
                "--calibration-target",
                "random_images",
            ]
        )
        assert code == 2
        assert "--mouse replaces the rig's eye tracker" in capsys.readouterr().err

    def test_an_unknown_choice_is_an_argparse_error(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exited:
            main(
                [
                    "run",
                    "--mode",
                    "test",
                    "--rig",
                    str(rig_file(tmp_path)),
                    "--calibration-motion",
                    "flash",
                ]
            )
        assert exited.value.code == 2
        assert "invalid choice: 'flash'" in capsys.readouterr().err

    def run_test_mode(self, tmp_path, eyetracker, *flags):
        """run.py in test mode on a rig file, up to where the rig is read
        (the task itself is stood in for and never reached)."""
        from alhazen.cli.modes import run_experiment
        from alhazen.config.models import Model
        from alhazen.core.events import EventSchema
        from alhazen.core.trial import outcomes as make_outcomes
        from alhazen.task.task import Task

        class Params(Model):
            pass

        class CalibrationTask(Task):
            name = "calibration-check"
            events = EventSchema(())
            outcomes = make_outcomes(DONE=dict(completed=True, success=True))
            params_model = Params

        return run_experiment(
            task_class=CalibrationTask,
            default_rig=rig_file(tmp_path, eyetracker),
            argv=["--mode", "test", "--task", "calibration-check", *flags],
        )

    @pytest.mark.parametrize(
        "eyetracker, flags, words",
        [
            (None, ["--calibration-motion", "pulse"], "this rig has no eye tracker"),
            ({"backend": "eyelink"}, ["--calibration-target", "images"], "needs the pictures"),
            (
                {"backend": "eyelink"},
                ["--calibration-target", "images", "--calibration-images", "monkey_1,,food_2"],
                "no empty entries",
            ),
            (
                {"backend": "eyelink"},
                ["--calibration-target", "random_images", "--calibration-images", "elephant"],
                "not among alhazen",
            ),
        ],
    )
    def test_a_choice_the_rig_cannot_draw_is_invalid_before_anyone_is_asked_anything(
        self, tmp_path, capsys, eyetracker, flags, words
    ):
        assert self.run_test_mode(tmp_path, eyetracker, *flags) == 1
        err = capsys.readouterr().err
        assert err.startswith("INVALID:") and words in err

    def test_a_valid_choice_reaches_the_session_build(self, tmp_path, monkeypatch, capsys):
        seen = {}

        def build(mode, *, rig, **kwargs):
            seen["target"] = rig.devices.eyetracker.calibration_target
            raise SystemExit(0)

        monkeypatch.setattr("alhazen.modes.session.build_mode_session", build)
        with pytest.raises(SystemExit):
            self.run_test_mode(
                tmp_path,
                {"backend": "eyelink"},
                "--sub",
                "s01",
                "--ses",
                "1",
                "--initials",
                "HD",
                "--calibration-target",
                "images",
                "--calibration-images",
                "monkey_4,tree_1",
                "--calibration-motion",
                "pulse",
            )
        target = seen["target"]
        assert (target.appearance, target.images, target.motion) == (
            "images",
            ("monkey_4", "tree_1"),
            "pulse",
        )


class TestCheckRig:
    @staticmethod
    def rig(tmp_path, **eyetracker) -> RigConfig:
        return RigConfig(
            monitor=MonitorConfig(**MONITOR),
            display=DisplayConfig(backend="simulated"),
            devices=DevicesConfig(eyetracker=EyeTrackerConfig(**eyetracker)),
            data_root=tmp_path / "data",
        )

    def test_a_target_that_would_be_cut_off_fails_without_touching_the_tracker(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            checks, "make_tracker", lambda *a, **k: pytest.fail("the tracker was built")
        )
        result = checks._check_eyetracker(
            self.rig(
                tmp_path,
                backend="eyelink",
                calibration_area=0.95,
                calibration_target={"appearance": "random_images", "motion": "pulse"},
            )
        )
        assert not result.ok
        assert result.detail.startswith("calibration target: the calibration target")
        assert "calibration_area to at most" in result.detail

    def test_a_target_that_fits_has_every_picture_checked_and_said(self, tmp_path, monkeypatch):
        class Unreachable:
            def connect(self):
                from alhazen.errors import TrackerError

                raise TrackerError("no tracker on this test machine")

        monkeypatch.setattr(checks, "make_tracker", lambda *a, **k: Unreachable())
        result = checks._check_eyetracker(
            self.rig(
                tmp_path,
                backend="eyelink",
                calibration_target={"appearance": "random_images"},
            )
        )
        # The tracker itself is not there; the target was checked first.
        assert result.evidence["calibration_target"].startswith("random pictures from all")
        assert result.evidence["calibration_pictures_checked"] == 38

    def test_the_standard_still_target_is_not_mentioned(self, tmp_path, monkeypatch):
        class Unreachable:
            def connect(self):
                from alhazen.errors import TrackerError

                raise TrackerError("no tracker")

        monkeypatch.setattr(checks, "make_tracker", lambda *a, **k: Unreachable())
        result = checks._check_eyetracker(self.rig(tmp_path, backend="eyelink"))
        assert "calibration_target" not in result.evidence


class TestTheCalibrationEvent:
    @staticmethod
    def monitor(result):
        tracker = types.SimpleNamespace(calibrate=lambda: result)
        events = []
        monitor = EyeTrackerMonitor(
            tracker,
            None,
            Screen.from_monitor(MonitorConfig(**MONITOR)),
            FakeClock(),
            EyeTrackerConfig(backend="eyelink", validate_after_calibration=False),
            poll_keys=lambda: [],
        )
        monitor.emit = lambda name, payload: events.append((name, payload))
        return monitor, events

    def test_it_carries_the_style_and_every_target_shown(self):
        shown = (
            TargetShown(1, (0.0, 0.0), "monkey_2", 1.5),
            TargetShown(2, (0.0, 324.0), "food_9", 2.25),
        )
        result = CalibrationResult(
            ok=True,
            layout="HV5",
            n_targets=5,
            eye="left",
            advance="manual",
            t=3.0,
            target_style="random pictures from all (2.5 deg), still; order from the session seed, "
            "stream calibration_target",
            shown=shown,
        )
        monitor, events = self.monitor(result)
        monitor.calibrate()
        ((name, payload),) = [e for e in events if e[0] == "CALIBRATION"]
        assert payload["target_style"].startswith("random pictures")
        assert payload["targets_shown"] == [
            {"ordinal": 1, "target_px": [0.0, 0.0], "image": "monkey_2", "t": 1.5},
            {"ordinal": 2, "target_px": [0.0, 324.0], "image": "food_9", "t": 2.25},
        ]

    def test_a_tracker_that_draws_no_target_files_the_event_it_always_did(self):
        result = CalibrationResult(
            ok=True, layout="HV5", n_targets=5, eye="left", advance="manual", t=3.0
        )
        monitor, events = self.monitor(result)
        monitor.calibrate()
        ((name, payload),) = [e for e in events if e[0] == "CALIBRATION"]
        assert "target_style" not in payload and "targets_shown" not in payload


class _Captured(Exception):
    pass


def test_the_builder_hands_the_tracker_the_seeds_own_calibration_stream(tmp_path, monkeypatch):
    """A real build_session over a rig with an EyeLink, stopped at the
    tracker's connect: before that, the tracker is handed the session seed's
    calibration_target stream, which draws exactly what named_stream(seed,
    ...) does."""
    from alhazen.config.models import Duration, Model
    from alhazen.core.events import EventSchema
    from alhazen.paradigms.base import Condition, SimpleSequence
    from alhazen.session.builder import build_session
    from alhazen.task.plan import TrialPlan
    from support import COMPLETED, TEST_EXPERIMENT, RunForFrames

    seen = {}

    class Tracker:
        """Stops the build at the first thing done with the tracker after
        the stream is handed over."""

        def set_calibration_rng(self, rng):
            seen["rng"] = rng

        def connect(self):
            raise _Captured

    def make_tracker(cfg, display, screen, clock):
        seen["target"] = cfg.calibration_target
        return Tracker()

    monkeypatch.setattr(builder_module, "make_tracker", make_tracker)

    class Params(Model):
        n_trials: int = 1

    rig = RigConfig(
        monitor=MonitorConfig(**MONITOR),
        display=DisplayConfig(backend="simulated"),
        devices=DevicesConfig(
            eyetracker=EyeTrackerConfig(
                backend="eyelink", calibration_target={"appearance": "random_images"}
            )
        ),
        data_root=tmp_path,
    )
    with pytest.raises(_Captured):
        build_session(
            rig=rig,
            subject="t01",
            session=1,
            run=1,
            task_name="test-task",
            task_params=Params(),
            event_schema=EventSchema(()),
            build_trial=lambda setup: TrialPlan(phases=[RunForFrames(1, COMPLETED)]),
            make_source=lambda params, rng: SimpleSequence(
                [Condition({"c": "a"})], n_repeats=1, rng=rng
            ),
            seed=4242,
            iti=Duration(ms=0),
            simulated_frame_period_s=0.0,
            date_yyyymmdd="20261006",
            experiment_version=TEST_EXPERIMENT.version,
        )
    assert seen["target"].appearance == "random_images"
    drawn = seen["rng"].integers(0, 10**9, 8)
    assert np.array_equal(drawn, named_stream(4242, "calibration_target").integers(0, 10**9, 8))
