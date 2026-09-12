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
    FrameQAConfig,
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


class TestFrameQAPolicyConfig:
    """A threshold the policy never reads is a config error, not a default.

    ``max_dropped_per_trial: 3`` under ``mark_trial`` sat in a rig file
    reading like a tolerance and did nothing at all; the analysis then
    excluded on any drop and emptied two thirds of its design cells.
    """

    def test_defaults_carry_no_inert_threshold(self):
        cfg = FrameQAConfig()
        assert cfg.policy == "warn"
        assert cfg.max_dropped_per_trial == 3 and cfg.max_dropped_fraction == pytest.approx(0.1)

    def test_a_budget_under_a_policy_that_ignores_it_is_refused(self):
        with pytest.raises(
            ValueError, match="max_dropped_per_trial only applies under policy 'abort_run'"
        ):
            FrameQAConfig(policy="mark_trial", max_dropped_per_trial=3)
        with pytest.raises(
            ValueError, match="max_dropped_fraction only applies under policy 'recycle_trial'"
        ):
            FrameQAConfig(policy="abort_run", max_dropped_fraction=0.2)

    def test_each_threshold_is_accepted_by_its_own_policy(self):
        assert FrameQAConfig(policy="abort_run", max_dropped_per_trial=0).max_dropped_per_trial == 0
        recycle = FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.25)
        assert recycle.max_dropped_fraction == pytest.approx(0.25)

    def test_the_fraction_is_a_fraction(self):
        with pytest.raises(ValueError, match="max_dropped_fraction must be in"):
            FrameQAConfig(policy="recycle_trial", max_dropped_fraction=1.0)
        with pytest.raises(ValueError, match="max_dropped_fraction must be in"):
            FrameQAConfig(policy="recycle_trial", max_dropped_fraction=0.0)

    def test_the_same_rule_holds_from_yaml(self, tmp_path):
        path = tmp_path / "rig.yaml"
        path.write_text(
            "monitor: {width_px: 100, height_px: 100, width_cm: 30, distance_cm: 60, "
            "refresh_rate_hz: 60}\n"
            "display: {backend: simulated, frame_qa: {policy: mark_trial, "
            "max_dropped_per_trial: 3}}\n"
            f"data_root: {tmp_path.as_posix()}\n"
        )
        with pytest.raises(ConfigError, match="max_dropped_per_trial only applies"):
            load_rig(path)


class TestTheMonitorIsNamedAfterTheRigFile:
    """PsychoPy looks a panel up by name, so two rig files sharing the
    default would share one registration and overwrite each other's
    geometry. A rig file is one machine, and its stem is the name nobody
    has to think of."""

    def rig(self, tmp_path, name, extra=""):
        path = tmp_path / name
        path.write_text(
            "monitor: {width_px: 100, height_px: 100, width_cm: 30, distance_cm: 60, "
            f"refresh_rate_hz: 60{extra}}}\n"
            f"data_root: {tmp_path.as_posix()}\n"
        )
        return path

    def test_an_unnamed_monitor_takes_the_files_stem(self, tmp_path):
        assert load_rig(self.rig(tmp_path, "rig-vpixx.yaml")).monitor.name == "rig-vpixx"

    def test_a_named_monitor_keeps_its_name(self, tmp_path):
        rig = load_rig(self.rig(tmp_path, "rig-vpixx.yaml", extra=", name: lab-panel"))
        assert rig.monitor.name == "lab-panel"

    def test_a_config_built_in_code_keeps_the_default(self):
        assert (
            MonitorConfig(
                width_px=100, height_px=100, width_cm=30, distance_cm=60, refresh_rate_hz=60
            ).name
            == "alhazen"
        )


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

    def test_the_iris_size_is_a_viewpixx_setting_within_the_image(self):
        assert EyeTrackerConfig(backend="viewpixx").iris_size_px is None
        assert EyeTrackerConfig(backend="viewpixx", iris_size_px=512).iris_size_px == 512
        with pytest.raises(ValueError, match="iris_size_px"):
            EyeTrackerConfig(backend="viewpixx", iris_size_px=0)
        with pytest.raises(ValueError, match="iris_size_px"):
            EyeTrackerConfig(backend="viewpixx", iris_size_px=513)
        with pytest.raises(ValueError, match="ignores iris_size_px"):
            EyeTrackerConfig(backend="eyelink", iris_size_px=100)

    def test_the_eye_a_binocular_tracker_reports_is_stated_not_guessed(self):
        assert EyeTrackerConfig(backend="viewpixx").eye == "left"
        assert EyeTrackerConfig(backend="viewpixx", eye="average").eye == "average"
        with pytest.raises(ValueError):
            EyeTrackerConfig(backend="viewpixx", eye="both")

    def test_the_procedure_fields_are_shared_by_every_backend(self):
        # How a calibration advances, whether a validation follows it, and
        # the two accuracy limits are about the *procedure*, which alhazen
        # runs the same way on every tracker — so no backend may reject them.
        for backend in ("eyelink", "viewpixx", "mouse_sim"):
            cfg = EyeTrackerConfig(
                backend=backend,
                calibration_advance="auto",
                validate_after_calibration=False,
                accuracy_max_deg=0.75,
                drift_max_deg=2.0,
            )
            assert cfg.calibration_advance == "auto"
            assert cfg.validate_after_calibration is False
            assert (cfg.accuracy_max_deg, cfg.drift_max_deg) == (0.75, 2.0)

    def test_the_procedure_defaults_are_manual_and_validated(self):
        # Manual: an experimenter watching beats a heuristic guessing when
        # the subject is on the target. Validated: a calibration that is
        # never measured is a calibration nobody knows the quality of.
        cfg = EyeTrackerConfig(backend="viewpixx")
        assert cfg.calibration_advance == "manual"
        assert cfg.validate_after_calibration is True
        assert cfg.accuracy_max_deg == 1.0
        assert cfg.drift_max_deg == 3.0

    def test_the_advance_mode_is_one_of_two_words(self):
        with pytest.raises(ValueError):
            EyeTrackerConfig(backend="viewpixx", calibration_advance="automatic")

    def test_the_accuracy_limits_must_be_positive(self):
        with pytest.raises(ValueError, match="accuracy_max_deg must be > 0"):
            EyeTrackerConfig(backend="eyelink", accuracy_max_deg=0.0)
        with pytest.raises(ValueError, match="drift_max_deg must be > 0"):
            EyeTrackerConfig(backend="eyelink", drift_max_deg=-1.0)

    def test_the_camera_image_is_a_viewpixx_switch(self):
        # Only the TRACKPixx3 hands alhazen its camera image; the EyeLink's
        # lives on the Host PC. So the switch is viewpixx-only, and on by
        # default there.
        assert EyeTrackerConfig(backend="viewpixx").camera_image is True
        assert EyeTrackerConfig(backend="viewpixx", camera_image=False).camera_image is False
        with pytest.raises(ValueError, match="ignores camera_image"):
            EyeTrackerConfig(backend="eyelink", camera_image=False)

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
            "alhazen_git_describe",
            "python",
            "platform",
            "experiment_git_sha",
            "environment_digest",
        }

    def test_the_version_recorded_is_alhazens_own(self, tmp_path):
        """It was `unknown` in every snapshot alhazen had ever written. The
        lookup used the bare distribution name, which belongs to an unrelated
        project on PyPI: on a machine with that project installed it stamped
        their version into the data, and on one without it stamped nothing at
        all. A provenance file whose one job is to say what produced the run
        said it did not know."""
        from alhazen.version import get_version

        cfg = make_session_config(tmp_path)
        path = tmp_path / "config_snapshot.yaml"
        write_snapshot(cfg, path)
        prov = yaml.safe_load(path.read_text())["provenance"]

        assert prov["alhazen_version"] == get_version()
        assert prov["alhazen_version"] != "unknown"

    def test_alhazens_own_tree_is_recorded_beside_the_experiments(self, tmp_path):
        """A version number does not identify alhazen's code between
        releases: `main` carries the last release's number until the next one
        is cut, so several different trees share it. The describe string does
        identify it — and this test runs from a checkout, so it gets one."""
        cfg = make_session_config(tmp_path)
        path = tmp_path / "config_snapshot.yaml"
        write_snapshot(cfg, path)
        prov = yaml.safe_load(path.read_text())["provenance"]

        described = prov["alhazen_git_describe"]
        assert described not in ("unknown", "not a source checkout"), described
        # A commit, a tag, or a tag plus how far past it — never empty, and
        # `-dirty` when the tree has uncommitted changes.
        assert described.strip() == described and described

    def test_a_directory_outside_any_repository_is_not_a_source_checkout(self, tmp_path):
        """The three answers have to stay apart. "Not a source checkout" means
        the version number alone identifies the code, which is what a wheel
        install looks like; "unknown" means nobody could tell. Reporting the
        second for the first would make a released install look broken."""
        from alhazen.config.snapshot import _alhazen_git_describe

        assert _alhazen_git_describe(tmp_path) == "not a source checkout"

    def test_a_directory_git_cannot_open_is_unknown_not_a_verdict(self, tmp_path):
        """git refusing to look is not git saying "no repository here". Only
        the second earns the label that means the version alone identifies the
        code; anything else is an honest unknown."""
        from alhazen.config.snapshot import _alhazen_git_describe

        assert _alhazen_git_describe(tmp_path / "no" / "such" / "directory") == "unknown"

    def test_a_shallow_clone_of_alhazen_is_described_by_its_commit(self, tmp_path):
        """Experiment CI clones alhazen with --depth 1, which brings no tags.
        That is exactly the clone-of-main case this key exists for, so it must
        name the commit rather than fall back to either label."""
        import subprocess

        from alhazen.config.snapshot import _alhazen_git_describe

        origin = tmp_path / "origin"
        (origin / "src" / "alhazen").mkdir(parents=True)
        (origin / "src" / "alhazen" / "__init__.py").write_text("", encoding="utf-8")
        (origin / "pyproject.toml").write_text(
            """[project]
name = "alhazen-vision"
""",
            encoding="utf-8",
        )

        def git(cwd, *args):
            return subprocess.run(
                ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git(origin, "init", "-q")
        git(origin, "add", ".")
        git(origin, "commit", "-q", "-m", "first")
        git(origin, "tag", "-a", "v0.0.1", "-m", "a tag the shallow clone will not have")
        (origin / "NOTES").write_text("second commit", encoding="utf-8")
        git(origin, "add", "NOTES")
        git(origin, "commit", "-q", "-m", "second")
        head = git(origin, "rev-parse", "--short", "HEAD")

        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", origin.as_uri(), str(clone)],
            capture_output=True,
            check=True,
        )

        assert _alhazen_git_describe(clone / "src" / "alhazen") == head

    def test_a_wheel_inside_an_experiments_repository_is_not_given_its_commit(self, tmp_path):
        """A virtualenv inside an experiment repo puts an installed alhazen
        inside that repo's work tree, and git describes it without complaint:
        on a scratch repo, the experiment's own tag came back as alhazen's.
        Recording someone else's commit under alhazen's name is the
        attribution bug this key exists to fix, so the tree has to prove it
        is alhazen's own before its description is believed."""
        import subprocess

        from alhazen.config.snapshot import _alhazen_git_describe

        repo = tmp_path / "experiment"
        installed = repo / ".venv" / "Lib" / "site-packages" / "alhazen" / "config"
        installed.mkdir(parents=True)
        # An experiment that DEPENDS on alhazen, so the one line that names
        # alhazen-vision in its pyproject is a dependency, not a declaration.
        (repo / "pyproject.toml").write_text(
            """[project]
name = "some-experiment"
dependencies = ["alhazen-vision>=1.3"]
"""
        )

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                capture_output=True,
                check=True,
            )

        git("init", "-q")
        git("add", "pyproject.toml")
        git("commit", "-q", "-m", "experiment")
        git("tag", "-a", "v9.9.9", "-m", "the experiment's own release")

        assert _alhazen_git_describe(installed) == "not a source checkout"

    def test_environment_digest_stable_within_process(self):
        assert environment_digest() == environment_digest()
