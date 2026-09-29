"""Rigs by name: alhazen's shared rigs, an experiment's own, and `extends`.

Five experiment repositories each kept their own copy of the same few
machines. alhazen now ships those machines (src/alhazen/rigs/), an experiment
keeps only its own rigs or what it does differently on a shared one, and a
rig is named on the command line rather than pathed: `--rig lab`. These pin
the rules that makes work — which file a name means, what `extends` merges
and what it refuses — and that every command taking `--rig` follows them.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from alhazen.cli.main import main
from alhazen.cli.modes import run_experiment
from alhazen.config import rigs as rigs_module
from alhazen.config.loader import load_rig
from alhazen.config.rigs import (
    SHARED_RIG_DIR,
    RigRef,
    deep_merge,
    list_rigs,
    local_rig_file,
    resolve_rig,
    rig_mapping,
    rig_name,
    shared_rig_files,
)
from alhazen.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
SHARED_NAMES = ["lab", "lab-rehearsal", "laptop", "mac", "vpixx"]
MONITOR = {
    "width_px": 800,
    "height_px": 600,
    "width_cm": 40.0,
    "distance_cm": 57.0,
    "refresh_rate_hz": 120.0,
}


def write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def whole_rig() -> dict:
    """A complete, valid rig: simulated display, no devices."""
    return {"monitor": dict(MONITOR), "display": {"backend": "simulated"}, "data_root": "data"}


@pytest.fixture
def experiment(tmp_path):
    """An experiment folder whose configs/ holds its own lab (a whole file)
    and, in a subfolder, a booth; alhazen's shared rigs are the real ones."""
    root = tmp_path / "experiment"
    write_yaml(root / "configs" / "rig-lab.yaml", whole_rig())
    write_yaml(root / "configs" / "rooms" / "rig-booth.yml", whole_rig())
    return root


@pytest.fixture
def shared(tmp_path):
    """A stand-in for alhazen's shared rigs, name to file, for the rules that
    need a shared rig with particular content: a lab with an EyeLink and sync
    lines, and a mac with no devices."""
    folder = tmp_path / "shared"
    lab = write_yaml(
        folder / "rig-lab.yaml",
        {
            "monitor": {**MONITOR, "fullscreen": True},
            "display": {
                "backend": "simulated",
                "frame_qa": {"policy": "recycle_trial", "max_dropped_fraction": 0.1},
                "photodiode": {"corner": "br", "size_px": 60, "events": []},
            },
            "live_monitor": {"enabled": True, "auto_open": False},
            "devices": {
                "eyetracker": {"backend": "eyelink", "host_ip": "100.1.1.1"},
                "reward": {"backend": "simulated"},
                "sync": {"backend": "simulated", "pulse_ms": 2.0},
            },
            "data_root": "data",
        },
    )
    mac = write_yaml(folder / "rig-mac.yaml", whole_rig())
    return {"lab": lab, "mac": mac}


class TestTheSharedRigs:
    def test_alhazen_ships_the_five_machines_and_each_one_validates(self):
        files = shared_rig_files()
        assert list(files) == SHARED_NAMES
        for name, path in files.items():
            rig = load_rig(path)
            # Named after the file, as any rig file is: rig-lab registers as
            # rig-lab with PsychoPy.
            assert rig.monitor.name == f"rig-{name}"

    def test_no_shared_rig_names_an_event_so_any_task_can_run_on_it(self):
        """The sync lines and the photodiode mark events a TASK declares, and a
        session refuses a rig naming one its task never declares. A shared
        rig naming amodal-averaging's four events would be refused by every
        other experiment; the names are each experiment's to add."""
        for path in shared_rig_files().values():
            rig = load_rig(path)
            if rig.devices.sync is not None:
                assert rig.devices.sync.event_lines == {}, path.name
            if rig.display.photodiode is not None:
                assert rig.display.photodiode.events == [], path.name

    def test_no_shared_rig_extends_anything(self):
        for path in shared_rig_files().values():
            assert "extends" not in yaml.safe_load(path.read_text(encoding="utf-8")), path.name

    def test_the_laptop_rig_targets_the_laptop_panel_not_the_ultrawide(self):
        """Screen 1 on that machine is off limits; its file says a test pins it."""
        assert load_rig(shared_rig_files()["laptop"]).monitor.screen_index == 0

    def test_the_laptop_rig_describes_the_panel_in_native_pixels(self):
        """The laptop is a Windows machine at 150 % display scaling: the
        desktop calls its 2560x1440 panel 1707x960. Stimuli are placed in
        framebuffer pixels, so the file must carry the native count (a
        logical one makes every stimulus 1.5x too big), and the panel's
        165 Hz, which is not the lab's 120 Hz."""
        monitor = load_rig(shared_rig_files()["laptop"]).monitor
        assert (monitor.width_px, monitor.height_px) == (2560, 1440)
        assert monitor.refresh_rate_hz == 165.0
        assert monitor.width_cm == 38.0

    def test_the_lab_rehearsal_is_the_lab_with_its_devices_stood_down(self):
        """What the rehearsal rehearses is the lab's configuration, so the two
        may differ only in what the rehearsal file says it changes: every
        device, the display backend and frame QA, and the live monitor's
        auto-open."""
        lab = load_rig(shared_rig_files()["lab"])
        rehearsal = load_rig(shared_rig_files()["lab-rehearsal"])
        geometry = ("width_px", "height_px", "width_cm", "distance_cm", "refresh_rate_hz")
        for field in geometry:
            assert getattr(rehearsal.monitor, field) == getattr(lab.monitor, field), field
        assert rehearsal.display.photodiode == lab.display.photodiode
        assert rehearsal.display.backend == "simulated"
        assert rehearsal.devices.eyetracker.backend == "mouse_sim"
        assert rehearsal.devices.reward.backend == "simulated"
        assert rehearsal.devices.sync.backend == "simulated"
        assert rehearsal.devices.sync.pulse_ms == lab.devices.sync.pulse_ms
        assert rehearsal.data_root == lab.data_root

    def test_every_shared_rig_file_ships_in_the_wheel(self):
        """A file left out of package-data installs fine and then `--rig lab`
        cannot find it. The slow scaffold test installs the package and looks;
        this checks the pattern on every commit."""
        try:
            import tomllib
        except ModuleNotFoundError:  # 3.10
            import tomli as tomllib
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = config["tool"]["setuptools"]["package-data"]["alhazen"]
        package = ROOT / "src" / "alhazen"
        for path in SHARED_RIG_DIR.iterdir():
            relative = path.relative_to(package).as_posix()
            assert any(fnmatch.fnmatch(relative, p) for p in patterns), relative

    def test_a_missing_shared_folder_is_an_incomplete_installation(self, monkeypatch, tmp_path):
        """Not "no shared rigs", which would send the reader hunting a typo."""
        monkeypatch.setattr(rigs_module, "SHARED_RIG_DIR", tmp_path / "gone")
        with pytest.raises(ConfigError, match="installation is incomplete"):
            shared_rig_files()


class TestNames:
    @pytest.mark.parametrize(
        "spelling", ["lab", "rig-lab", "rig-lab.yaml", "rig-lab.yml", "alhazen/lab"]
    )
    def test_every_spelling_of_a_name_is_the_name(self, spelling):
        assert rig_name(spelling) == "lab"

    @pytest.mark.parametrize("spelling", ["configs/rig-lab.yaml", "..\\rig-lab.yaml", "", "rig-"])
    def test_a_path_or_nothing_is_not_a_name(self, spelling):
        assert rig_name(spelling) is None

    @pytest.mark.parametrize("spelling", ["lab", "rig-lab", "rig-lab.yaml"])
    def test_the_three_spellings_find_the_same_rig(self, experiment, spelling):
        ref = resolve_rig(spelling, experiment)
        assert ref == RigRef("lab", experiment / "configs" / "rig-lab.yaml", "experiment")

    def test_the_experiments_own_rig_comes_first(self, experiment):
        assert resolve_rig("lab", experiment).source == "experiment"

    def test_alhazen_prefix_reaches_the_shared_rig_the_experiment_shadows(self, experiment):
        ref = resolve_rig("alhazen/lab", experiment)
        assert ref == RigRef("lab", SHARED_RIG_DIR / "rig-lab.yaml", "alhazen")

    def test_a_name_the_experiment_lacks_is_the_shared_one(self, experiment):
        ref = resolve_rig("mac", experiment)
        assert (ref.source, ref.path) == ("alhazen", SHARED_RIG_DIR / "rig-mac.yaml")

    def test_rigs_in_subfolders_and_yml_files_are_found(self, experiment):
        ref = resolve_rig("booth", experiment)
        assert ref.path == experiment / "configs" / "rooms" / "rig-booth.yml"

    def test_two_experiment_files_with_one_name_are_refused_naming_both(self, experiment):
        other = write_yaml(experiment / "configs" / "old" / "rig-lab.yaml", whole_rig())
        with pytest.raises(ConfigError, match="2 rigs named 'lab'") as refused:
            resolve_rig("lab", experiment)
        assert str(experiment / "configs" / "rig-lab.yaml") in str(refused.value)
        assert str(other) in str(refused.value)
        # The path still reaches either one.
        assert resolve_rig(other, experiment).path == other

    def test_an_unknown_name_lists_every_rig_and_whose_it_is(self, experiment):
        with pytest.raises(ConfigError) as refused:
            resolve_rig("labb", experiment)
        message = str(refused.value)
        assert "no rig named 'labb'" in message
        assert f"booth            this experiment's rig, {experiment / 'configs'}" in message
        for name in SHARED_NAMES:
            assert f"{name:<16} alhazen's shared rig" in message
        # The shared lab is hidden by the experiment's, and says how to reach it.
        assert "hidden by the experiment's own lab; name it alhazen/lab" in message

    def test_an_unknown_shared_name_lists_the_shared_rigs(self):
        with pytest.raises(ConfigError, match="no shared rig named 'booth'.*lab, lab-rehearsal"):
            resolve_rig("alhazen/booth", None)

    def test_a_path_keeps_meaning_that_file_exactly(self, experiment, monkeypatch):
        """As typed — relative stays relative — so what the snapshot records
        as `sources["rig"]` is what it recorded before names existed."""
        monkeypatch.chdir(experiment)
        ref = resolve_rig("configs/rig-lab.yaml", None)
        assert ref.path == Path("configs/rig-lab.yaml")
        assert (ref.name, ref.source) == ("lab", "experiment")

    def test_a_path_to_a_shared_file_is_alhazens(self):
        ref = resolve_rig(str(SHARED_RIG_DIR / "rig-vpixx.yaml"), None)
        assert (ref.name, ref.source) == ("vpixx", "alhazen")

    def test_a_missing_path_says_so_and_what_a_name_is(self):
        with pytest.raises(ConfigError, match="config file not found: configs/rig-nope.yaml"):
            resolve_rig("configs/rig-nope.yaml", None)

    def test_the_experiment_is_found_only_when_a_name_needs_it(self, experiment):
        """Finding the experiment reads its pyproject.toml; a path or an
        alhazen/ name must work for a task that has none."""

        def unfindable():
            raise AssertionError("the experiment root was looked up")

        path = experiment / "configs" / "rig-lab.yaml"
        assert resolve_rig(path, unfindable).path == path
        assert resolve_rig("alhazen/mac", unfindable).source == "alhazen"
        assert resolve_rig("lab", lambda: experiment).source == "experiment"

    def test_with_no_experiment_only_shared_rigs_are_searched(self):
        assert resolve_rig("lab", None).source == "alhazen"
        with pytest.raises(ConfigError, match="no experiment folder was searched"):
            resolve_rig("booth", None)


class TestListing:
    def test_experiment_rigs_first_then_shared_with_the_shadowed_ones_marked(self, experiment):
        listed = list_rigs(experiment)
        assert [(r.name, r.source, r.shadowed) for r in listed] == [
            ("booth", "experiment", False),
            ("lab", "experiment", False),
            ("lab", "alhazen", True),
            ("lab-rehearsal", "alhazen", False),
            ("laptop", "alhazen", False),
            ("mac", "alhazen", False),
            ("vpixx", "alhazen", False),
        ]

    def test_a_measured_gamma_beside_a_rig_is_not_a_rig(self, experiment):
        (experiment / "configs" / "rig-lab_gamma.yaml").write_text("gamma: 2.2\n")
        assert [r.name for r in list_rigs(experiment, shared={})] == ["booth", "lab"]

    def test_the_shared_rigs_can_be_another_alhazens(self, experiment, shared):
        """The workspace lists the rigs the PROJECT's alhazen ships."""
        listed = list_rigs(experiment, shared=shared)
        assert [(r.name, r.source, r.shadowed) for r in listed][-2:] == [
            ("lab", "alhazen", True),
            ("mac", "alhazen", False),
        ]
        assert listed[-1].path == shared["mac"]


class TestExtends:
    def extending(self, root: Path, body: dict, name: str = "rig-lab.yaml") -> Path:
        return write_yaml(root / "configs" / name, {"extends": "lab", **body})

    def test_sections_merge_key_by_key(self, tmp_path, shared):
        path = self.extending(
            tmp_path,
            {
                "monitor": {"distance_cm": 45.0},
                "devices": {"eyetracker": {"accuracy_max_deg": 0.5}},
            },
        )
        rig = load_rig(path, shared_rigs=shared)
        # Changed where the experiment said so...
        assert rig.monitor.distance_cm == 45.0
        assert rig.devices.eyetracker.accuracy_max_deg == 0.5
        # ...and everything else the shared rig's, down to the leaf.
        assert rig.monitor.width_px == MONITOR["width_px"] and rig.monitor.fullscreen
        assert rig.devices.eyetracker.backend == "eyelink"
        assert rig.devices.eyetracker.host_ip == "100.1.1.1"
        assert rig.devices.reward.backend == "simulated"
        assert rig.display.frame_qa.policy == "recycle_trial"

    def test_a_list_replaces_the_shared_list_whole(self, tmp_path, shared):
        write_yaml(
            shared["lab"],
            {
                **yaml.safe_load(shared["lab"].read_text()),
                "display": {"backend": "simulated", "photodiode": {"events": ["A", "B"]}},
            },
        )
        path = self.extending(tmp_path, {"display": {"photodiode": {"events": ["STIM_ON"]}}})
        assert load_rig(path, shared_rigs=shared).display.photodiode.events == ["STIM_ON"]

    def test_event_lines_are_added_to_a_shared_rig_that_names_none(self, tmp_path, shared):
        lines = {"STIM_ON": "Dev1/port0/line0", "GO_CUE": "Dev1/port0/line1"}
        path = self.extending(tmp_path, {"devices": {"sync": {"event_lines": lines}}})
        sync = load_rig(path, shared_rigs=shared).devices.sync
        assert sync.event_lines == lines and sync.pulse_ms == 2.0

    def test_a_null_removes_what_the_shared_rig_has(self, tmp_path, shared):
        path = self.extending(tmp_path, {"devices": {"reward": None, "sync": None}})
        rig = load_rig(path, shared_rigs=shared)
        assert rig.devices.reward is None and rig.devices.sync is None
        assert rig.devices.eyetracker is not None

    def test_an_empty_section_is_refused_because_merged_it_would_change_nothing(
        self, tmp_path, shared
    ):
        """`devices: {}` in a whole file means no devices; merged it would keep
        every device the shared rig has — the opposite, silently."""
        path = self.extending(tmp_path, {"devices": {"sync": {}}})
        with pytest.raises(ConfigError, match=r"`devices\.sync: \{\}` in a rig that extends"):
            load_rig(path, shared_rigs=shared)

    def test_the_merged_rig_is_validated_naming_both_files(self, tmp_path, shared):
        path = self.extending(tmp_path, {"devices": {"eyetracker": {"eye": "left"}}})
        with pytest.raises(ConfigError) as refused:
            load_rig(path, shared_rigs=shared)
        message = str(refused.value)
        assert str(path) in message and str(shared["lab"]) in message
        assert "extends alhazen's shared rig 'lab'" in message

    def test_an_unknown_setting_in_the_experiments_file_is_still_a_typo(self, tmp_path, shared):
        path = self.extending(tmp_path, {"monitor": {"distnce_cm": 45.0}})
        with pytest.raises(ConfigError, match="distnce_cm"):
            load_rig(path, shared_rigs=shared)

    @pytest.mark.parametrize("spelling", ["lab", "rig-lab", "rig-lab.yaml", "alhazen/lab"])
    def test_extends_takes_every_spelling_of_the_name(self, tmp_path, shared, spelling):
        path = write_yaml(tmp_path / "configs" / "rig-x.yaml", {"extends": spelling})
        assert rig_mapping(path, shared=shared).extends == "lab"

    def test_an_unknown_shared_rig_is_refused_listing_the_shared_ones(self, tmp_path, shared):
        path = write_yaml(tmp_path / "configs" / "rig-x.yaml", {"extends": "booth"})
        with pytest.raises(ConfigError, match=r"not one of alhazen's shared rigs \(lab, mac\)"):
            load_rig(path, shared_rigs=shared)

    def test_an_experiment_rig_cannot_be_extended(self, experiment):
        """booth is the experiment's own rig, not a shared one."""
        path = write_yaml(experiment / "configs" / "rig-x.yaml", {"extends": "booth"})
        with pytest.raises(ConfigError, match="Only a shared rig can be extended"):
            load_rig(path)

    def test_a_shared_rig_that_says_extends_is_refused(self, tmp_path, shared):
        write_yaml(shared["mac"], {"extends": "lab"})
        with pytest.raises(ConfigError, match="shared rig is a whole file"):
            load_rig(shared["mac"], shared_rigs=shared)
        # Nor can an experiment build a chain through it.
        path = write_yaml(tmp_path / "configs" / "rig-mac.yaml", {"extends": "mac"})
        with pytest.raises(ConfigError, match="shared rigs extend nothing"):
            load_rig(path, shared_rigs=shared)

    def test_extends_must_be_a_name(self, tmp_path, shared):
        path = write_yaml(tmp_path / "configs" / "rig-x.yaml", {"extends": ["lab"]})
        with pytest.raises(ConfigError, match="must name one of alhazen's shared rigs"):
            load_rig(path, shared_rigs=shared)

    def test_the_monitor_is_named_after_the_experiments_file(self, tmp_path, shared):
        """rig-booth.yaml extending lab is the booth: registered as rig-booth."""
        path = self.extending(tmp_path, {}, name="rig-booth.yaml")
        assert load_rig(path, shared_rigs=shared).monitor.name == "rig-booth"
        named = self.extending(tmp_path, {"monitor": {"name": "booth-2"}}, name="rig-b2.yaml")
        assert load_rig(named, shared_rigs=shared).monitor.name == "booth-2"

    def test_the_real_shared_lab_extended_the_way_amodal_averaging_would(self, tmp_path):
        """The reference experiment's lab rig as a whole file, and as the
        shared lab extended by what is its own: the same rig."""
        own = {
            "display": {"photodiode": {"events": ["STIM_ON"]}},
            "devices": {
                "eyetracker": {"edf_host_filename": "amodal.EDF", "accuracy_max_deg": 0.5},
                "sync": {
                    "event_lines": {
                        "STIM_ON": "Dev1/port0/line0",
                        "GO_CUE": "Dev1/port0/line1",
                        "SACCADE_ONSET": "Dev1/port0/line2",
                        "LANDED": "Dev1/port0/line3",
                    }
                },
            },
        }
        extended = write_yaml(tmp_path / "a" / "rig-lab.yaml", {"extends": "lab", **own})
        whole = deep_merge(yaml.safe_load((SHARED_RIG_DIR / "rig-lab.yaml").read_text()), own)
        written = write_yaml(tmp_path / "b" / "rig-lab.yaml", whole)
        assert load_rig(extended) == load_rig(written)

    def test_merging_changes_neither_input(self):
        base = {"a": {"b": 1, "c": [1]}}
        override = {"a": {"b": 2}}
        assert deep_merge(base, override) == {"a": {"b": 2, "c": [1]}}
        assert base == {"a": {"b": 1, "c": [1]}} and override == {"a": {"b": 2}}


class TestWhereMeasurementsOfASharedRigGo:
    def test_beside_the_experiments_own_file_of_that_name(self, experiment):
        mac = resolve_rig("mac", experiment)
        assert local_rig_file(mac, experiment) == experiment / "configs" / "rig-mac.yaml"
        own = resolve_rig("lab", experiment)
        assert local_rig_file(own, None) == own.path

    def test_refused_where_there_is_no_experiment_configs_folder(self, tmp_path):
        with pytest.raises(ConfigError, match="configs/ folder instead — and there is none"):
            local_rig_file(resolve_rig("mac", None), tmp_path)


class TestTheCommandLine:
    def test_rigs_lists_every_rig_whose_it_is_and_what_it_extends(
        self, experiment, capsys, monkeypatch
    ):
        write_yaml(experiment / "configs" / "rig-vpixx.yaml", {"extends": "vpixx"})
        monkeypatch.chdir(experiment)
        assert main(["rigs"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines[2].split() == ["NAME", "SOURCE", "FILE", "NOTE"]
        rows = [line.split(None, 3) for line in lines[3:10]]
        assert [row[:3] for row in rows] == [
            ["booth", "experiment", "configs/rooms/rig-booth.yml"],
            ["lab", "experiment", "configs/rig-lab.yaml"],
            ["vpixx", "experiment", "configs/rig-vpixx.yaml"],
            ["lab", "alhazen", "rig-lab.yaml"],
            ["lab-rehearsal", "alhazen", "rig-lab-rehearsal.yaml"],
            ["laptop", "alhazen", "rig-laptop.yaml"],
            ["mac", "alhazen", "rig-mac.yaml"],
        ]
        assert rows[2][3] == "extends alhazen/vpixx"
        assert rows[3][3] == (
            "shadowed by the experiment's lab (reach this one with --rig alhazen/lab)"
        )
        assert str(SHARED_RIG_DIR) in out

    def test_rigs_takes_the_project_folder(self, experiment, capsys):
        assert main(["rigs", "--project", str(experiment)]) == 0
        assert "configs/rooms/rig-booth.yml" in capsys.readouterr().out

    def test_rigs_exits_nonzero_on_a_rig_a_name_would_refuse(self, experiment, capsys):
        write_yaml(experiment / "configs" / "old" / "rig-lab.yaml", whole_rig())
        (experiment / "configs" / "rig-bad.yaml").write_text("extends: [\n")
        assert main(["rigs", "--project", str(experiment)]) == 1
        out = capsys.readouterr().out
        assert "DUPLICATE NAME: --rig lab is refused until one is renamed" in out
        assert "UNREADABLE: invalid YAML in" in out

    def test_rigs_refuses_a_folder_that_is_not_there(self, tmp_path, capsys):
        assert main(["rigs", "--project", str(tmp_path / "nowhere")]) == 1
        assert "CANNOT LIST RIGS" in capsys.readouterr().err

    def test_validate_takes_a_name_and_says_whose_rig_it_found(
        self, experiment, capsys, monkeypatch
    ):
        write_yaml(experiment / "configs" / "rig-mac.yaml", {"extends": "mac"})
        monkeypatch.chdir(experiment)
        assert main(["validate", "--rig", "mac"]) == 0
        out = capsys.readouterr().out
        assert "OK: mac (" in out and "rig-mac.yaml), extends alhazen/mac" in out
        assert main(["validate", "--rig", "alhazen/mac"]) == 0
        assert "OK: alhazen/mac (alhazen's shared rig" in capsys.readouterr().out
        assert main(["validate", "--rig", "nope"]) == 1
        assert "no rig named 'nope'" in capsys.readouterr().err

    def test_check_rig_runs_the_shared_rehearsal_by_name(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["check-rig", "--rig", "lab-rehearsal", "--pulse"]) == 0
        out = capsys.readouterr().out
        assert "OK   reward:" in out and "OK   sync:" in out

    def test_every_rig_flag_says_it_takes_a_name(self, capsys):
        for command in (
            ["validate"],
            ["check-rig"],
            ["run"],
            ["calibrate", "ruler"],
            ["calibrate", "gamma"],
            ["monitor", "register"],
            ["monitor", "show"],
        ):
            with pytest.raises(SystemExit):
                main([*command, "--help"])
            text = " ".join(capsys.readouterr().out.split())
            assert "a name, such as lab" in text, command

    def test_measure_mode_keeps_a_shared_rigs_report_in_the_experiment(
        self, experiment, capsys, monkeypatch
    ):
        from alhazen.modes import measure

        measured = []

        def fake_run(rig, rig_path, **kwargs):
            measured.append(rig_path)
            return measure.MeasurementReport(rig_path=rig_path)

        monkeypatch.setattr(measure, "run_measurements", fake_run)
        monkeypatch.chdir(experiment)
        assert main(["run", "--mode", "measure", "--rig", "mac"]) == 0
        assert measured == [str(SHARED_RIG_DIR / "rig-mac.yaml")]
        assert list((experiment / "configs" / "measurements").glob("rig-mac_*.json"))
        assert not (SHARED_RIG_DIR / "measurements").exists()

    def test_measure_mode_refuses_a_shared_rig_with_nowhere_to_keep_it_before_measuring(
        self, tmp_path, capsys, monkeypatch
    ):
        from alhazen.modes import measure

        monkeypatch.setattr(
            measure,
            "run_measurements",
            lambda *a, **k: pytest.fail("measured with nowhere to keep the report"),
        )
        monkeypatch.chdir(tmp_path)
        assert main(["run", "--mode", "measure", "--rig", "mac"]) == 1
        assert "CANNOT MEASURE" in capsys.readouterr().err

    def test_calibrate_gamma_keeps_a_shared_rigs_fit_in_the_experiment(
        self, experiment, capsys, monkeypatch
    ):
        csv = experiment / "gamma.csv"
        csv.write_text("level,luminance\n0,0.5\n0.25,6\n0.5,25\n0.75,60\n1,110\n")
        monkeypatch.chdir(experiment)
        assert main(["calibrate", "gamma", "--rig", "mac", "--measurements", str(csv)]) == 0
        fit = experiment / "configs" / "rig-mac_gamma.yaml"
        assert f"written: {fit}" in capsys.readouterr().out
        assert not (SHARED_RIG_DIR / "rig-mac_gamma.yaml").exists()
        # And the fit is not mistaken for a rig.
        assert "mac_gamma" not in [ref.name for ref in list_rigs(experiment)]


class TestSessionsStartedByName:
    """run.py and `alhazen run` look a name up in the experiment the task
    belongs to, and record which rig ran."""

    @pytest.fixture
    def task_in_experiment(self, experiment, monkeypatch):
        """A task whose module lives in `experiment`, beside its
        pyproject.toml — how find_experiment knows the experiment's folder."""
        (experiment / "pyproject.toml").write_text(
            '[project]\nname = "rig-demo"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        module = experiment / "rig_demo_task.py"
        module.write_text(
            "from test_task_hooks import SilentTask\n\n"
            "class RigDemoTask(SilentTask):\n    name = 'rig-demo'\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location("rig_demo_task", module)
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "rig_demo_task", loaded)
        spec.loader.exec_module(loaded)
        return loaded.RigDemoTask

    def test_run_py_finds_the_experiments_rig_from_another_folder(
        self, task_in_experiment, experiment, tmp_path, monkeypatch
    ):
        seen = {}

        def spy(args, rig, task, params, mode):
            seen["args"] = args
            return 0

        monkeypatch.setattr(sys.modules["alhazen.cli.main"], "_trial_session", spy)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        code = run_experiment(
            task_class=task_in_experiment,
            default_rig="lab",
            argv=["--mode", "simulate", "--headless"],
        )
        assert code == 0
        args = seen["args"]
        assert args.rig == str(experiment / "configs" / "rig-lab.yaml")
        assert (args.rig_ref.name, args.rig_ref.source) == ("lab", "experiment")

    def test_a_session_on_a_shared_rig_records_its_name_and_source(
        self, task_in_experiment, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        code = run_experiment(
            task_class=task_in_experiment,
            default_rig="alhazen/lab-rehearsal",
            # The shared rehearsal rig turns the live monitor on, whose child
            # process is no part of what this checks.
            argv=["--mode", "simulate", "--headless", "--no-live-monitor"],
        )
        assert code == 0, capsys.readouterr().err
        snapshot = next((tmp_path / "data-rehearsal").rglob("config_snapshot.yaml"))
        sources = yaml.safe_load(snapshot.read_text(encoding="utf-8"))["config"]["sources"]
        assert sources["rig"] == str(SHARED_RIG_DIR / "rig-lab-rehearsal.yaml")
        assert sources["rig_name"] == "lab-rehearsal"
        assert sources["rig_source"] == "alhazen"
