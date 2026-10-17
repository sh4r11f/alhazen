"""The dashboard's library (alhazen.cli.workspace_library): copies listed from the
platform, downloaded on demand after a disk check, built and registered."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from tests.unit.platform_support import experiment, zip_of

from alhazen.cli import workspace_library
from alhazen.cli.workspace_library import Library
from alhazen.experiment_platform import library
from alhazen.experiment_platform.library import LibraryError, LibrarySource


class FakeWorkspace:
    """The two things the library uses: the registered projects and add()."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.projects: list[dict[str, Any]] = []
        self.added: list[str] = []

    def add(self, path: str, python: str = "") -> dict[str, Any]:
        root = Path(path).resolve()
        self.added.append(str(root))
        project = {
            "id": hashlib.sha256(str(root).encode()).hexdigest()[:16],
            "path": str(root),
            "name": root.name,
            "title": "Dots",
        }
        self.projects = [p for p in self.projects if p["id"] != project["id"]] + [project]
        return project


class FakeClient:
    def __init__(self, forks: dict[str, Any], archives: dict[str, bytes]):
        self._forks, self._archives, self.downloads = forks, archives, 0

    def __call__(self, source: LibrarySource) -> FakeClient:
        return self

    def forks(self) -> dict[str, Any]:
        return self._forks

    def archive(self, handle: str) -> bytes:
        self.downloads += 1
        return self._archives[handle]


def fork(fid: str, bundle: str) -> dict[str, Any]:
    return {
        "title": "Dots",
        "origin": {"title": "Dots", "publisherName": "Lab"},
        "head": {"id": bundle, "path": f"F~{fid}~{bundle}"},
        "updatedAt": 1,
    }


@pytest.fixture()
def setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    state = tmp_path / "state"
    state.mkdir()
    library.save_source(
        state,
        LibrarySource(
            url="https://mana.example/svc", token="t", label="Mana", copies=tmp_path / "copies"
        ),
    )
    ws = FakeWorkspace(state)
    client = FakeClient(
        {"dots-1": fork("dots-1", "b1")}, {"F~dots-1~b1": zip_of(experiment(), top="dots-1")}
    )
    synced: list[tuple[Path, list[str]]] = []

    def sync(root: Path, extras: list[str], log: Path) -> None:
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        synced.append((root, extras))

    lib = Library(ws, client_factory=client, sync=sync)
    return lib, ws, client, synced, tmp_path / "copies"


def wait(lib: Library) -> None:
    lib.close(timeout=10)


def test_without_a_connection_the_page_shows_nothing(tmp_path: Path):
    assert Library(FakeWorkspace(tmp_path)).get() == {"configured": False, "items": []}


def test_a_copy_not_on_disk_is_listed_as_not_downloaded(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    page = lib.refresh()
    (item,) = page["items"]
    assert item["disk"]["state"] == "absent" and item["project"] is None
    assert page["source"]["origin"] == "https://mana.example" and "svc" not in str(page["source"])
    assert client.downloads == 0 and ws.added == []


def test_download_writes_builds_and_registers(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    lib.refresh()
    lib.download("dots-1")
    wait(lib)
    (item,) = lib.get()["items"]
    assert item["job"]["status"] == "done", item["job"]
    assert item["disk"]["state"] == "current" and item["project"]
    assert ws.added == [str((copies / "dots-1").resolve())]
    assert synced == [(copies / "dots-1", [])]  # the fixture experiment declares no psychopy extra


def test_a_second_download_of_a_current_copy_fetches_nothing(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    lib.refresh()
    lib.download("dots-1")
    wait(lib)
    lib.download("dots-1")
    wait(lib)
    assert client.downloads == 1 and len(synced) == 1
    assert "Already on this machine" in lib.get()["items"][0]["job"]["message"]


def test_a_newer_revision_beside_data_waits_for_an_answer(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    lib.refresh()
    lib.download("dots-1")
    wait(lib)
    run = copies / "dots-1" / "data" / "v1" / "sub-01" / "ses-01"
    run.mkdir(parents=True)
    (run / "session.json").write_text("{}", encoding="utf-8")
    client._forks = {"dots-1": fork("dots-1", "b2")}
    client._archives["F~dots-1~b2"] = zip_of(
        experiment(**{"configs/task.yaml": b"n_trials: 3\n"}), top="dots-1"
    )
    lib.refresh()
    assert lib.get()["items"][0]["disk"]["state"] == "outdated"
    lib.download("dots-1")
    wait(lib)
    job = lib.get()["items"][0]["job"]
    assert job["status"] == "conflict" and "1 saved session" in job["message"]
    lib.download("dots-1", replace=True)
    wait(lib)
    item = lib.get()["items"][0]
    assert item["job"]["status"] == "done" and item["disk"]["state"] == "current"
    assert item["disk"]["data"]["sessions"] == 1


def test_a_copy_on_disk_with_data_is_registered_on_refresh(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    root = copies / "dots-1"
    library.write_copy(
        root,
        client._archives["F~dots-1~b1"],
        copy_id="dots-1",
        bundle_id="b1",
        title="Dots",
        source="x",
    )
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    run = root / "data" / "v1" / "s" / "1"
    run.mkdir(parents=True)
    (run / "session.json").write_text("{}", encoding="utf-8")
    lib.refresh()
    assert ws.added == [str(root.resolve())]
    assert lib.get()["items"][0]["project"]


def test_an_unreachable_platform_keeps_the_last_list_and_says_why(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    lib.refresh()

    def down() -> dict[str, Any]:
        raise LibraryError("Mana cannot be reached: timed out")

    client.forks = down  # type: ignore[method-assign]
    with pytest.raises(LibraryError):
        lib.refresh()
    page = lib.get()
    assert page["error"] == "Mana cannot be reached: timed out" and len(page["items"]) == 1


def test_a_failed_build_is_reported(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup

    def broken(root: Path, extras: list[str], log: Path) -> None:
        raise LibraryError("Building the environment failed: no solution")

    lib._sync = broken
    lib.refresh()
    lib.download("dots-1")
    wait(lib)
    job = lib.get()["items"][0]["job"]
    assert job["status"] == "failed" and "no solution" in job["message"] and ws.added == []


def test_declared_extras_are_only_those_the_experiment_has(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\n[project.optional-dependencies]\npsychopy=["psychopy"]\n',
        encoding="utf-8",
    )
    assert workspace_library.declared_extras(tmp_path, ("psychopy", "nidaq")) == ["psychopy"]
    assert workspace_library.declared_extras(tmp_path / "missing", ("psychopy",)) == []


def test_the_command_line_connects_from_a_local_platform_url(tmp_path: Path, capsys):  # type: ignore[no-untyped-def]
    import argparse

    parser = argparse.ArgumentParser()
    workspace_library.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "library",
            "connect",
            "http://127.0.0.1:9/#token=abc",
            "--state-dir",
            str(tmp_path),
            "--copies",
            str(tmp_path / "c"),
        ]
    )
    assert workspace_library.run(args) == 0
    saved = library.load_source(tmp_path)
    assert saved.auth == "alhazen-token" and saved.token == "abc"
    assert "abc" not in capsys.readouterr().out
