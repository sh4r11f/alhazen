"""The library as a rig sees it (alhazen.experiment_platform.library): the connection
file, the client against a real local platform, and the disk rules a download follows."""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, experiment, zip_of

from alhazen.experiment_platform import library
from alhazen.experiment_platform.library import (
    CopyConflict,
    LibraryClient,
    LibraryError,
    LibrarySource,
)
from alhazen.experiment_platform.local import LocalPlatform
from alhazen.experiment_platform.store import FileStore


def source(tmp_path: Path, **fields) -> LibrarySource:  # type: ignore[no-untyped-def]
    return LibrarySource(
        url="https://mana.example/api/x/svc", token="t0ken", copies=tmp_path / "copies", **fields
    )


class TestConnection:
    def test_a_local_platform_url_carries_its_token(self, tmp_path: Path):
        s = LibrarySource.from_url("http://127.0.0.1:8123/#token=abc", copies=tmp_path)
        assert (s.url, s.token, s.auth) == ("http://127.0.0.1:8123", "abc", "alhazen-token")

    def test_a_token_never_crosses_the_network_in_clear(self, tmp_path: Path):
        with pytest.raises(LibraryError, match="https"):
            LibrarySource(url="http://lab.example/api", token="t", copies=tmp_path)
        with pytest.raises(LibraryError, match="token"):
            LibrarySource.from_url("https://lab.example", None, copies=tmp_path)

    def test_the_file_is_owner_only_and_round_trips(self, tmp_path: Path):
        path = library.save_source(tmp_path / "state", source(tmp_path, label="Mana", expires=5))
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600
        again = library.load_source(tmp_path / "state")
        assert again == source(tmp_path, label="Mana", expires=5)
        assert (
            library.forget_source(tmp_path / "state")
            and library.load_source(tmp_path / "state") is None
        )

    def test_an_unreadable_file_is_an_error_not_no_library(self, tmp_path: Path):
        (tmp_path / library.SOURCE_FILE).write_text("{nope", encoding="utf-8")
        with pytest.raises(LibraryError, match="not JSON"):
            library.load_source(tmp_path)

    def test_the_page_sees_the_origin_never_the_path(self, tmp_path: Path):
        assert source(tmp_path).origin == "https://mana.example"


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, kind: str = "application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": kind}


class TestClient:
    def test_a_refused_token_says_to_connect_again_without_showing_it(self, tmp_path: Path):
        def opener(request, timeout):  # type: ignore[no-untyped-def]
            assert request.headers["Authorization"] == "Bearer t0ken"
            raise urllib.error.HTTPError(
                request.full_url, 401, "no", {}, io.BytesIO(b'{"detail":"expired"}')
            )

        with pytest.raises(LibraryError, match="Connect the library again") as raised:
            LibraryClient(source(tmp_path), opener=opener).forks()
        assert "t0ken" not in str(raised.value)

    def test_copies_without_a_title_or_with_unsafe_ids_are_dropped(self, tmp_path: Path):
        body = {"forks": {"good-1": {"title": "G"}, "../x": {"title": "X"}, "bare": {}}}
        client = LibraryClient(
            source(tmp_path), opener=lambda r, timeout: FakeResponse(json.dumps(body).encode())
        )
        assert list(client.forks()) == ["good-1"]

    def test_against_a_real_local_platform(self, tmp_path: Path):
        store = FileStore(tmp_path / "store", create=True)
        add_version(store, "lab", "dots", experiment())
        platform = LocalPlatform(store, name="Me")
        platform.start()
        try:
            platform_source = LibrarySource.from_url(platform.url, copies=tmp_path / "copies")
            client = LibraryClient(platform_source)
            assert client.forks() == {}
            made = urllib.request.urlopen(  # the web app's "Add to library"
                urllib.request.Request(
                    f"{platform.origin}/api/forks",
                    data=b'{"publisher":"lab","exp":"dots"}',
                    headers={"X-Alhazen-Token": platform.token, "Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=20,
            )
            created = json.loads(made.read())
            fork_id, fork = created["id"], created["fork"]
            forks = client.forks()
            archive = client.archive(forks[fork_id]["head"]["path"])
            result = library.write_copy(
                platform_source.copy_dir(fork_id),
                archive,
                copy_id=fork_id,
                bundle_id=fork["head"]["id"],
                title="Dots",
                source="local",
            )
            assert result.action == "written" and result.state.state == "current"
            assert (platform_source.copy_dir(fork_id) / "run.py").is_file()
        finally:
            platform.shutdown()


def archive_of(**changes: bytes) -> bytes:
    return zip_of(experiment(**changes), top="dots-abc")


def session_in(root: Path, folder: str = "data") -> None:
    run = root / folder / "v1" / "sub-01" / "ses-01"
    run.mkdir(parents=True)
    (run / "session.json").write_text("{}", encoding="utf-8")


class TestDisk:
    def write(self, root: Path, archive: bytes, bundle: str, replace: bool = False):  # type: ignore[no-untyped-def]
        return library.write_copy(
            root,
            archive,
            copy_id="dots-abc",
            bundle_id=bundle,
            title="Dots",
            source="test",
            replace=replace,
        )

    def test_states_follow_the_marker(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        assert library.inspect_copy(root, "b1").state == "absent"
        self.write(root, archive_of(), "b1")
        assert library.inspect_copy(root, "b1").state == "current"
        assert library.inspect_copy(root, "b2").state == "outdated"
        (root / library.MARKER).unlink()
        assert library.inspect_copy(root, "b1").state == "unknown"

    def test_a_current_folder_is_not_written_again(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        self.write(root, archive_of(), "b1")
        (root / "run.py").write_text("# edited on the rig\n", encoding="utf-8")
        assert self.write(root, archive_of(), "b1").action == "unchanged"
        assert (root / "run.py").read_text(encoding="utf-8") == "# edited on the rig\n"

    def test_a_folder_already_holding_these_files_is_adopted(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        self.write(root, archive_of(), "b1")
        (root / library.MARKER).unlink()
        session_in(root)
        result = self.write(root, archive_of(), "b1")
        assert result.action == "adopted" and result.state.state == "current"
        assert result.state.data.sessions == 1

    def test_another_revision_beside_data_is_never_overwritten_unasked(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        self.write(root, archive_of(), "b1")
        session_in(root)
        session_in(root, "data-rehearsal")
        newer = archive_of(**{"configs/task.yaml": b"n_trials: 99\n"})
        with pytest.raises(CopyConflict) as raised:
            self.write(root, newer, "b2")
        assert raised.value.state.data.sessions == 2
        assert raised.value.state.data.folders == ("data", "data-rehearsal")
        assert b"99" not in (root / "configs" / "task.yaml").read_bytes()
        result = self.write(root, newer, "b2", replace=True)
        assert result.action == "updated" and result.state.state == "current"
        assert b"99" in (root / "configs" / "task.yaml").read_bytes()
        assert result.state.data.sessions == 2
        marker = library.read_marker(root)
        assert marker["previous"][-1]["bundle"] == "b1"

    def test_another_revision_without_data_is_updated(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        self.write(root, archive_of(), "b1")
        assert (
            self.write(root, archive_of(**{"configs/task.yaml": b"n_trials: 7\n"}), "b2").action
            == "updated"
        )

    def test_environments_and_hidden_folders_are_not_counted_as_data(self, tmp_path: Path):
        root = tmp_path / "dots-abc"
        session_in(root, ".venv")
        session_in(root, "data")
        assert library.find_data(root).sessions == 1

    def test_an_archive_that_is_not_an_experiment_is_refused(self, tmp_path: Path):
        with pytest.raises(LibraryError, match="not an alhazen experiment"):
            self.write(tmp_path / "x", zip_of({"notes.txt": b"hi"}), "b1")
