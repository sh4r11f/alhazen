"""Config models, loader, snapshot: loud validation and reproducible provenance."""

from __future__ import annotations

import re

import pytest
import yaml

from alhazen.config.loader import load_model, load_rig
from alhazen.config.models import (
    DevicesConfig,
    Duration,
    EyeTrackerConfig,
    MonitorConfig,
    PhotodiodeConfig,
    RewardPulses,
    RigConfig,
    SessionInfo,
    SyncHwConfig,
    resolve_refresh,
)
from alhazen.config.snapshot import environment_digest, write_snapshot
from alhazen.errors import ConfigError
from support import make_session_config


class TestDuration:
    def test_exactly_one_required(self):
        with pytest.raises(ValueError, match="exactly one"):
            Duration()
        with pytest.raises(ValueError, match="exactly one"):
            Duration(ms=10, frames=2)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            Duration(ms=-1)
        with pytest.raises(ValueError):
            Duration(frames=-1)

    def test_seconds_from_both_units(self):
        assert Duration(ms=500).seconds(60.0) == 0.5
        assert Duration(frames=30).seconds(60.0) == 0.5

    def test_frames_resolution_rounds_once(self):
        assert Duration(frames=12).n_frames(60.0) == 12
        assert Duration(ms=200).n_frames(60.0) == 12
        assert Duration(ms=205).n_frames(60.0) == 12  # 12.3 -> nearest
        assert Duration(ms=225).n_frames(60.0) == 14  # 13.5 -> banker's nearest even


class TestModels:
    def test_monitor_requires_positive_geometry(self):
        with pytest.raises(ValueError, match="width_cm"):
            MonitorConfig(
                width_px=100, height_px=100, width_cm=0, distance_cm=60, refresh_rate_hz=60
            )

    def test_unknown_keys_are_errors(self):
        with pytest.raises(ValueError):
            MonitorConfig(
                width_px=100,
                height_px=100,
                width_cm=30,
                distance_cm=60,
                refresh_rate_hz=60,
                refresh=60,  # typo'd duplicate must not silently pass
            )

    def test_session_info_validation(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            SessionInfo(subject="a/b", session=1, run=1, task_name="t", seed=1)
        with pytest.raises(ValueError, match="lowercase"):
            SessionInfo(subject="s1", session=1, run=1, task_name="Task", seed=1)
        with pytest.raises(ValueError, match="session"):
            SessionInfo(subject="s1", session=0, run=1, task_name="t", seed=1)

    def test_configs_frozen(self):
        info = SessionInfo(subject="s1", session=1, run=1, task_name="t", seed=1)
        with pytest.raises(ValueError):
            info.subject = "other"  # type: ignore[misc]


class TestDeviceModels:
    def test_a_rig_without_devices_has_none_of_them(self, tmp_path):
        # A rig file may name no devices, and "absent" is spelled None
        # rather than a disabled-but-present backend.
        rig = RigConfig(
            monitor=MonitorConfig(
                width_px=100, height_px=100, width_cm=30, distance_cm=60, refresh_rate_hz=60
            ),
            data_root=tmp_path,
        )
        assert rig.devices == DevicesConfig()
        assert rig.devices.eyetracker is None

    def test_edf_filename_must_be_8_3(self):
        # The EyeLink Host PC writes to an 8.3 filesystem and rejects a longer
        # name at file-open time — i.e. with the subject already in the rig.
        for bad in ("toolongname.EDF", "has space.EDF", "alhazen.edfx", "alhazen"):
            with pytest.raises(ValueError, match="8.3"):
                EyeTrackerConfig(backend="eyelink", edf_host_filename=bad)

    def test_edf_filename_accepts_a_valid_name(self):
        assert EyeTrackerConfig(backend="eyelink").edf_host_filename == "alhazen.EDF"
        EyeTrackerConfig(backend="eyelink", edf_host_filename="sub01.EDF")

    def test_calibration_area_is_a_fraction(self):
        with pytest.raises(ValueError, match="calibration_area"):
            EyeTrackerConfig(backend="eyelink", calibration_area=1.5)

    def test_unknown_backend_is_rejected(self):
        with pytest.raises(ValueError):
            EyeTrackerConfig(backend="tobii")

    def test_both_real_trackers_are_selectable(self):
        # The whole point of the backend field: one rig file, one word
        # changed, and the session runs on the other tracker.
        assert EyeTrackerConfig(backend="eyelink").backend == "eyelink"
        assert EyeTrackerConfig(backend="viewpixx").backend == "viewpixx"

    def test_a_viewpixx_rig_is_not_asked_for_an_edf_name(self):
        # The 8.3 rule is the EyeLink Host PC's filesystem, and a TRACKPixx3
        # has neither. The default must not be validated against a rule that
        # does not apply to the configured backend.
        EyeTrackerConfig(backend="viewpixx")
        EyeTrackerConfig(backend="mouse_sim")

    def test_a_field_the_chosen_backend_ignores_is_an_error(self):
        # Not a harmless extra: whoever typed it believes they configured
        # something, and nothing at runtime would ever tell them otherwise.
        with pytest.raises(ValueError, match="ignores host_ip"):
            EyeTrackerConfig(backend="viewpixx", host_ip="100.1.1.1")
        with pytest.raises(ValueError, match="ignores eye"):
            EyeTrackerConfig(backend="eyelink", eye="right")
        with pytest.raises(ValueError, match="ignores led_intensity"):
            EyeTrackerConfig(backend="mouse_sim", led_intensity=4)

    def test_defaults_that_do_not_apply_stay_silent(self):
        # Only keys the file actually supplied are checked — otherwise every
        # viewpixx rig would trip over the EyeLink's default host_ip.
        cfg = EyeTrackerConfig(backend="viewpixx")
        assert cfg.host_ip == "100.1.1.1"  # present, defaulted, and unused

    def test_viewpixx_calibration_type_must_be_one_alhazen_can_lay_out(self):
        # The TRACKPixx3 has no Host PC to own a target grid, so alhazen draws
        # it — and can only honour layouts it knows. Rejected at load time,
        # not when the calibrate key is pressed with a subject in the chair.
        with pytest.raises(ValueError, match="calibration_type"):
            EyeTrackerConfig(backend="viewpixx", calibration_type="HV3")
        for good in ("HV5", "HV9", "HV13"):
            EyeTrackerConfig(backend="viewpixx", calibration_type=good)

    def test_the_eyelink_keeps_its_own_calibration_types(self):
        # Its Host PC owns the grid, so alhazen never enumerates the points
        # and must not narrow what the tracker itself accepts.
        assert EyeTrackerConfig(backend="eyelink", calibration_type="HV3").calibration_type == "HV3"

    def test_led_intensity_is_within_the_illuminator_range(self):
        with pytest.raises(ValueError, match="led_intensity"):
            EyeTrackerConfig(backend="viewpixx", led_intensity=0)
        with pytest.raises(ValueError, match="led_intensity"):
            EyeTrackerConfig(backend="viewpixx", led_intensity=9)
        assert EyeTrackerConfig(backend="viewpixx", led_intensity=8).led_intensity == 8

    def test_the_eye_a_binocular_tracker_reports_is_stated_not_guessed(self):
        assert EyeTrackerConfig(backend="viewpixx").eye == "left"
        assert EyeTrackerConfig(backend="viewpixx", eye="average").eye == "average"
        with pytest.raises(ValueError):
            EyeTrackerConfig(backend="viewpixx", eye="both")

    def test_sync_needs_a_positive_pulse_width(self):
        with pytest.raises(ValueError, match="pulse_ms"):
            SyncHwConfig(backend="simulated", pulse_ms=0)

    def test_sync_lines_default_to_nothing_wired(self):
        assert SyncHwConfig(backend="none").event_lines == {}

    def test_reward_pulses_reject_negative_timing(self):
        for kwargs in ({"n_pulses": -1}, {"pulse_ms": -1}, {"inter_pulse_ms": -1}):
            with pytest.raises(ValueError, match="must be >= 0"):
                RewardPulses(**kwargs)

    def test_photodiode_needs_a_visible_patch(self):
        with pytest.raises(ValueError, match="size_px"):
            PhotodiodeConfig(size_px=0)


class TestResolveRefresh:
    def test_measured_within_tolerance_wins(self):
        assert resolve_refresh(60.0, 59.8, 5.0) == 59.8

    def test_divergence_is_loud(self):
        with pytest.raises(ConfigError, match="disagrees"):
            resolve_refresh(240.0, 60.1, 5.0)


class TestLoader:
    def test_missing_file_named(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_rig(tmp_path / "nope.yaml")

    def test_invalid_yaml_named(self, tmp_path):
        path = tmp_path / "rig.yaml"
        path.write_text("monitor: [unclosed")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_rig(path)

    def test_validation_error_names_file(self, tmp_path):
        path = tmp_path / "rig.yaml"
        path.write_text("data_root: data\n")  # monitor missing
        with pytest.raises(ConfigError, match=re.escape(str(path))):
            load_rig(path)

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "rig.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "monitor": {
                        "width_px": 800,
                        "height_px": 600,
                        "width_cm": 40,
                        "distance_cm": 57,
                        "refresh_rate_hz": 120,
                    },
                    "display": {"backend": "simulated"},
                    "data_root": "data",
                }
            )
        )
        rig = load_rig(path)
        assert rig.monitor.refresh_rate_hz == 120
        assert rig.display.backend == "simulated"

    def test_experiment_params_same_treatment(self, tmp_path):
        from alhazen.config.models import Model

        class MyParams(Model):
            n_trials: int

        path = tmp_path / "task.yaml"
        path.write_text("n_trials: 5\nn_trails: 6\n")  # typo must be loud
        with pytest.raises(ConfigError, match=re.escape(str(path))):
            load_model(path, MyParams)


class TestSnapshot:
    def test_contents(self, tmp_path):
        cfg = make_session_config(tmp_path)
        path = tmp_path / "config_snapshot.yaml"
        write_snapshot(cfg, path)
        snap = yaml.safe_load(path.read_text())
        assert snap["config"]["info"]["seed"] == 7
        assert snap["config"]["rig"]["monitor"]["width_px"] == 1920
        prov = snap["provenance"]
        assert set(prov) == {
            "created",
            "alhazen_version",
            "python",
            "platform",
            "experiment_git_sha",
            "environment_digest",
        }

    def test_environment_digest_stable_within_process(self):
        assert environment_digest() == environment_digest()
