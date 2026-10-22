"""The rig CLI must not need a home folder just to be imported (Windows CI, 2026-10-21)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_importing_the_rig_cli_does_not_look_up_the_home_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every `alhazen` command imports this module. With no HOME / USERPROFILE,
    # Path.home() raises, and an import-time lookup made `alhazen validate` fail.
    def no_home(cls: type[Path]) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    import alhazen.cli.machine_rig_cli as rig_cli

    importlib.reload(rig_cli)
    with pytest.raises(RuntimeError, match="home directory"):
        rig_cli.default_state()
