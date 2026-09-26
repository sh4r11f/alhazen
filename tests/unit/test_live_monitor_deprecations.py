"""The live monitor's pre-1.9 names still work, and say they are going.

Until 1.8 the live session monitor was "the dashboard". That word now names
the experiment workspace (`alhazen dashboard`), and every public spelling of
the monitor's names moved with it. docs/versioning.md §4 promises that a name
deprecated in a MINOR release keeps working and warning until the next MAJOR
one, so each old spelling below must do two things: resolve to exactly what
the new spelling does, and warn with the version it goes in and what to use
instead — a warning that says only "deprecated" fails here. The old spellings
and this file are removed together in 2.0.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import warnings

import pytest
from test_builder import Params, build

import alhazen
from alhazen import outcomes
from alhazen.cli.main import add_mode_arguments
from alhazen.config.models import LiveMonitorConfig, RigConfig
from alhazen.core.events import EventSchema
from alhazen.live_monitor.spec import LiveMonitorPanel, LiveMonitorSpec
from alhazen.session.builder import task_live_monitor
from alhazen.task.task import Task
from support import MONITOR

GOING = r"is deprecated since alhazen 1\.9 and will be removed in 2\.0; use "


def fresh_import(module: str):
    """Import `module` as a package written for 1.x would on first import:
    the warning is raised by the package's body, which runs once per process,
    so an earlier test's import of the package — not just of the submodule
    asked for — must be forgotten first."""
    package = ".".join(module.split(".")[:2])
    for name in list(sys.modules):
        if name == package or name.startswith(package + "."):
            del sys.modules[name]
    return importlib.import_module(module)


class TestTheOldImportPath:
    def test_importing_the_old_package_warns_and_gives_the_same_classes(self):
        with pytest.warns(
            DeprecationWarning, match=r"alhazen\.dashboard " + GOING + r"alhazen\.live_monitor"
        ):
            old = fresh_import("alhazen.dashboard")
        assert old.DashboardPanel is LiveMonitorPanel
        assert old.DashboardSpec is LiveMonitorSpec

    def test_the_old_spec_module_too(self):
        # `from alhazen.dashboard.spec import DashboardPanel, DashboardSpec`
        # is the line every experiment package written for 1.x carries.
        with pytest.warns(DeprecationWarning, match=r"alhazen\.dashboard "):
            spec = fresh_import("alhazen.dashboard.spec")
        assert spec.DashboardPanel is LiveMonitorPanel
        assert spec.DashboardSpec is LiveMonitorSpec

    def test_the_new_package_warns_about_nothing(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            fresh_import("alhazen.live_monitor")


class TestTheOldTopLevelNames:
    @pytest.mark.parametrize(
        "old, new",
        [
            ("DashboardConfig", LiveMonitorConfig),
            ("DashboardPanel", LiveMonitorPanel),
            ("DashboardSpec", LiveMonitorSpec),
        ],
    )
    def test_resolve_to_the_new_class_with_a_warning(self, old, new):
        with pytest.warns(
            DeprecationWarning, match=rf"alhazen\.{old} " + GOING + rf"alhazen\.{new.__name__}"
        ):
            assert getattr(alhazen, old) is new

    def test_only_the_new_names_are_exported(self):
        exported = set(alhazen.__all__)
        assert {"LiveMonitorConfig", "LiveMonitorPanel", "LiveMonitorSpec"} <= exported
        assert not {"DashboardConfig", "DashboardPanel", "DashboardSpec"} & exported

    def test_an_unknown_name_is_still_an_attribute_error(self):
        # The module-level __getattr__ that serves the old names must not
        # turn every typo into a silent None.
        with pytest.raises(AttributeError, match="no attribute 'Nope'"):
            alhazen.Nope  # noqa: B018 — the attribute access is the test


class TestTheOldRigFileSection:
    def rig(self, **sections):
        return {"monitor": MONITOR, "data_root": ".", **sections}

    def test_a_dashboard_section_is_read_as_live_monitor_with_a_warning(self):
        with pytest.warns(
            DeprecationWarning,
            match=r"the rig file's `dashboard:` section " + GOING + r"`live_monitor:`",
        ):
            rig = RigConfig.model_validate(self.rig(dashboard={"enabled": True, "port": 4242}))
        assert rig.live_monitor == LiveMonitorConfig(enabled=True, port=4242)

    def test_a_file_naming_both_sections_is_refused(self):
        with pytest.raises(ValueError, match="both `dashboard` and `live_monitor`"):
            RigConfig.model_validate(
                self.rig(dashboard={"enabled": True}, live_monitor={"enabled": False})
            )

    def test_the_new_section_warns_about_nothing(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            rig = RigConfig.model_validate(self.rig(live_monitor={"enabled": True}))
        assert rig.live_monitor.enabled is True


class TestTheOldFlags:
    def parse(self, *argv: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        return parser.parse_args(["--mode", "simulate", *argv])

    @pytest.mark.parametrize(
        "flag, instead, dest, value",
        [
            ("--dashboard", "--live-monitor", "live_monitor", True),
            ("--no-dashboard", "--no-live-monitor", "live_monitor", False),
            (
                "--no-dashboard-browser",
                "--no-live-monitor-browser",
                "no_live_monitor_browser",
                True,
            ),
        ],
    )
    def test_an_old_flag_sets_the_new_one_and_warns(self, flag, instead, dest, value):
        with pytest.warns(DeprecationWarning, match=rf"the {flag} flag " + GOING + instead):
            args = self.parse(flag)
        assert getattr(args, dest) is value

    def test_the_old_flags_are_not_in_the_help(self):
        # Nobody should learn the old spelling from the tool retiring it.
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        assert "--live-monitor" in parser.format_help()
        assert "dashboard" not in parser.format_help()

    def test_the_new_flags_warn_about_nothing(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            args = self.parse("--live-monitor", "--no-live-monitor-browser")
        assert args.live_monitor is True and args.no_live_monitor_browser is True
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert self.parse().live_monitor is None


class TestTheOldBuildSessionKeywords:
    """`build_session(dashboard=, open_dashboard=)`, through test_builder's
    simulated-rig helper. The monitor stays off in every case (a real one
    starts a child process), so the observable effect is the warning and the
    absence of a monitor address."""

    def test_the_old_keywords_still_work_and_warn(self, tmp_path):
        schema = EventSchema(("FIX_ON",))
        with pytest.warns(
            DeprecationWarning, match=r"the 'dashboard' argument " + GOING + "live_monitor"
        ):
            runner = build(tmp_path, schema, dashboard=False)
        assert runner.live_monitor_url is None
        with pytest.warns(
            DeprecationWarning,
            match=r"the 'open_dashboard' argument " + GOING + "open_live_monitor",
        ):
            runner = build(tmp_path, schema, open_dashboard=False)
        assert runner.live_monitor_url is None

    def test_both_spellings_at_once_are_refused_before_anything_is_built(self, tmp_path):
        schema = EventSchema(("FIX_ON",))
        with (
            pytest.warns(DeprecationWarning),
            pytest.raises(ValueError, match="not both live_monitor= and dashboard="),
        ):
            build(tmp_path, schema, dashboard=False, live_monitor=False)
        assert not list(tmp_path.glob("sub-*")), "a refused build must leave no run directory"


class TestTheOldTaskAttribute:
    """A task written for 1.x declares its panels as `dashboard = DashboardSpec(...)`."""

    panels = LiveMonitorSpec()

    def test_a_task_declaring_dashboard_still_feeds_the_monitor(self):
        class OldTask(Task):
            name = "old-spelling"
            events = EventSchema(("FIX_ON",))
            outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
            params_model = Params
            dashboard = self.panels

        with pytest.warns(
            DeprecationWarning, match=r"OldTask\.dashboard " + GOING + "live_monitor"
        ):
            assert task_live_monitor(OldTask(Params())) is self.panels

    def test_a_task_declaring_both_is_refused(self):
        class BothTask(Task):
            name = "both"
            events = EventSchema(("FIX_ON",))
            outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
            params_model = Params
            dashboard = self.panels
            live_monitor = LiveMonitorSpec()

        with (
            pytest.warns(DeprecationWarning),
            pytest.raises(ValueError, match="declares both live_monitor and dashboard"),
        ):
            task_live_monitor(BothTask(Params()))

    def test_a_task_on_the_new_spelling_warns_about_nothing(self):
        class NewTask(Task):
            name = "new-spelling"
            events = EventSchema(("FIX_ON",))
            outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
            params_model = Params
            live_monitor = self.panels

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert task_live_monitor(NewTask(Params())) is self.panels
