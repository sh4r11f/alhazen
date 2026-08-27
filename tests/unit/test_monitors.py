"""Registering a rig's monitor with PsychoPy.

The default suite has no psychopy (CI installs no renderer), so most of this
runs against a fake ``psychopy.monitors`` — injected into ``sys.modules`` the
same way test_devices_viewpixx.py fakes pypixxlib. The fake stores one JSON
file per monitor in a temp folder, which is the behaviour alhazen's code
actually depends on.

Because a fake can drift from the library it imitates, one test at the bottom
round-trips through the REAL psychopy and is skipped when it is not installed:
that is what catches a renamed method or a changed file layout.
"""

from __future__ import annotations

import json
import sys
import types

import pytest
import yaml

from alhazen.cli.main import main
from alhazen.config.models import MonitorConfig
from alhazen.display import monitors as registry
from alhazen.errors import DisplayError

MONITOR = MonitorConfig(
    width_px=1920,
    height_px=1080,
    width_cm=52.0,
    distance_cm=57.0,
    refresh_rate_hz=120.0,
    name="rig-a",
)


class FakeMonitor:
    """The slice of ``psychopy.monitors.Monitor`` alhazen uses.

    Faithful in the two ways that matter: constructing one loads any stored
    record (so ``register`` updates rather than replaces), and the getters
    raise KeyError for a field that was never stored — which is what the real
    ones do, and why ``monitors._maybe`` exists.
    """

    def __init__(self, name, verbose=True, autoLog=True, **kwargs):  # noqa: N803 - psychopy's name
        self.name = name
        self.folder = FAKE.folder
        path = self.folder / f"{name}.json"
        self.calib = json.loads(path.read_text()) if path.exists() else {}

    def setSizePix(self, size):  # noqa: N802 - psychopy's own method names
        self.calib["sizePix"] = list(size)

    def setWidth(self, width):  # noqa: N802
        self.calib["width"] = width

    def setDistance(self, distance):  # noqa: N802
        self.calib["distance"] = distance

    def setGamma(self, gamma):  # noqa: N802
        self.calib["gamma"] = gamma

    def setNotes(self, notes):  # noqa: N802
        self.calib["notes"] = notes

    def setCalibDate(self, date=None):  # noqa: N802
        self.calib["calibDate"] = date if date is not None else 1_756_000_000.0

    def getSizePix(self):  # noqa: N802
        return self.calib["sizePix"]

    def getWidth(self):  # noqa: N802
        return self.calib["width"]

    def getDistance(self):  # noqa: N802
        return self.calib["distance"]

    def getGamma(self):  # noqa: N802
        return self.calib["gamma"]

    def getNotes(self):  # noqa: N802
        return self.calib["notes"]

    def getCalibDate(self):  # noqa: N802
        return self.calib["calibDate"]

    def save(self):
        (self.folder / f"{self.name}.json").write_text(json.dumps(self.calib))


class FakeMonitors:
    """Stand-in for the psychopy.monitors module itself."""

    Monitor = FakeMonitor

    def __init__(self):
        self.folder = None

    @property
    def monitorFolder(self):  # noqa: N802 - psychopy's own attribute name
        return str(self.folder)

    def getAllMonitors(self):  # noqa: N802
        return [path.stem for path in self.folder.glob("*.json")]

    def strFromDate(self, date):  # noqa: N802
        return f"stamped:{date:.0f}"


# One instance, pointed at a fresh temp folder by the fixture below. Module
# level because FakeMonitor is constructed by alhazen, not by the test, so it
# has no other way to learn where the folder is.
FAKE = FakeMonitors()


@pytest.fixture
def fake_psychopy(tmp_path, monkeypatch):
    """Make ``from psychopy import monitors`` yield the fake, storing under tmp."""
    FAKE.folder = tmp_path / "monitors"
    FAKE.folder.mkdir()
    package = types.ModuleType("psychopy")
    package.monitors = FAKE
    monkeypatch.setitem(sys.modules, "psychopy", package)
    monkeypatch.setitem(sys.modules, "psychopy.monitors", FAKE)
    return FAKE


def rig_file(tmp_path, backend="psychopy", **monitor_overrides):
    """A rig YAML on disk, since the CLI takes paths, not objects."""
    body = {
        "monitor": {**MONITOR.model_dump(), **monitor_overrides},
        "display": {"backend": backend},
        "data_root": str(tmp_path / "data"),
    }
    path = tmp_path / "rig.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


class TestMonitorName:
    """The name is a file name in someone's home directory, so it is checked."""

    def test_defaults_to_alhazen(self):
        assert (
            MonitorConfig(
                width_px=800, height_px=600, width_cm=40, distance_cm=57, refresh_rate_hz=60
            ).name
            == "alhazen"
        )

    @pytest.mark.parametrize("name", ["", "  ", " rig-a", "rig-a ", "a/b", "a\\b", ".", ".."])
    def test_a_name_that_is_not_a_plain_file_name_is_refused(self, name):
        with pytest.raises(ValueError):
            MonitorConfig(
                width_px=800,
                height_px=600,
                width_cm=40,
                distance_cm=57,
                refresh_rate_hz=60,
                name=name,
            )


class TestRegister:
    def test_writes_the_configs_geometry_under_the_configured_name(self, fake_psychopy):
        path = registry.register(MONITOR)

        assert path.name == "rig-a.json"
        stored = registry.lookup("rig-a")
        assert stored.registered
        assert (stored.width_px, stored.height_px) == (1920, 1080)
        assert (stored.width_cm, stored.distance_cm) == (52.0, 57.0)

    def test_registering_twice_updates_one_record(self, fake_psychopy):
        registry.register(MONITOR)
        moved = MONITOR.model_copy(update={"distance_cm": 60.0})
        registry.register(moved)

        assert registry.registered_names() == ["rig-a"]
        assert registry.lookup("rig-a").distance_cm == 60.0

    def test_a_measured_gamma_is_stored_on_the_monitor(self, fake_psychopy):
        registry.register(MONITOR, gamma=2.2)
        assert registry.lookup("rig-a").gamma == 2.2

    def test_registering_without_a_gamma_leaves_a_measured_one_alone(self, fake_psychopy):
        # The gamma may have been measured in PsychoPy's own Monitor Center,
        # which alhazen knows nothing about. Re-registering geometry must not
        # throw that away.
        registry.register(MONITOR, gamma=2.2)
        registry.register(MONITOR)
        assert registry.lookup("rig-a").gamma == 2.2

    def test_a_non_positive_gamma_is_refused(self, fake_psychopy):
        with pytest.raises(DisplayError, match="gamma must be positive"):
            registry.register(MONITOR, gamma=0.0)

    def test_notes_are_stored_so_monitor_center_says_where_it_came_from(self, fake_psychopy):
        registry.register(MONITOR, notes="registered by alhazen 1.0.0 from rig.yaml")
        assert "alhazen" in (registry.lookup("rig-a").notes or "")


class TestLookup:
    def test_an_unregistered_name_is_not_an_error(self, fake_psychopy):
        found = registry.lookup("never-seen")
        assert not found.registered
        assert "not registered" in found.summary()

    def test_reading_does_not_create_a_record(self, fake_psychopy):
        registry.lookup("never-seen")
        assert registry.registered_names() == []

    def test_a_partial_record_reads_back_as_unknown_fields(self, fake_psychopy):
        # A file written by hand or by an older psychopy: the fields that are
        # there must still be readable, not a KeyError from a getter.
        (fake_psychopy.folder / "sparse.json").write_text(json.dumps({"width": 30.0}))
        found = registry.lookup("sparse")
        assert found.registered
        assert found.width_cm == 30.0
        assert found.width_px is None and found.distance_cm is None


class TestDifferences:
    def test_a_matching_registration_has_none(self, fake_psychopy):
        registry.register(MONITOR)
        assert registry.differences(MONITOR, registry.lookup("rig-a")) == []

    def test_an_unregistered_monitor_has_nothing_to_disagree_with(self, fake_psychopy):
        assert registry.differences(MONITOR, registry.lookup("rig-a")) == []

    def test_each_geometry_field_is_reported(self, fake_psychopy):
        registry.register(MONITOR)
        edited = MONITOR.model_copy(
            update={"width_px": 1280, "width_cm": 50.0, "distance_cm": 60.0}
        )
        drift = registry.differences(edited, registry.lookup("rig-a"))
        assert len(drift) == 3
        assert any("size:" in line for line in drift)
        assert any("width_cm:" in line for line in drift)
        assert any("distance_cm:" in line for line in drift)

    def test_a_measured_gamma_is_not_a_disagreement(self, fake_psychopy):
        # Gamma is a measurement psychopy owns; the rig config has no opinion
        # about it, so it can never be a conflict.
        registry.register(MONITOR, gamma=2.2)
        assert registry.differences(MONITOR, registry.lookup("rig-a")) == []


class TestResolve:
    def test_an_unregistered_monitor_still_opens_with_the_configs_geometry(self, fake_psychopy):
        mon = registry.resolve(MONITOR)
        assert mon.getSizePix() == [1920, 1080]
        assert mon.getWidth() == 52.0
        assert mon.getDistance() == 57.0

    def test_resolving_an_unregistered_monitor_writes_nothing(self, fake_psychopy):
        registry.resolve(MONITOR)
        assert registry.registered_names() == []

    def test_a_registered_monitor_keeps_its_calibration(self, fake_psychopy):
        registry.register(MONITOR, gamma=2.2)
        assert registry.resolve(MONITOR).getGamma() == 2.2

    def test_drift_refuses_to_open_rather_than_guessing(self, fake_psychopy):
        registry.register(MONITOR)
        moved = MONITOR.model_copy(update={"distance_cm": 100.0})
        with pytest.raises(DisplayError) as excinfo:
            registry.resolve(moved)
        message = str(excinfo.value)
        assert "distance_cm" in message
        assert "alhazen monitor register" in message


class TestWithoutPsychopy:
    def test_every_entry_point_names_the_extra(self, monkeypatch):
        # No psychopy at all: importing it raises, and each entry point has to
        # say what to install rather than surfacing an ImportError.
        monkeypatch.setitem(sys.modules, "psychopy", None)
        for call in (
            lambda: registry.register(MONITOR),
            lambda: registry.lookup("rig-a"),
            lambda: registry.resolve(MONITOR),
            registry.registered_names,
            registry.monitor_folder,
        ):
            with pytest.raises(DisplayError, match=r"alhazen\[psychopy\]"):
                call()


class TestCheckRig:
    """`alhazen check-rig` is where an experimenter finds out before a session."""

    def test_an_unregistered_monitor_passes_but_says_so(self, fake_psychopy, tmp_path, capsys):
        assert main(["check-rig", "--rig", str(rig_file(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "OK   monitor: 'rig-a' is not registered" in out
        assert "alhazen monitor register" in out

    def test_a_matching_registration_passes(self, fake_psychopy, tmp_path, capsys):
        registry.register(MONITOR, gamma=2.2)
        assert main(["check-rig", "--rig", str(rig_file(tmp_path))]) == 0
        assert (
            "OK   monitor: 'rig-a' registered with psychopy, gamma 2.200" in capsys.readouterr().out
        )

    def test_drift_fails_the_check(self, fake_psychopy, tmp_path, capsys):
        registry.register(MONITOR)
        rig = rig_file(tmp_path, distance_cm=100.0)
        assert main(["check-rig", "--rig", str(rig)]) == 1
        assert "FAIL monitor:" in capsys.readouterr().out

    def test_a_simulated_rig_needs_no_registration(self, fake_psychopy, tmp_path, capsys):
        assert main(["check-rig", "--rig", str(rig_file(tmp_path, backend="simulated"))]) == 0
        assert "no psychopy registration needed" in capsys.readouterr().out


class TestCli:
    def test_register_writes_the_monitor_and_reports_the_file(
        self, fake_psychopy, tmp_path, capsys
    ):
        rig = rig_file(tmp_path)
        assert main(["monitor", "register", "--rig", str(rig)]) == 0
        out = capsys.readouterr().out
        assert "registered 'rig-a'" in out
        assert "rig-a.json" in out
        # No gamma has been measured for this rig, and that is worth saying:
        # a monitor registered without one linearises nothing.
        assert "no measured gamma yet" in out
        assert registry.lookup("rig-a").registered

    def test_register_picks_up_a_measured_gamma(self, fake_psychopy, tmp_path, capsys):
        rig = rig_file(tmp_path)
        (tmp_path / "rig_gamma.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "gamma": 2.4})
        )
        assert main(["monitor", "register", "--rig", str(rig)]) == 0
        assert "gamma 2.400" in capsys.readouterr().out
        assert registry.lookup("rig-a").gamma == 2.4

    def test_registering_a_simulated_rig_says_it_will_not_be_used(
        self, fake_psychopy, tmp_path, capsys
    ):
        rig = rig_file(tmp_path, backend="simulated")
        assert main(["monitor", "register", "--rig", str(rig)]) == 0
        assert "display backend is 'simulated'" in capsys.readouterr().out

    def test_list_shows_what_psychopy_knows(self, fake_psychopy, tmp_path, capsys):
        registry.register(MONITOR, gamma=2.2)
        assert main(["monitor", "list"]) == 0
        out = capsys.readouterr().out
        assert "rig-a: 1920x1080, 52 cm wide, 57 cm away, gamma 2.200" in out

    def test_list_on_a_fresh_machine_says_none(self, fake_psychopy, capsys):
        assert main(["monitor", "list"]) == 0
        assert "none registered yet" in capsys.readouterr().out

    def test_show_reports_agreement(self, fake_psychopy, tmp_path, capsys):
        registry.register(MONITOR)
        assert main(["monitor", "show", "--rig", str(rig_file(tmp_path))]) == 0
        assert "OK — the rig config and psychopy agree" in capsys.readouterr().out

    def test_show_fails_on_drift_and_names_the_field(self, fake_psychopy, tmp_path, capsys):
        registry.register(MONITOR)
        rig = rig_file(tmp_path, width_cm=50.0)
        assert main(["monitor", "show", "--rig", str(rig)]) == 1
        out = capsys.readouterr().out
        assert "MISMATCH" in out
        assert "width_cm: config 50, registered 52.0" in out

    def test_show_fails_when_the_monitor_was_never_registered(
        self, fake_psychopy, tmp_path, capsys
    ):
        assert main(["monitor", "show", "--rig", str(rig_file(tmp_path))]) == 1
        assert "register it with: alhazen monitor register" in capsys.readouterr().out

    def test_monitor_without_a_subcommand_prints_help(self, fake_psychopy, capsys):
        with pytest.raises(SystemExit):
            main(["monitor"])

    def test_a_broken_rig_file_is_a_config_error(self, fake_psychopy, tmp_path, capsys):
        path = tmp_path / "bad.yaml"
        path.write_text("monitor: {width_px: 0}\n")
        assert main(["monitor", "register", "--rig", str(path)]) == 1
        assert "INVALID" in capsys.readouterr().err


def test_round_trip_through_the_real_psychopy(tmp_path, monkeypatch):
    """The one test that proves the fake above is not lying.

    Skipped wherever psychopy is not installed (all of CI). The monitor folder
    is redirected at tmp_path first, so running this never touches the
    machine's real monitor database.
    """
    monitors = pytest.importorskip("psychopy.monitors")
    folder = tmp_path / "monitors"
    folder.mkdir()
    # Both bindings: calibTools reads its own module global when it saves,
    # while alhazen reads the one re-exported on psychopy.monitors.
    monkeypatch.setattr(monitors, "monitorFolder", str(folder))
    monkeypatch.setattr(monitors.calibTools, "monitorFolder", str(folder))

    written = registry.register(MONITOR, gamma=2.2, notes="from a test")
    assert written.exists()
    assert registry.registered_names() == ["rig-a"]

    found = registry.lookup("rig-a")
    assert (found.width_px, found.height_px) == (1920, 1080)
    assert (found.width_cm, found.distance_cm) == (52.0, 57.0)
    assert found.gamma == 2.2
    assert found.calibrated is not None
    assert registry.differences(MONITOR, found) == []

    # And the real Monitor object a window would be opened against.
    mon = registry.resolve(MONITOR)
    assert mon.getSizePix() == [1920, 1080]
    assert mon.getGamma() == 2.2
