"""What alhazen 2.0 removed is gone, and where an old spelling could be read
as something else, it is refused naming what replaced it.

docs/versioning.md §4: a name deprecated in a MINOR release keeps working and
warning until the next MAJOR one, which removes it. 2.0 is that release for
everything deprecated through 1.x. A removal must be loud, never a quiet
change of meaning — so each old spelling here is checked for failing, and the
two that would otherwise fail badly are checked for their message:

- a rig file's `dashboard:` section, which `extra="forbid"` would refuse only
  as "Extra inputs are not permitted", saying nothing about what to type;
- a task's `dashboard = ...`, which nothing reads any more, so the task would
  have run with no panels and nothing saying why.

Every other old spelling is refused by Python or argparse in their own words
(an ImportError, an AttributeError, a TypeError naming the keyword, an
unrecognized argument), which name the thing that is gone; the CHANGELOG's
2.0 "Removed" section names each replacement.
"""

from __future__ import annotations

import argparse
import importlib

import pytest
from test_builder import Params, build

import alhazen
from alhazen import outcomes
from alhazen.cli.main import add_mode_arguments
from alhazen.config.loader import load_rig
from alhazen.config.models import LiveMonitorConfig, RigConfig
from alhazen.core.events import EventSchema
from alhazen.errors import ConfigError
from alhazen.live_monitor.spec import LiveMonitorSpec
from alhazen.task.task import Task
from support import MONITOR

RENAMED = r"was renamed to `live_monitor:` in alhazen 1\.9"


class TestTheOldImportPath:
    @pytest.mark.parametrize("module", ["alhazen.dashboard", "alhazen.dashboard.spec"])
    def test_the_pre_1_9_package_is_gone(self, module):
        with pytest.raises(ModuleNotFoundError, match="alhazen.dashboard"):
            importlib.import_module(module)

    @pytest.mark.parametrize("old", ["DashboardConfig", "DashboardPanel", "DashboardSpec"])
    def test_the_old_top_level_names_are_gone(self, old):
        with pytest.raises(AttributeError, match=old):
            getattr(alhazen, old)
        # The form an experiment package actually wrote.
        with pytest.raises(ImportError, match=old):
            exec(f"from alhazen import {old}", {})

    def test_the_new_names_are_what_is_exported(self):
        assert {"LiveMonitorConfig", "LiveMonitorPanel", "LiveMonitorSpec"} <= set(alhazen.__all__)
        assert not [name for name in alhazen.__all__ if "Dashboard" in name]


class TestTheOldRigFileSection:
    """A rig file written before 1.9 says `dashboard:`. 2.0 refuses it, and
    the refusal is what the experimenter holding that file reads — so it
    names the key to type, not just the one that is wrong."""

    def rig(self, **sections):
        return {"monitor": MONITOR.model_dump(), "data_root": ".", **sections}

    def test_it_is_refused_naming_the_new_key(self):
        with pytest.raises(ValueError, match=RENAMED + r".*rename it to `live_monitor:`"):
            RigConfig.model_validate(self.rig(dashboard={"enabled": True}))

    def test_beside_the_new_section_it_is_refused_asking_for_a_delete(self):
        # Renaming it would leave the file with two `live_monitor:` sections.
        with pytest.raises(ValueError, match=RENAMED + r".*delete it"):
            RigConfig.model_validate(
                self.rig(dashboard={"enabled": True}, live_monitor={"enabled": False})
            )

    def test_the_refusal_from_a_file_names_the_file(self, tmp_path):
        path = tmp_path / "rig-old.yaml"
        path.write_text(
            "monitor:\n  width_px: 1920\n  height_px: 1080\n  width_cm: 60\n"
            "  distance_cm: 60\n  refresh_rate_hz: 60\n"
            f"data_root: {tmp_path.as_posix()}\n"
            "dashboard:\n  enabled: true\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=RENAMED) as refused:
            load_rig(path)
        assert str(path) in str(refused.value)

    def test_the_new_section_is_read(self):
        rig = RigConfig.model_validate(self.rig(live_monitor={"enabled": True, "port": 4242}))
        assert rig.live_monitor == LiveMonitorConfig(enabled=True, port=4242)


class TestTheOldFlags:
    """`--dashboard`, `--no-dashboard`, `--no-dashboard-browser`: argparse's
    own refusal, which names the flag. The experiment workspace still sends
    `--no-dashboard-browser` to a project whose own alhazen is older than 1.9
    (cli/workspace.py no_browser_flag), never to one on this alhazen."""

    @pytest.mark.parametrize("flag", ["--dashboard", "--no-dashboard", "--no-dashboard-browser"])
    def test_an_old_flag_is_refused(self, flag, capsys):
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(["--mode", "simulate", flag])
        assert refused.value.code == 2
        assert f"unrecognized arguments: {flag}" in capsys.readouterr().err

    def test_the_help_names_only_the_live_monitor(self):
        parser = argparse.ArgumentParser()
        add_mode_arguments(parser)
        assert "--no-live-monitor-browser" in parser.format_help()
        assert "dashboard" not in parser.format_help()


class TestTheOldBuildSessionKeywords:
    @pytest.mark.parametrize("keyword", ["dashboard", "open_dashboard"])
    def test_an_old_keyword_is_refused_before_anything_is_written(self, tmp_path, keyword):
        with pytest.raises(TypeError, match=f"unexpected keyword argument '{keyword}'"):
            build(tmp_path, EventSchema(("FIX_ON",)), **{keyword: False})
        assert not any(tmp_path.iterdir()), "a refused build must leave nothing behind"


class TestTheOldTaskAttribute:
    """A task written for 1.x declared its panels as `dashboard = ...`."""

    def test_a_task_declaring_dashboard_is_refused_naming_live_monitor(self):
        with pytest.raises(TypeError, match=r"OldTask declares `dashboard`.*live_monitor ="):

            class OldTask(Task):
                name = "old-spelling"
                events = EventSchema(("FIX_ON",))
                outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
                params_model = Params
                dashboard = LiveMonitorSpec()

    def test_so_is_a_shared_base_that_declares_it(self):
        # A family of tasks declares its panels once, on a base with no name.
        with pytest.raises(TypeError, match="SharedBase declares `dashboard`"):

            class SharedBase(Task):
                dashboard = LiveMonitorSpec()

    def test_the_new_attribute_is_accepted_and_the_old_one_is_not_inherited(self):
        panels = LiveMonitorSpec()

        class NewTask(Task):
            name = "new-spelling"
            events = EventSchema(("FIX_ON",))
            outcomes = outcomes(COMPLETED=dict(completed=True, success=True))
            params_model = Params
            live_monitor = panels

        assert NewTask(Params()).live_monitor is panels
        assert not hasattr(NewTask, "dashboard")
