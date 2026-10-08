"""Upload to the archive (workspace_upload.py, upload_transport.py,
upload_receipts.py): settings, preview, copy, verify, receipts and the
rules that make re-uploading safe — over real HTTP, against a folder on this
computer (LocalCopy), a scripted transport for failures, and real rsync
through a stand-in ssh that runs the "remote" side locally."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from tests.unit import test_workspace as base
from tests.unit.test_workspace_manage import http, pid  # noqa: F401  (fixtures)

from alhazen.cli.upload_receipts import RECEIPTS_DIR, latest, receipts, write_receipt
from alhazen.cli.upload_transport import (
    PARTIAL_DIR,
    Cancelled,
    FilePlan,
    Item,
    LocalCopy,
    Progress,
    SshRsync,
    UploadError,
    UploadSettings,
    experiment_folder,
    load_settings,
    save_settings,
)
from alhazen.cli.workspace_upload import Uploads
from alhazen.data.manifest import verify_manifest, write_manifest

SLUG = "demo-experiment"
RUN = "v0.1.0/sub-01/ses-001/run-01_task-demo"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for ws in base.workspace.__wrapped__(tmp_path, monkeypatch):
        root = Path(ws.projects[0]["path"])
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{SLUG}"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        yield ws


def session(root: Path, run: str = RUN, *, complete: bool = True, data: str = "data") -> Path:
    """A session folder as a session leaves it: tables, a card, a figure,
    and (complete) its manifest."""
    folder = root / data / run
    (folder / "figures").mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"task": "demo", "mode": "run"}))
    (folder / "sub-01_ses-001_run-01_trials.csv").write_text("trial,rt\n1,0.25\n2,0.31\n")
    (folder / "figures" / "summary.png").write_bytes(os.urandom(4096))
    if complete:
        write_manifest(folder, folder / "manifest.yaml", experiment_version="0.1.0")
    return folder


def roots(call, key):
    status, out = call(f"/api/data/roots?project={key}")
    assert status == 200, out
    return {r["kind"]: r["id"] for r in out["roots"]}


def wait_job(call, timeout=20):
    deadline = time.time() + timeout
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


class TestSettings:
    def test_the_code_ships_no_destination(self, tmp_path):
        # The host, user and base path are the operator's, typed in on this
        # computer: nothing to point at by default.
        settings = load_settings(tmp_path)
        assert (settings.user, settings.host, settings.base_path) == ("", "", "")
        assert settings.transport == "ssh" and settings.label == "archive"
        assert SshRsync(settings).check()["ok"] is False

    def test_saved_in_the_workspace_and_read_back(self, tmp_path):
        fields = {"user": "alice", "base_path": "/remote/archive/", "label": "Vault"}
        save_settings(tmp_path, fields)
        again = load_settings(tmp_path)
        assert again.user == "alice" and again.base_path == "/remote/archive"
        assert again.label == "Vault"
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
            {"surprise": 1},
        ],
    )
    def test_refused_and_nothing_written(self, tmp_path, fields):
        with pytest.raises(ValueError):
            save_settings(tmp_path, fields)
        assert not (tmp_path / "upload.json").exists()

    def test_experiment_folder_is_the_name_exactly(self):
        assert experiment_folder("amodal-averaging") == "amodal-averaging"
        for bad in ("a b", "../x", "", ".."):
            with pytest.raises(UploadError):
                experiment_folder(bad)

    def test_settings_route(self, http):  # noqa: F811
        call, _ = http
        status, out = call("/api/upload/settings")
        assert status == 200 and out["settings"]["base_path"] == ""
        assert out["open_command"] is None  # no login user or host yet
        status, out = call(
            "/api/upload/settings",
            {"settings": {"user": "alice", "host": "archive.example.org"}},
        )
        assert status == 200
        assert out["open_command"] == (
            "ssh -fN -o ControlMaster=yes -o ControlPersist=12h "
            "-o 'ControlPath=~/.ssh/cm-%r@%h:%p' alice@archive.example.org"
        )
        status, out = call("/api/upload/settings", {"settings": {"base_path": "nope"}})
        assert status == 400 and "absolute" in out["error"]


class TestLocalUpload:
    def test_preview_copy_verify_and_receipt(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        folder = session(Path(workspace.project(key)["path"]))
        real = roots(call, key)["real"]
        selection = [{"root": real, "runs": [RUN]}]
        status, preview = call("/api/upload/preview", {"project": key, "selection": selection})
        assert status == 200, preview
        (group,) = preview["groups"]
        assert group["destination"] == str(archive / SLUG)
        (one,) = group["sessions"]
        assert one["new"] == one["files"] == 4 and one["present"] == one["conflict"] == 0
        assert one["upload"] is None and one["complete"] is True
        assert not (archive / SLUG).exists()  # a preview copies nothing

        status, out = call("/api/upload/start", {"project": key, "selection": selection})
        assert status == 200, out
        job = wait_job(call)
        assert job["phase"] == "done", job
        assert job["results"][f"{real}:{RUN}"]["status"] == "verified"
        target = archive / SLUG / RUN
        for name in ("session.json", "manifest.yaml", "figures/summary.png"):
            assert (target / name).read_bytes() == (folder / name).read_bytes()
        assert not list(target.rglob(PARTIAL_DIR))
        # The session folder is exactly as the session left it: the receipt
        # sits beside it, not inside, so its manifest still verifies.
        assert verify_manifest(folder, folder / "manifest.yaml") == []
        (receipt,) = receipts(folder.parents[3], RUN)
        assert receipt["status"] == "verified" and receipt["verified"] is True
        assert receipt["destination"]["path"] == f"{archive / SLUG}/{RUN}"
        assert {f["path"] for f in receipt["files"]} == {
            "session.json",
            "sub-01_ses-001_run-01_trials.csv",
            "figures/summary.png",
            "manifest.yaml",
        }
        assert all(len(f["sha256"]) == 64 for f in receipt["files"])
        assert receipt["local_manifest_problems"] == []
        assert (folder.parents[3] / RECEIPTS_DIR / RUN).is_dir()
        # History shows the state.
        status, history = call(f"/api/manage/history?project={key}")
        (row,) = history["sessions"]  # uploads/ is not mistaken for a run
        assert row["upload"]["status"] == "verified" and row["upload"]["ever_verified"]

    def test_again_copies_nothing_and_never_replaces_or_deletes(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        selection = [{"root": roots(call, key)["real"], "all": True}]
        call("/api/upload/start", {"project": key, "selection": selection})
        assert wait_job(call)["phase"] == "done"
        target = archive / SLUG / RUN
        extra = target / "added-at-the-archive.txt"
        extra.write_text("someone else's file")
        before = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
        status, preview = call("/api/upload/preview", {"project": key, "selection": selection})
        (one,) = preview["groups"][0]["sessions"]
        assert one["new"] == 0 and one["present"] == 4 and one["upload"]["status"] == "verified"
        call("/api/upload/start", {"project": key, "selection": selection})
        assert wait_job(call)["phase"] == "done"
        assert {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()} == before
        assert extra.read_text() == "someone else's file"  # never deleted
        assert latest(Path(workspace.project(key)["path"]) / "data", RUN)["attempts"] == 2

    def test_a_different_file_at_the_destination_is_a_conflict_left_alone(
        self, local, workspace, archive
    ):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]))
        theirs = archive / SLUG / RUN / "session.json"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("a different session.json")
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        status, preview = call("/api/upload/preview", {"project": key, "selection": selection})
        (one,) = preview["groups"][0]["sessions"]
        assert one["conflict"] == 1 and one["conflicts"] == ["session.json"] and one["new"] == 3
        call("/api/upload/start", {"project": key, "selection": selection})
        job = wait_job(call)
        assert job["phase"] == "done"
        (result,) = job["results"].values()
        assert result["status"] == "conflict"
        assert theirs.read_text() == "a different session.json"
        (receipt,) = receipts(Path(workspace.project(key)["path"]) / "data", RUN)
        assert receipt["conflicts"] == ["session.json"] and receipt["verified"] is False
        assert len(receipt["copied"]) == 3

    def test_incomplete_sessions_need_asking_for(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        session(Path(workspace.project(key)["path"]), complete=False)
        selection = [{"root": roots(call, key)["real"], "runs": [RUN]}]
        status, out = call("/api/upload/start", {"project": key, "selection": selection})
        assert status == 400 and "no manifest" in out["error"]
        assert not (archive / SLUG).exists()
        status, out = call(
            "/api/upload/start",
            {"project": key, "selection": selection, "include_incomplete": True},
        )
        assert status == 200
        job = wait_job(call)
        assert job["phase"] == "done"
        (receipt,) = receipts(Path(workspace.project(key)["path"]) / "data", RUN)
        assert receipt["status"] == "verified"
        assert receipt["local_manifest_problems"] == ["no manifest.yaml"]

    def test_rehearsal_goes_to_its_own_folder(self, local, workspace, archive):
        call, _ = local
        key = pid(workspace)
        root = Path(workspace.project(key)["path"])
        session(root, data="data-rehearsal")
        (root / "data").mkdir()
        selection = [{"root": roots(call, key)["rehearsal"], "runs": [RUN]}]
        call("/api/upload/start", {"project": key, "selection": selection})
        assert wait_job(call)["phase"] == "done"
        assert (archive / f"{SLUG}-rehearsal" / RUN / "session.json").is_file()
        assert not (archive / SLUG).exists()

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

    def __init__(self, fail_in: str | None = None, block: bool = False):
        self.fail_in, self.block = fail_in, block
        self.started = threading.Event()

    def destination(self, folder):
        return f"fake:/{folder}"

    def check(self):
        return {"ok": True, "message": "fake"}

    def plan(self, folder, items):
        return [FilePlan(i.relative, "session.json", 10, "new") for i in items]

    def copy(self, folder, items, progress, cancelled):
        self.started.set()
        progress(Progress(5, 10, "session.json"))
        if self.block:
            assert cancelled.wait(10)
            raise Cancelled("stopped")
        if self.fail_in == "copy":
            raise UploadError("Upload failed: the connection broke while copying")

    def verify(self, folder, items):
        return [(i.relative, "session.json") for i in items] if self.fail_in == "verify" else []


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
        assert receipt["status"] == "failed" and receipt["verified"] is False
        assert "connection broke" in receipt["error"]

    def test_a_file_missing_after_the_copy_is_incomplete(self, workspace):
        uploads = self.uploads(workspace, Scripted(fail_in="verify"))
        key, selection = self.selection(uploads, workspace)
        uploads.start(key, selection, False)
        job = self.finished(uploads)
        assert job["phase"] == "done"
        (result,) = job["results"].values()
        assert result["status"] == "incomplete"

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
        job = self.finished(uploads)
        assert job["phase"] == "cancelled"
        with pytest.raises(ValueError, match="No upload is running"):
            uploads.cancel()
        (receipt,) = receipts(Path(workspace.project(key)["path"]) / "data", RUN)
        assert receipt["status"] == "cancelled"


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
        assert status == 200
        found = out["session"]
        assert found["run"] == "v0.1.0/sub-007/ses-001/run-01_task-demo"
        assert found["root_kind"] == "rehearsal" and found["upload"] is None
        assert found["complete"] is False  # this stand-in session writes no manifest
        assert call(f"/api/upload/launch-session?project={key}&launch=nope")[0] == 404


# -- rsync over a stand-in ssh ----------------------------------------------------

FAKE_SSH = r"""#!{python}
# A stand-in for ssh: "connects" by running the remote command here. It
# answers -O check from a marker file (the master connection), and records
# every invocation so the test can read what was asked of ssh.
import os, subprocess, sys
args = sys.argv[1:]
log = os.environ["FAKE_SSH_LOG"]
with open(log, "a") as stream:
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

needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None or sys.platform == "win32",
    reason="rsync is not installed here (the sandbox and Linux/macOS CI have it)",
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
class TestSshRsync:
    def transport(self, fake_ssh, base_path):
        script, _, _ = fake_ssh
        return SshRsync(
            UploadSettings(
                user="alice",
                host="archive.example.org",
                base_path=str(base_path),
                ssh_command=str(script),
            )
        )

    def test_check_asks_for_the_connection_first(self, fake_ssh, tmp_path):
        script, master, _ = fake_ssh
        remote = tmp_path / "remote"
        remote.mkdir()
        transport = self.transport(fake_ssh, remote)
        answer = transport.check()
        assert answer["ok"] is False and "second factor" in answer["message"]
        assert "ControlMaster=yes" in answer["command"] and "-fN" in answer["command"]
        master.touch()
        assert transport.check()["ok"] is True
        shutil.rmtree(remote)
        answer = transport.check()
        assert answer["ok"] is False and "is not a folder" in answer["message"]

    def test_copy_verify_resume_and_no_delete(self, fake_ssh, tmp_path):
        _, master, log = fake_ssh
        master.touch()
        remote = tmp_path / "remote"
        remote.mkdir()
        project = tmp_path / "exp"
        folder = session(project)
        items = [Item(folder, RUN)]
        transport = self.transport(fake_ssh, remote)
        assert transport.destination("exp") == f"alice@archive.example.org:{remote}/exp"
        planned = transport.plan("exp", items)
        assert {p.path for p in planned} == {
            "session.json",
            "sub-01_ses-001_run-01_trials.csv",
            "figures/summary.png",
            "manifest.yaml",
        }
        assert {p.state for p in planned} == {"new"}
        seen: list[Progress] = []
        transport.copy("exp", items, seen.append, threading.Event())
        assert seen and seen[-1].bytes_done == seen[-1].bytes_total > 0
        target = remote / "exp" / RUN
        assert (target / "figures/summary.png").read_bytes() == (
            folder / "figures/summary.png"
        ).read_bytes()
        assert transport.verify("exp", items) == []
        # A file someone changed on the archive is reported, never replaced;
        # a file only the archive has is never deleted.
        (target / "session.json").write_text("changed at the archive")
        (target / "theirs.txt").write_text("keep")
        states = {p.path: p.state for p in transport.plan("exp", items)}
        assert states["session.json"] == "conflict" and states["manifest.yaml"] == "present"
        transport.copy("exp", items, lambda p: None, threading.Event())
        assert (target / "session.json").read_text() == "changed at the archive"
        assert (target / "theirs.txt").read_text() == "keep"
        assert transport.verify("exp", items) == [(RUN, "session.json")]
        # A file lost on the archive is copied again.
        (target / "figures/summary.png").unlink()
        transport.copy("exp", items, lambda p: None, threading.Event())
        assert (target / "figures/summary.png").is_file()
        text = log.read_text()
        assert "--delete" not in text and "BatchMode=yes" in text and "ControlMaster=no" in text

    def test_no_connection_is_a_clear_failure(self, fake_ssh, tmp_path):
        script, _, _ = fake_ssh
        transport = SshRsync(
            UploadSettings(
                user="alice",
                host="archive.example.org",
                base_path="/remote",
                ssh_command=str(script),
            ),
        )
        transport.settings = transport.settings.model_copy(update={"ssh_command": "false"})
        folder = session(tmp_path / "exp")
        with pytest.raises(UploadError):
            transport.copy("exp", [Item(folder, RUN)], lambda p: None, threading.Event())

    def test_old_rsync_is_refused(self, fake_ssh):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "rsync  version 2.6.9  protocol 29", "")

        transport = self.transport(fake_ssh, "/remote")
        transport.run = run
        answer = transport.check()
        assert answer["ok"] is False and "3.1 or newer" in answer["message"]


def test_sessions_need_a_local_folder_to_copy_into(tmp_path):
    with pytest.raises(UploadError, match="local folder"):
        LocalCopy.from_settings(UploadSettings(transport="local"))
    assert LocalCopy(tmp_path / "missing").check()["ok"] is False
