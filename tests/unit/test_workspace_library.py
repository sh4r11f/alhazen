"""The dashboard's library (alhazen.cli.workspace_library): copies listed from the
platform, downloaded on demand after a disk check, built and registered."""

from __future__ import annotations

import hashlib
import json
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


# ---------------------------------------------------------------- the list appears by itself
def test_a_connection_written_while_the_page_is_open_is_read_in_the_background(tmp_path: Path):
    """Mana's kit writes library.json after the page has loaded: the next poll starts a read,
    says it is reading, and the one after lists the copies (no Refresh needed)."""
    state = tmp_path / "state"
    state.mkdir()
    client = FakeClient({"dots-1": fork("dots-1", "b1")}, {})
    lib = Library(FakeWorkspace(state), client_factory=client, sync=lambda *a: None)
    assert lib.get() == {"configured": False, "items": []}
    library.save_source(
        state, LibrarySource(url="https://mana.example/svc", token="t", copies=tmp_path / "c")
    )
    first = lib.get()
    assert first["configured"] and first["refreshing"] is True and first["items"] == []
    wait(lib)
    page = lib.get()
    assert page["refreshing"] is False and [x["id"] for x in page["items"]] == ["dots-1"]


def test_a_changed_connection_drops_the_old_list_and_reads_again(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup
    lib.refresh()
    assert len(lib.get()["items"]) == 1
    client._forks = {"other-2": fork("other-2", "b9")}
    library.save_source(
        lib.directory,
        LibrarySource(url="https://other.example/svc", token="t2", copies=copies),
    )
    assert lib.get()["items"] == []  # the old list belongs to the old connection
    wait(lib)
    assert [x["id"] for x in lib.get()["items"]] == ["other-2"]


def test_a_failed_background_read_is_shown_not_swallowed(setup):  # type: ignore[no-untyped-def]
    lib, ws, client, synced, copies = setup

    def broken() -> dict[str, Any]:
        raise RuntimeError("socket closed")

    client.forks = broken  # type: ignore[method-assign]
    assert lib.get()["refreshing"] is True
    wait(lib)
    page = lib.get()
    assert page["refreshing"] is False and "socket closed" in page["error"]
    assert lib.get()["refreshing"] is False  # not retried on every poll


def test_the_page_connects_with_a_token_checked_before_it_is_saved(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    client = FakeClient({"dots-1": fork("dots-1", "b1")}, {})
    lib = Library(FakeWorkspace(state), client_factory=client, sync=lambda *a: None)
    page = lib.connect("https://lab.example/platform/", "alm_x", "Lab")
    assert page["source"]["label"] == "Lab" and [x["id"] for x in page["items"]] == ["dots-1"]
    saved = library.load_source(state)
    assert saved.url == "https://lab.example/platform" and saved.auth == "bearer"

    def refused() -> dict[str, Any]:
        raise LibraryError("Lab refused this dashboard's token (401)")

    client.forks = refused  # type: ignore[method-assign]
    with pytest.raises(LibraryError):
        lib.connect("https://evil.example", "alm_wrong")
    assert library.load_source(state).url == "https://lab.example/platform"  # kept


def test_closing_reports_the_sessions_and_the_jobs(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    library.save_source(
        state,
        LibrarySource(
            url="https://mana.example/svc",
            token="t",
            copies=tmp_path / "c",
            machine="Booth 2",
            run_jobs=("1792455795541",),
        ),
    )
    sent: list[dict[str, Any]] = []

    class Reporting(FakeClient):
        def report(self, event: dict[str, Any]) -> dict[str, Any]:
            sent.append(event)
            return {"ok": True}

    lib = Library(FakeWorkspace(state), client_factory=Reporting({}, {}), sync=lambda *a: None)
    sessions = [{"experiment": "Dots", "task": "main", "outcomes": {"CORRECT": 3}}]
    message = lib.report_closed(
        started="2026-10-19T10:00:00+00:00", sessions=sessions, reason="shut down from the page"
    )
    assert "Told Library the dashboard closed (1 session" in message
    (event,) = sent
    assert event["event"] == "dashboard-closed" and event["machine"] == "Booth 2"
    assert event["jobs"] == ["1792455795541"] and event["sessions"] == sessions
    assert (state / "library" / "reports.log").read_text(encoding="utf-8").count("\n") == 1


def test_closing_never_waits_long_for_a_platform_that_hangs(tmp_path: Path):
    import threading
    import time as _time

    state = tmp_path / "state"
    state.mkdir()
    library.save_source(
        state, LibrarySource(url="https://mana.example/svc", token="t", copies=tmp_path / "c")
    )
    release = threading.Event()

    class Hanging(FakeClient):
        def report(self, event: dict[str, Any]) -> dict[str, Any]:
            release.wait(30)
            return {}

    lib = Library(FakeWorkspace(state), client_factory=Hanging({}, {}), sync=lambda *a: None)
    t0 = _time.monotonic()
    message = lib.report_closed(started="x", sessions=[], reason="r", timeout=0.2)
    release.set()
    assert _time.monotonic() - t0 < 3 and "did not answer" in message


def test_closing_without_a_library_says_nothing(tmp_path: Path):
    lib = Library(FakeWorkspace(tmp_path))
    assert lib.report_closed(started="x", sessions=[], reason="r") == ""


def test_session_summaries_read_the_runs_and_their_last_console_line(tmp_path: Path):
    import threading

    class Runs:
        directory = tmp_path
        lock = threading.Lock()
        runs = {
            "r1": {
                "id": "r1",
                "name": "dots",
                "task": "main",
                "mode": "run",
                "started": "2026-10-19T10:05:00+00:00",
                "finished": "2026-10-19T10:20:00+00:00",
                "status": "completed",
                "cwd": str(tmp_path / "copy"),
            },
            "r0": {
                "id": "r0",
                "name": "dots",
                "task": "main",
                "mode": "run",
                "started": "2026-10-18T09:00:00+00:00",
                "finished": None,
                "status": "completed",
                "cwd": str(tmp_path / "copy"),
            },
        }

    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "console.log").write_text(
        "INFO running main\nINFO session end: complete — 12 trials served, 10 rows recorded "
        "(CORRECT 7, WRONG 3)\n",
        encoding="utf-8",
    )
    (tmp_path / "copy" / ".alhazen-hub").mkdir(parents=True)
    (tmp_path / "copy" / ".alhazen-hub" / "source.json").write_text(
        json.dumps({"schema": 1, "id": "dots-1", "bundle": "b1"}), encoding="utf-8"
    )
    (only,) = workspace_library.session_summaries(Runs(), "2026-10-19T10:00:00+00:00")
    assert only["copy"] == "dots-1" and only["revision"] == "b1" and only["session"] == "complete"
    assert only["outcomes"] == {"CORRECT": 7, "WRONG": 3} and only["task"] == "main"


def test_the_command_line_takes_a_machine_token_and_a_name(tmp_path: Path):
    import argparse

    parser = argparse.ArgumentParser()
    workspace_library.add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "library",
            "connect",
            "https://lab.example/platform",
            "--token",
            "alm_abc",
            "--machine",
            "Booth 2",
            "--state-dir",
            str(tmp_path),
        ]
    )
    assert workspace_library.run(args) == 0
    saved = library.load_source(tmp_path)
    assert saved.auth == "bearer" and saved.token == "alm_abc" and saved.machine == "Booth 2"
