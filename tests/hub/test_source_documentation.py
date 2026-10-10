"""`alhazen hub pack` points the manifest at docs/experiment.json when it is
packed (found importing kde-vergence: the CLI never set the pointer, so a
documented repository was uploaded as undocumented)."""

from __future__ import annotations

from pathlib import Path

from alhazen.hub.source import DOCUMENTATION_DESCRIPTOR as DOCUMENTATION_PATH
from alhazen.hub.source import suggest_metadata


def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    (tmp_path / "run.py").write_text("", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / DOCUMENTATION_PATH).write_text("{}", encoding="utf-8")
    return tmp_path


def test_a_packed_descriptor_is_pointed_at(tmp_path):
    root = project(tmp_path)
    metadata = suggest_metadata(root, ["run.py", DOCUMENTATION_PATH])
    assert metadata["documentation"] == "docs/experiment.json"


def test_a_descriptor_left_out_of_the_files_is_not(tmp_path):
    root = project(tmp_path)
    assert "documentation" not in suggest_metadata(root, ["run.py"])
    # Integration (fix/import-round): with no file list the suggestion looks
    # at the folder (the amodal-averaging/mbri rule); every caller in alhazen
    # now passes the list, so this only concerns direct library callers.
    assert suggest_metadata(root)["documentation"] == DOCUMENTATION_PATH
