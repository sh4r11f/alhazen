"""Upload to the archive (workspace_upload.py, upload_transport.py,
upload_sftp.py, upload_receipts.py): settings, the login, preview, copy,
verify, versions and receipts — over real HTTP, against a folder on this
computer (LocalCopy), a stand-in SFTP server (tests/unit/sftp_standin.py),
a scripted transport for failures, and real rsync through a stand-in ssh."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import paramiko
import pytest
from tests.unit import test_workspace as base
from tests.unit.sftp_standin import StandIn, remote_path
from tests.unit.test_workspace_manage import http, pid  # noqa: F401  (fixtures)

from alhazen.cli.upload_receipts import RECEIPTS_DIR, latest, receipts, write_receipt
from alhazen.cli.upload_sftp import KNOWN_HOSTS_FILE, SftpSession, SftpTransport
from alhazen.cli.upload_transport import (
    PARTIAL_DIR,
    RSYNC_MINIMUM,
    Cancelled,
    LocalCopy,
    Progress,
    Put,
    RsyncSsh,
    UploadError,
    UploadSettings,
    _version,
    experiment_folder,
    is_version_of,
    load_settings,
    parse_sha256,
    save_settings,
    sha256_file,
    versioned,
)
from alhazen.cli.workspace_upload import Uploads
from alhazen.data.manifest import verify_manifest, write_manifest

SLUG = "demo-experiment"
RUN = "v0.1.0/sub-01/ses-001/run-01_task-demo"
RUN2 = "v0.1.0/sub-02/ses-001/run-01_task-demo"
FILES = {"session.json", "sub-01_ses-001_run-01_trials.csv", "figures/summary.png", "manifest.yaml"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for ws in base.workspace.__wrapped__(tmp_path, monkeypatch):
        root = Path(ws.projects[0]["path"])
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{SLUG}"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        yield ws


def session(root: Path, run: str = RUN, *, complete: bool = True, data: str = "data") -> Path:
    """A session folder as a session leaves it, and beside it in the data
    folder what sessions also write there."""
    folder = root / data / run
    (folder / "figures").mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"task": "demo", "mode": "run"}))
    (folder / "sub-01_ses-001_run-01_trials.csv").write_text("trial,rt\n1,0.25\n2,0.31\n")
    (folder / "figures" / "summary.png").write_bytes(os.urandom(4096))
    if complete:
        write_manifest(folder, folder / "manifest.yaml", experiment_version="0.1.0")
    (root / data / "participants.tsv").write_text("participant_id\tinitials\nsub-01\tHD\n")
    (root / data / "calibrations").mkdir(exist_ok=True)
    (root / data / "calibrations" / "gaze.json").write_text("{}")
    db = sqlite3.connect(root / data / "experiment.sqlite3")
    db.execute("create table if not exists runs (id text)")
    db.execute("insert into runs values ('r1')")
    db.commit()
    db.close()
    return folder


def roots(call, key):
    status, out = call(f"/api/data/roots?project={key}")
    assert status == 200, out
    return {r["kind"]: r["id"] for r in out["roots"]}


def wait_job(call, timeout=30):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        status, out = call("/api/upload/job")
        job = out["job"]
        if job and job["phase"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"upload did not finish: {job}")


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / "archive"
    folder.mkdir()
    return folder


@pytest.fixture
def local(http, workspace, archive):  # noqa: F811
    call, server = http
    status, out = call(
        "/api/upload/settings", {"settings": {"transport": "local", "local_path": str(archive)}}
    )
    assert status == 200, out
    return call, server


def upload(call, key, selection, **extra):
    status, out = call("/api/upload/start", {"project": key, "selection": selection, **extra})
    assert status == 200, out
    return wait_job(call)


class TestSettings:
    def test_the_code_ships_no_destination(self, tmp_path):
        settings = load_settings(tmp_path)
        assert (settings.user, settings.host, settings.base_path) == ("", "", "")
        assert settings.transport == "sftp" and settings.port == 22
        assert settings.label == "archive"
        with pytest.raises(UploadError, match="login user, remote host, remote base path"):
            settings.remote_folder("x")

    def test_saved_in_the_workspace_and_read_back(self, tmp_path):
        fields = {"user": "alice", "base_path": "/remote/archive/", "label": "Vault", "port": 2222}
        save_settings(tmp_path, fields)
        again = load_settings(tmp_path)
        assert again.user == "alice" and again.base_path == "/remote/archive"
        assert again.label == "Vault" and again.port == 2222
        assert json.loads((tmp_path / "upload.json").read_text())["user"] == "alice"

    @pytest.mark.parametrize(
        "fields",
        [
            {"user": "-oProxyCommand=x"},
            {"user": "a b"},
            {"host": "-oProxyCommand=x"},
            {"base_path": "relative/path"},
            {"base_path": "/remote/../etc"},
            {"base_path": "/remote/with space"},
            {"base_path": "/remote/$(id)"},
            {"label": "<b>x</b>"},
            {"port": 0},
            {"transport": "ftp"},
            {"surprise": 1},
        ],
    )
    def test_refused_and_nothing_written(self, tmp_path, fields):
        with pytest.raises(ValueError):
            save_settings(tmp_path, fields)
        assert not (tmp_path / "upload.json").exists()

    def test_names(self):
        assert experiment_folder("amodal-averaging") == "amodal-averaging"
        for bad in ("a b", "../x", "", ".."):
            with pytest.raises(UploadError):
                experiment_folder(bad)
        assert versioned("participants.tsv", "20261008T174512Z") == (
            "participants.20261008T174512Z.tsv"
        )
        assert versioned("a/b/notes", "20261008T174512Z") == "a/b/notes.20261008T174512Z"
        assert is_version_of("participants.20261008T174512Z.tsv", "participants.tsv")
        assert not is_version_of("x/participants.20261008T174512Z.tsv", "participants.tsv")
        assert not is_version_of("participants.tsv", "participants.tsv")

    def test_sha256_output_is_matched_by_order(self):
        a, b = "a" * 64, "b" * 64
        assert parse_sha256(f"{a}  x\n\\{b}  y\\nz\n", ["x", "y\nz"]) == {"x": a, "y\nz": b}
        assert parse_sha256(f"{a}  x\n", ["x", "y"]) == {}
        assert parse_sha256("This account is currently not available.\n", ["x"]) == {}


class TestLocalUpload:
    def test_the_whole_data_folder_registry_and_receipts(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        workspace.people.add_subject(key, {"code": "01", "initials": "HD"})
        root = Path(workspace.project(key)["path"])
        folder = session(root)
        real = roots(call, key)["real"]
        status, preview = call(
            "/api/upload/preview", {"project": key, "selection": [{"root": real, "all": True}]}
        )
        assert status == 200, preview
        data, people = preview["groups"]
        assert data["destination"] == str(archive / SLUG)
        rows = {r["item"]: r for r in data["items"]}
        assert rows[RUN]["new"] == 4 and rows[RUN]["kind"] == "session"
        assert rows["_shared"]["new"] == 3  # participants.tsv, calibrations, experiment.sqlite3
        assert people["kind"] == "people" and {r["item"] for r in people["items"]} == {"_people"}
        assert not (archive / SLUG).exists()  # a preview copies nothing

        job = upload(call, key, [{"root": real, "all": True}])
        assert job["phase"] == "done", job
        assert job["results"][f"{real}:{RUN}"]["status"] == "verified"
        assert job["results"][f"{real}:_shared"]["status"] == "verified"
        assert job["results"]["people:_people"]["status"] == "verified"
        target = archive / SLUG
        for name in FILES:
            assert (target / RUN / name).read_bytes() == (folder / name).read_bytes()
        assert (target / "participants.tsv").is_file()
        assert (target / "calibrations" / "gaze.json").is_file()
        copy = sqlite3.connect(target / "experiment.sqlite3")
        assert copy.execute("select id from runs").fetchall() == [("r1",)]
        copy.close()
        assert (target / "people" / "people.sqlite3").is_file()
        assert (target / "people" / "csv" / key / "subjects.csv").is_file()
        assert not list(archive.rglob(PARTIAL_DIR))
        # The session folder is as the session left it: receipts sit beside.
        assert verify_manifest(folder, folder / "manifest.yaml") == []
        (receipt,) = receipts(root / "data", RUN)
        assert receipt["status"] == "verified" and receipt["local_manifest_problems"] == []
        assert {f["path"] for f in receipt["files"]} == {f"{RUN}/{n}" for n in FILES}
        assert all(len(f["sha256"]) == 64 for f in receipt["files"])
        assert (root / "data" / RECEIPTS_DIR / "_shared").is_dir()
        status, history = call(f"/api/manage/history?project={key}")
        (row,) = history["sessions"]  # uploads/ is not mistaken for a run
        assert row["upload"]["status"] == "verified"

    def test_a_database_in_wal_mode_is_snapshot_without_touching_the_folder(
        self, local, workspace, archive
    ):
        call, _ = local
        key = pid(workspace)
        root = Path(workspace.project(key)["path"])
        session(root)
        live = sqlite3.connect(root / "data" / "experiment.sqlite3")
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("insert into runs values ('only-in-the-wal')")
        live.commit()  # committed, still in experiment.sqlite3-wal
        try:
            assert (root / "data" / "experiment.sqlite3-wal").is_file()
            before = sorted(p.name for p in (root / "data").iterdir())
            selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
            call("/api/upload/preview", {"project": key, "selection": selection})
            assert upload(call, key, selection)["phase"] == "done"
            after = sorted(p.name for p in (root / "data").iterdir())
            assert after == sorted([*before, RECEIPTS_DIR])  # nothing else appeared
        finally:
            live.close()
        names = {p.name for p in (archive / SLUG).iterdir()}
        assert not {n for n in names if n.endswith(("-wal", "-shm", "-journal"))}
        copy = sqlite3.connect(archive / SLUG / "experiment.sqlite3")
        rows = {r for (r,) in copy.execute("select id from runs")}
        mode = copy.execute("PRAGMA journal_mode").fetchone()[0]
        copy.close()
        assert rows == {"r1", "only-in-the-wal"} and mode == "delete"

    def test_again_copies_nothing_and_never_deletes(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        assert upload(call, key, selection)["phase"] == "done"
        target = archive / SLUG / RUN
        extra = target / "added-at-the-archive.txt"
        extra.write_text("someone else's file")
        before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
        job = upload(call, key, selection)
        assert job["phase"] == "done"
        assert {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()} == before
        assert extra.read_text() == "someone else's file"
        root = Path(workspace.project(key)["path"]) / "data"
        assert latest(root, RUN)["attempts"] == 2
        assert receipts(root, RUN)[0]["already_there"]  # nothing copied the second time

    def test_a_changed_file_outside_sessions_is_kept_as_a_new_version(
        self, local, workspace, archive
    ):
        call, _ = local
        key = pid(workspace)
        root = Path(workspace.project(key)["path"])
        session(root)
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        assert upload(call, key, selection)["phase"] == "done"
        tsv = root / "data" / "participants.tsv"
        tsv.write_text(tsv.read_text() + "sub-02\tAB\n")
        status, preview = call("/api/upload/preview", {"project": key, "selection": selection})
        shared = next(r for r in preview["groups"][0]["items"] if r["item"] == "_shared")
        assert shared["version"] == 1 and shared["versions"] == ["participants.tsv"]
        assert upload(call, key, selection)["phase"] == "done"
        there = archive / SLUG
        versions = [p for p in there.iterdir() if is_version_of(p.name, "participants.tsv")]
        assert len(versions) == 1 and versions[0].read_text().endswith("sub-02\tAB\n")
        assert "sub-02" not in (there / "participants.tsv").read_text()  # never replaced
        (receipt,) = receipts(root / "data", "_shared")[:1]
        assert receipt["new_versions"] == {"participants.tsv": versions[0].name}
        # The same content again finds that version: no third copy.
        assert upload(call, key, selection)["phase"] == "done"
        assert len([p for p in there.iterdir() if is_version_of(p.name, "participants.tsv")]) == 1

    def test_a_different_file_in_a_session_is_a_conflict_left_alone(
        self, local, workspace, archive
    ):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        theirs = archive / SLUG / RUN / "session.json"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("a different session.json")
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        job = upload(call, key, selection)
        assert job["results"][f"{roots(call, key)['real']}:{RUN}"]["status"] == "conflict"
        assert theirs.read_text() == "a different session.json"
        assert sorted(p.name for p in theirs.parent.iterdir()) == sorted(
            ["session.json", "sub-01_ses-001_run-01_trials.csv", "figures", "manifest.yaml"]
        )  # no versioned copy inside a session folder
        root = Path(workspace.project(key)["path"]) / "data"
        (receipt,) = receipts(root, RUN)
        assert receipt["conflicts"] == [f"{RUN}/session.json"] and receipt["verified"] is False

    def test_incomplete_sessions_need_asking_for(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]), complete=False)
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        job = upload(call, key, selection)
        assert any("no manifest" in s for s in job["skipped"])
        assert not (archive / SLUG / RUN).exists()
        assert (archive / SLUG / "participants.tsv").is_file()  # the rest still went
        job = upload(call, key, selection, include_incomplete=True)
        assert (archive / SLUG / RUN / "session.json").is_file()
        (receipt,) = receipts(Path(workspace.project(key)["path"]) / "data", RUN)
        assert receipt["status"] == "verified"
        assert receipt["local_manifest_problems"] == ["no manifest.yaml"]

    def test_rehearsal_goes_to_its_own_folder(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        root = Path(workspace.project(key)["path"])
        session(root, data="data-rehearsal")
        (root / "data").mkdir()
        job = upload(call, key, [{"root": roots(call, key)["rehearsal"], "runs": [RUN]}])
        assert job["phase"] == "done"
        assert (archive / f"{SLUG}-rehearsal" / RUN / "session.json").is_file()
        assert (archive / f"{SLUG}-rehearsal" / "participants.tsv").is_file()
        assert not (archive / SLUG / RUN).exists()  # never among the real sessions

    def test_only_the_chosen_sessions(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        root = Path(workspace.project(key)["path"])
        session(root)
        session(root, RUN2)
        upload(call, key, [{"root": roots(call, key)["real"], "runs": [RUN2]}])
        assert (archive / SLUG / RUN2).is_dir() and not (archive / SLUG / RUN).exists()

    @pytest.mark.parametrize(
        "selection, code",
        [
            ([{"root": "nope", "runs": [RUN]}], 404),
            ([{"root": "REAL", "runs": ["../../etc"]}], 400),
            ([{"root": "REAL", "runs": [RUN, RUN]}], 400),
            ([], 400),
            ("all", 400),
        ],
    )
    def test_refused_selections(self, local, workspace, selection, code):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        real = roots(call, key)["real"]
        selection = json.loads(json.dumps(selection).replace("REAL", real))
        status, out = call("/api/upload/preview", {"project": key, "selection": selection})
        assert status == code, out

    def test_access_rules_hold(self, local, workspace):
        call, _ = local
        assert call("/api/upload/job", headers={"X-Alhazen-Token": "wrong"})[0] == 403
        assert call("/api/upload/nothing")[0] == 404


class Scripted:
    """A transport that fails, or waits to be cancelled, on cue."""

    def __init__(self, fail_in=None, block=False):
        self.fail_in, self.block = fail_in, block
        self.started = threading.Event()

    def destination(self, folder):
        return f"fake:/{folder}"

    def check(self):
        return {"ok": True, "message": "fake"}

    def listing(self, folder):
        return {}

    def checksums(self, folder, paths):
        return {}

    def put(self, folder, puts, progress, cancelled):
        self.started.set()
        progress(Progress(5, 10, "x"))
        if self.block:
            assert cancelled.wait(10)
            raise Cancelled("stopped")
        if self.fail_in == "copy":
            raise UploadError("Upload failed: the connection broke while copying")
        if self.fail_in == "verify":
            return {}
        return {p.remote: p.sha256 for p in puts}


class TestFailures:
    def uploads(self, workspace, transport):
        from alhazen.cli.workspace_data import DataView

        return Uploads(workspace, DataView(workspace), transport)

    def selection(self, uploads, workspace):
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        (real,) = uploads.data.roots(key)["roots"]
        return key, [{"root": real["id"], "runs": [RUN]}]

    def finished(self, uploads):
        uploads._thread.join(timeout=10)
        return uploads.job()["job"]

    def test_a_failed_copy_says_why_and_leaves_a_failed_receipt(self, workspace):
        uploads = self.uploads(workspace, Scripted(fail_in="copy"))
        key, selection = self.selection(uploads, workspace)
        uploads.start(key, selection, False)
        job = self.finished(uploads)
        assert job["phase"] == "failed" and "connection broke" in job["error"]
        (receipt,) = receipts(Path(workspace.project(key)["path"]) / "data", RUN)
        assert receipt["status"] == "failed" and "connection broke" in receipt["error"]

    def test_a_file_not_there_after_the_copy_is_incomplete(self, workspace):
        uploads = self.uploads(workspace, Scripted(fail_in="verify"))
        key, selection = self.selection(uploads, workspace)
        uploads.start(key, selection, False)
        job = self.finished(uploads)
        assert job["phase"] == "done"
        assert job["results"][f"{selection[0]['root']}:{RUN}"]["status"] == "incomplete"

    def test_cancel_and_one_at_a_time(self, workspace):
        transport = Scripted(block=True)
        uploads = self.uploads(workspace, transport)
        key, selection = self.selection(uploads, workspace)
        uploads.start(key, selection, False)
        assert transport.started.wait(5)
        with pytest.raises(ValueError, match="already running"):
            uploads.start(key, selection, False)
        assert uploads.job()["job"]["progress"]["bytes_done"] == 5
        uploads.cancel()
        assert self.finished(uploads)["phase"] == "cancelled"
        with pytest.raises(ValueError, match="No upload is running"):
            uploads.cancel()


class TestReceipts:
    def test_added_never_replaced_and_unreadable_ones_said(self, tmp_path):
        write_receipt(tmp_path, RUN, {"status": "failed"})
        time.sleep(0.002)
        write_receipt(tmp_path, RUN, {"status": "verified", "files": [{"path": "a"}]})
        (tmp_path / RECEIPTS_DIR / RUN / "99999999.json").write_text("{nope")
        found = receipts(tmp_path, RUN)
        assert "error" in found[0] and found[1]["status"] == "verified"
        assert latest(tmp_path, RUN)["status"] == "unreadable"
        with pytest.raises(ValueError):
            write_receipt(tmp_path, RUN, {"status": "fine"})


class TestLaunchSession:
    def test_the_run_page_finds_what_a_launch_wrote(self, local, workspace, archive):
        from tests.unit.test_workspace_manage import SESSION_RUN_PY

        call, _ = local
        key = pid(workspace)
        project = workspace.project(key)
        project["capabilities"] = ["experimenter"]
        root = Path(project["path"])
        (root / "run.py").write_text(SESSION_RUN_PY, encoding="utf-8")
        registry = workspace.people
        subject = registry.add_subject(key, {"code": "007", "initials": "HD"})
        who = registry.add_experimenter({"name": "Zoë Lee"})
        registry.assign(key, who["id"])
        run = base.finish(
            workspace,
            workspace.start(
                base.request_for(
                    workspace, mode="test", subject_record=subject["id"], experimenter=who["id"]
                )
            ),
        )
        assert run["status"] == "completed", run["log"]
        status, out = call(f"/api/upload/launch-session?project={key}&launch={run['id']}")
        found = out["session"]
        assert found["run"] == "v0.1.0/sub-007/ses-001/run-01_task-demo"
        assert found["root_kind"] == "rehearsal" and found["upload"] is None
        assert (
            Path(found["destination"]).resolve()
            == (Path(archive) / f"{SLUG}-rehearsal" / found["run"]).resolve()
        )
        assert call(f"/api/upload/launch-session?project={key}&launch=nope")[0] == 404


# -- SFTP: the default transport, against a stand-in server ------------------------


def settings_for(server, base_path):
    return UploadSettings(
        user="alice", host="127.0.0.1", port=server.port, base_path=remote_path(base_path)
    )


def logged_in(tmp_path, server, base_path):
    session = SftpSession(
        tmp_path / "ws", user_known_hosts=tmp_path / "no-known-hosts", key_files=[], use_agent=False
    )
    (tmp_path / "ws").mkdir(exist_ok=True)
    state = session.connect(settings_for(server, base_path))
    if state["state"] == "hostkey":
        state = session.trust(state["fingerprint"])
    assert state["state"] == "prompt" and state["prompts"] == [
        {"text": "Password: ", "echo": False}
    ], state
    state = session.answer([server.password])
    assert state["state"] == "prompt" and state["prompts"][0]["echo"] is True, state
    state = session.answer([server.code])
    assert state["state"] == "connected", state
    return session


class TestSftpLogin:
    def test_host_key_then_password_then_second_factor(self, tmp_path):
        remote = tmp_path / "remote"
        remote.mkdir()
        with StandIn() as server:
            session = SftpSession(
                tmp_path, user_known_hosts=tmp_path / "none", key_files=[], use_agent=False
            )
            state = session.connect(settings_for(server, remote))
            assert state["state"] == "hostkey" and state["fingerprint"].startswith("SHA256:")
            with pytest.raises(UploadError, match="not the fingerprint"):
                session.trust("SHA256:something-else")
            state = session.trust(state["fingerprint"])
            assert state["state"] == "prompt"
            session.answer([server.password])
            assert session.answer([server.code])["state"] == "connected"
            assert (tmp_path / KNOWN_HOSTS_FILE).is_file()
            assert SftpTransport(session, settings_for(server, remote)).check()["ok"] is True
            session.close()
            # Known now: the next login goes straight to the questions.
            again = SftpSession(
                tmp_path, user_known_hosts=tmp_path / "none", key_files=[], use_agent=False
            )
            assert again.connect(settings_for(server, remote))["state"] == "prompt"
            again.close()

    def test_a_wrong_answer_is_refused(self, tmp_path):
        with StandIn() as server:
            session = SftpSession(
                tmp_path, user_known_hosts=tmp_path / "none", key_files=[], use_agent=False
            )
            state = session.connect(settings_for(server, tmp_path))
            session.trust(state["fingerprint"])
            state = session.answer(["wrong"])
            assert state["state"] == "failed" and "refused" in state["message"]
            with pytest.raises(UploadError, match="Not connected"):
                session.transport()

    def test_a_changed_host_key_refuses_the_connection(self, tmp_path):
        with StandIn() as first:
            logged_in(tmp_path, first, tmp_path).close()
            port = first.port
        (tmp_path / "ws" / KNOWN_HOSTS_FILE).write_text(
            (tmp_path / "ws" / KNOWN_HOSTS_FILE).read_text()
        )
        with StandIn() as second:  # another key; pretend it is the same host
            known = (tmp_path / "ws" / KNOWN_HOSTS_FILE).read_text()
            (tmp_path / "ws" / KNOWN_HOSTS_FILE).write_text(
                known.replace(f"[127.0.0.1]:{port}", f"[127.0.0.1]:{second.port}")
            )
            session = SftpSession(
                tmp_path / "ws", user_known_hosts=tmp_path / "none", key_files=[], use_agent=False
            )
            state = session.connect(settings_for(second, tmp_path))
            assert state["state"] == "failed" and "CHANGED" in state["message"]

    def test_unset_settings_are_said_before_dialing(self, tmp_path):
        session = SftpSession(tmp_path, key_files=[], use_agent=False)
        with pytest.raises(UploadError, match="remote host"):
            session.connect(UploadSettings(user="alice"))


class TestSftpTransport:
    def put_one(self, transport, local, name="a/b/file.bin"):
        item = Put(local, name, local.stat().st_size, sha256_file(local))
        seen: list[Progress] = []
        written = transport.put("exp", [item], seen.append, threading.Event())
        return written, seen

    def test_copy_verify_and_listing(self, tmp_path):
        remote = tmp_path / "remote"
        remote.mkdir()
        local = tmp_path / "file.bin"
        local.write_bytes(os.urandom(700_000))
        with StandIn() as server:
            session = logged_in(tmp_path, server, remote)
            transport = SftpTransport(session, settings_for(server, remote))
            assert transport.listing("exp") == {}
            written, seen = self.put_one(transport, local)
            assert written == {"a/b/file.bin": sha256_file(local)}
            assert seen[-1].bytes_done == seen[-1].bytes_total == 700_000
            assert (remote / "exp/a/b/file.bin").read_bytes() == local.read_bytes()
            assert not (remote / "exp/a/b" / PARTIAL_DIR).exists()
            assert transport.listing("exp") == {"a/b/file.bin": 700_000}
            # Two more in the same folder, in one put: its partial folder is
            # made again after the first one removed it.
            two = [Put(local, f"a/b/{n}", 700_000, sha256_file(local)) for n in ("c", "d")]
            assert set(transport.put("exp", two, lambda p: None, threading.Event())) == {
                "a/b/c",
                "a/b/d",
            }
            assert transport.checksums("exp", ["a/b/file.bin", "missing"]) == {
                "a/b/file.bin": sha256_file(local)
            }
            assert any(c.startswith("sha256sum") for c in server.commands)  # on the host
            assert transport.destination("exp") == (
                f"alice@127.0.0.1:{server.port}:{remote_path(remote)}/exp"
            )
            session.close()

    def test_resume_from_a_partial_copy_and_restart_a_bad_one(self, tmp_path):
        remote = tmp_path / "remote"
        local = tmp_path / "file.bin"
        payload = os.urandom(600_000)
        local.write_bytes(payload)
        partial = remote / "exp/a/b" / PARTIAL_DIR / "file.bin"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(payload[:400_000])
        with StandIn() as server:
            session = logged_in(tmp_path, server, remote)
            transport = SftpTransport(session, settings_for(server, remote))
            written, seen = self.put_one(transport, local)
            assert seen[0].bytes_done > 400_000  # it went on from the partial copy
            assert (remote / "exp/a/b/file.bin").read_bytes() == payload
            # A partial copy with wrong bytes is caught by its checksum and redone.
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"x" * 400_000)
            self.put_one(transport, local, "a/b/other.bin")  # unrelated name: new file
            partial.rename(partial.with_name("again.bin"))
            written, _ = self.put_one(transport, local, "a/b/again.bin")
            assert (remote / "exp/a/b/again.bin").read_bytes() == payload
            session.close()

    def test_never_replaces_and_verifies_by_reading_when_commands_are_refused(self, tmp_path):
        remote = tmp_path / "remote"
        theirs = remote / "exp/a/b/file.bin"
        theirs.parent.mkdir(parents=True)
        theirs.write_bytes(b"theirs")
        local = tmp_path / "file.bin"
        local.write_bytes(b"mine, different")
        with StandIn(exec_ok=False) as server:
            session = logged_in(tmp_path, server, remote)
            transport = SftpTransport(session, settings_for(server, remote))
            written, _ = self.put_one(transport, local)
            assert written == {} and theirs.read_bytes() == b"theirs"
            digest = transport.checksums("exp", ["a/b/file.bin"])
            assert digest == {"a/b/file.bin": sha256_file(theirs)}  # read back over SFTP
            assert server.commands == []
            session.close()

    def test_cancel_keeps_the_partial_copy(self, tmp_path):
        remote = tmp_path / "remote"
        local = tmp_path / "file.bin"
        local.write_bytes(os.urandom(2_000_000))
        with StandIn() as server:
            session = logged_in(tmp_path, server, remote)
            transport = SftpTransport(session, settings_for(server, remote))
            stop = threading.Event()

            def progress(p):
                if p.bytes_done > 500_000:
                    stop.set()

            item = Put(local, "file.bin", 2_000_000, sha256_file(local))
            with pytest.raises(Cancelled):
                transport.put("exp", [item], progress, stop)
            assert not (remote / "exp/file.bin").exists()
            assert (remote / "exp" / PARTIAL_DIR / "file.bin").stat().st_size > 0
            session.close()


class TestSftpThroughTheDashboard:
    def test_login_and_upload_from_the_page(self, http, workspace, tmp_path):  # noqa: F811
        call, server_ = http
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        remote = tmp_path / "remote"
        remote.mkdir()
        server_.uploads.sftp.user_known_hosts = tmp_path / "none"
        server_.uploads.sftp.key_files = []
        server_.uploads.sftp.use_agent = False
        with StandIn() as server:
            status, _ = call(
                "/api/upload/settings",
                {
                    "settings": {
                        "user": "alice",
                        "host": "127.0.0.1",
                        "port": server.port,
                        "base_path": remote_path(remote),
                        "label": "Vault",
                    }
                },
            )
            assert status == 200
            assert call("/api/upload/check")[1]["login"] is True
            status, state = call("/api/upload/login", {"action": "connect"})
            assert state["state"] == "hostkey"
            state = call(
                "/api/upload/login", {"action": "trust", "fingerprint": state["fingerprint"]}
            )[1]
            state = call("/api/upload/login", {"action": "answer", "answers": [server.password]})[1]
            state = call("/api/upload/login", {"action": "answer", "answers": [server.code]})[1]
            assert state["state"] == "connected"
            assert call("/api/upload/check")[1]["ok"] is True
            job = upload(call, key, [{"root": roots(call, key)["real"], "all": True}])
            assert job["phase"] == "done", (job["error"], job["results"])
            assert {r["status"] for r in job["results"].values()} == {"verified"}
            assert (remote / SLUG / RUN / "session.json").is_file()
            assert (remote / SLUG / "people" / "people.sqlite3").is_file()
            status, state = call("/api/upload/login", {"action": "disconnect"})
            assert state["state"] == "disconnected"


# -- rsync over a stand-in ssh (optional transport) ----------------------------------

FAKE_SSH = r"""#!{python}
# A stand-in for ssh: "connects" by running the remote command here. It
# answers -O check from a marker file (the master connection) and records
# every invocation.
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as stream:
    stream.write(repr(args) + "\n")
if "-O" in args:
    sys.exit(0 if os.path.exists(os.environ["FAKE_SSH_MASTER"]) else 255)
rest, i = [], 0
while i < len(args):
    if args[i] in ("-o", "-l", "-p"):
        i += 2
        continue
    rest = args[i + 1:]
    break
sys.exit(subprocess.call(" ".join(rest), shell=True))
"""


def _rsync_usable() -> bool:
    """Whether this computer has an rsync the transport accepts (3.1 or
    newer; macOS ships 2.6, which the transport refuses by design)."""
    if shutil.which("rsync") is None or sys.platform == "win32":
        return False
    answer = subprocess.run(["rsync", "--version"], capture_output=True, text=True, timeout=10)
    version = _version(answer.stdout)
    return version is not None and version >= RSYNC_MINIMUM


needs_rsync = pytest.mark.skipif(
    not _rsync_usable(),
    reason="no rsync 3.1 or newer here (the optional transport; SFTP needs nothing)",
)


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "fake-ssh"
    script.parent.mkdir()
    script.write_text(FAKE_SSH.replace("{python}", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    master = tmp_path / "master-open"
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_MASTER", str(master))
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return script, master, log


@needs_rsync
class TestRsyncSsh:
    def transport(self, fake_ssh, base_path):
        script, _, _ = fake_ssh
        return RsyncSsh(
            UploadSettings(
                user="alice",
                host="archive.example.org",
                base_path=str(base_path),
                ssh_command=str(script),
                transport="rsync",
            )
        )

    def test_check_asks_for_the_connection_first(self, fake_ssh, tmp_path):
        _, master, _ = fake_ssh
        remote = tmp_path / "remote"
        remote.mkdir()
        transport = self.transport(fake_ssh, remote)
        answer = transport.check()
        assert answer["ok"] is False and "second factor" in answer["message"]
        assert "ControlMaster=yes" in answer["command"]
        master.touch()
        assert transport.check()["ok"] is True

    def test_listing_put_checksums_and_no_delete(self, fake_ssh, tmp_path):
        _, master, log = fake_ssh
        master.touch()
        remote = tmp_path / "remote"
        remote.mkdir()
        folder = session(tmp_path / "exp")
        transport = self.transport(fake_ssh, remote)
        assert transport.listing("exp") == {}
        puts = [
            Put(folder / n, f"{RUN}/{n}", (folder / n).stat().st_size, sha256_file(folder / n))
            for n in sorted(FILES)
        ]
        tsv = tmp_path / "exp" / "data" / "participants.tsv"
        puts.append(
            Put(tsv, "participants.20261008T000000Z.tsv", tsv.stat().st_size, sha256_file(tsv))
        )  # uploaded under another name
        seen: list[Progress] = []
        transport.put("exp", puts, seen.append, threading.Event())
        assert seen[-1].bytes_done == seen[-1].bytes_total
        listed = transport.listing("exp")
        assert set(listed) == {p.remote for p in puts}
        sums = transport.checksums("exp", sorted(listed))
        assert all(sums[p.remote] == p.sha256 for p in puts)
        (remote / "exp" / RUN / "session.json").write_text("changed at the archive")
        (remote / "exp" / "theirs.txt").write_text("keep")
        transport.put("exp", puts, lambda p: None, threading.Event())
        assert (remote / "exp" / RUN / "session.json").read_text() == "changed at the archive"
        assert (remote / "exp" / "theirs.txt").read_text() == "keep"
        text = log.read_text()
        assert "--delete" not in text and "BatchMode=yes" in text

    def test_old_rsync_is_refused(self, fake_ssh):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "rsync  version 2.6.9  protocol 29", "")

        transport = self.transport(fake_ssh, "/remote")
        transport.run = run
        answer = transport.check()
        assert answer["ok"] is False and "3.1 or newer" in answer["message"]


def test_a_local_target_needs_its_folder(tmp_path):
    with pytest.raises(UploadError, match="local folder"):
        LocalCopy.from_settings(UploadSettings(transport="local"))
    assert LocalCopy(tmp_path / "missing").check()["ok"] is False


def test_paramiko_is_a_core_dependency():
    # The default transport must work on a fresh install, on any system.
    text = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    core = text.split("[project.optional-dependencies]")[0]
    assert '"paramiko>=' in core
    assert paramiko.__version__
