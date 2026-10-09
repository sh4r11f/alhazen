# ruff: noqa: F811  (pytest fixtures imported from test_rig_adapter)
"""Rig regressions for the data/failure review (data-review.md BL1, BL2,
MA2-MA5 and minors 5, 6, 7, 10, 13), against the stand-in hub."""

from __future__ import annotations

import json
import threading
import time
import unicodedata
from pathlib import Path

import pytest
from tests.hub.rig_support import FakeHub
from tests.hub.test_rig_adapter import (  # noqa: F401  (fixtures)
    API,
    connect,
    http,
    hub,
    preview,
    probes,
    project_id,
    root_id,
    run_folder,
    start,
    wait_job,
    workspace,
)
from tests.unit.test_workspace_upload import RUN

from alhazen.cli.workspace_hub import HubAdapter
from alhazen.data.manifest import write_manifest
from alhazen.hub import sync
from alhazen.hub.credentials import Credential, RigState


def session_with(workspace, files: dict[str, bytes]) -> Path:
    """A completed session folder holding exactly ``files`` (+ manifest)."""
    folder = Path(workspace.projects[0]["path"]) / "data" / RUN
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(
        json.dumps({"task": "demo", "mode": "run"}), encoding="utf-8"
    )
    for name, data in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    write_manifest(folder, folder / "manifest.yaml", experiment_version="0.1.0")
    return folder


def test_bl1_a_session_with_an_empty_file_completes(http, hub, workspace):
    call, _ = http
    session_with(workspace, {"events.log": b"", "trials.csv": b"trial\n1\n"})
    connect(call, hub)
    body, out = preview(call, workspace)
    job = wait_job(call, start(call, body, out)["id"])
    assert job["status"] == "completed", job
    stored = next(iter(hub.sessions.values()))
    assert "events.log" in stored["verified"]


class TestClosedAttempts:
    def test_bl2_an_expired_attempt_is_retried_under_the_same_identity(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.close_next_put = True
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed", job
        sessions = list(hub.sessions.values())
        assert [s["status"] for s in sessions] == ["expired", "committed"]
        assert job["session_id"] == sessions[1]["id"]
        assert sessions[1]["client_session_id"] == sessions[0]["client_session_id"].removesuffix(
            "~closed"
        )

    def test_bl2_a_hub_that_keeps_the_closed_attempt_leaves_it_resumable(
        self, http, hub, workspace
    ):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.replay_closed_as_new = False
        hub.close_next_put = True
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "failed" and job["error"]["code"] == "upload_closed"
        assert job["error"]["retryable"] is True
        hub.replay_closed_as_new = True
        status, answer = call(f"{API}/local/jobs/{job['id']}/resume", {})
        assert status == 200, answer
        assert wait_job(call, job["id"])["status"] == "completed"


class TestSeal:
    def test_ma2_sealing_in_progress_waits_with_retry_after(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.seal_busy = 2
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"], timeout=30)
        assert job["status"] == "completed", job
        assert len(hub.sessions) == 1

    def test_ma2_a_long_seal_pauses_resumably_with_the_same_session(self, http, hub, workspace):
        call, server = http
        server.hub().uploader.seal_wait_s = 0.5
        run_folder(workspace)
        connect(call, hub)
        hub.seal_busy = 100
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"], timeout=30)
        assert job["status"] == "paused" and job["error"]["code"] == "sealing_in_progress"
        assert job["error"]["retryable"] is True
        hub.seal_busy = 0
        call(f"{API}/local/jobs/{job['id']}/resume", {})
        done = wait_job(call, job["id"])
        assert done["status"] == "completed" and done["session_id"] == job["session_id"]
        assert len(hub.sessions) == 1


def test_ma3_a_stale_401_never_signs_out_a_newer_account(tmp_path):
    state = RigState(tmp_path / "hub")
    from alhazen.hub.credentials import Connection

    state.save_connection(Connection("https://hub.example.org"))
    a = Credential("https://hub.example.org", "tok-a", {"id": "ua", "username": "a"})
    b = Credential("https://hub.example.org", "tok-b", {"id": "ub", "username": "b"})
    state.save_credential(a)
    state.save_credential(b)
    assert state.clear_credential(expected=a) is False
    assert state.credential().token == "tok-b"
    assert state.clear_credential(expected=b) is True
    assert state.credential() is None


def test_ma3_adapter_401_race_keeps_the_new_sign_in(http, hub, workspace):
    call, server = http
    connect(call, hub, "alice")
    alice = next(iter(hub.tokens))
    adapter = server.hub()

    def race(request):
        # Bob signs in on the rig while alice's request is at the hub, and
        # alice's bearer has been revoked there.
        if request["auth"] == f"Bearer {alice}" and request["path"] == "/catalog":
            hub.tokens.pop(alice, None)
            adapter.state.save_credential(
                Credential(hub.base, "tok-bob-x", {"id": "u-bob", "username": "bob"})
            )
            hub.tokens["tok-bob-x"] = "bob"

    hub.on_request = race
    status, answer = call(f"{API}/catalog")
    assert status == 401
    hub.on_request = None
    assert adapter.state.credential().user_id == "u-bob"


def test_ma4_hashing_waits_within_a_file(tmp_path, monkeypatch, workspace, hub):
    monkeypatch.setattr(sync, "HASH_BLOCK_BYTES", 1024)
    session_with(workspace, {"big.bin": bytes(range(256)) * 64})  # 16 KiB, 16 blocks
    from alhazen.cli.dashboard import DashboardServer

    server = DashboardServer(workspace)
    calls = {"n": 0}
    hold = threading.Event()

    adapter = HubAdapter(workspace, server.data, uploader_wait_s=0.02, backoff_s=(0.01,))
    try:
        adapter.handle("POST", "/local/connect", {}, {"url": hub.base, "allow_http_loopback": True})
        adapter.handle(
            "POST", "/auth/login", {}, {"username": "alice", "password": hub.users["alice"]}
        )

        def busy():
            calls["n"] += 1
            return calls["n"] >= 8 and not hold.is_set()

        adapter.uploader.busy = busy
        roots = server.data.roots(project_id(workspace))["roots"]
        root = next(r["id"] for r in roots if r["kind"] == "real")
        body = {
            "project_id": project_id(workspace),
            "root_id": root,
            "run_id": RUN,
            "experiment_id": "e1",
            "version_id": "v1",
        }
        _, out = adapter.handle("POST", "/local/upload-preview", {}, body)
        _, answer = adapter.handle(
            "POST", "/local/upload", {}, {**body, "preview_id": out["preview_id"], "consent": True}
        )
        job_id = answer["job"]["id"]
        deadline = time.time() + 5
        while adapter.outbox.load(job_id)["status"] != "waiting" and time.time() < deadline:
            time.sleep(0.01)
        job = adapter.outbox.load(job_id)
        assert job["status"] == "waiting" and job["files"] is None  # mid-hash, waiting
        assert hub.sessions == {}
        hold.set()
        deadline = time.time() + 10
        while adapter.outbox.load(job_id)["status"] != "completed" and time.time() < deadline:
            time.sleep(0.02)
        assert adapter.outbox.load(job_id)["status"] == "completed"
    finally:
        adapter.close()
        server.server_close()


class TestRemoteAbort:
    def test_ma5_cancel_discards_the_hubs_unfinished_copy(self, http, hub, workspace):
        call, server = http
        server.hub().uploader.seal_wait_s = 0.3
        run_folder(workspace)
        connect(call, hub)
        hub.seal_busy = 10_000  # complete never commits: the hub keeps it staged
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"], timeout=30)
        assert job["status"] == "paused"
        stored = hub.sessions[job["session_id"]]
        status, answer = call(f"{API}/local/jobs/{job['id']}/cancel", {})
        assert status == 200
        deadline = time.time() + 5
        while time.time() < deadline:
            job = call(f"{API}/local/jobs/{job['id']}")[1]["job"]
            if (job["remote_abort"] or {}).get("status") == "aborted":
                break
            time.sleep(0.02)
        assert job["status"] == "cancelled" and job["remote_abort"]["status"] == "aborted"
        assert stored["status"] == "aborted"

    def test_ma5_abort_waits_for_the_bound_account(self, http, hub, workspace):
        call, server = http
        server.hub().uploader.seal_wait_s = 0.3
        run_folder(workspace)
        connect(call, hub, "alice")
        hub.seal_busy = 10_000
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"], timeout=30)
        stored = hub.sessions[job["session_id"]]
        hub.redirect = True  # the hub cannot be reached when alice cancels
        call(f"{API}/local/jobs/{job['id']}/cancel", {})
        deadline = time.time() + 5
        while time.time() < deadline:
            local = call(f"{API}/local/jobs/{job['id']}")[1]["job"]
            if (local["remote_abort"] or {}).get("message"):
                break
            time.sleep(0.02)
        assert local["remote_abort"]["status"] == "pending", local
        hub.redirect = False
        connect(call, hub, "bob")
        time.sleep(0.3)
        assert stored["status"] == "staging"  # never discarded with bob's credential
        assert not any(
            r["path"].endswith("/abort") and "bob" in (r["auth"] or "") for r in hub.requests
        )
        connect(call, hub, "alice")
        deadline = time.time() + 5
        while stored["status"] != "aborted" and time.time() < deadline:
            time.sleep(0.02)
        assert stored["status"] == "aborted"
        assert call(f"{API}/local/jobs/{job['id']}")[1]["job"]["remote_abort"]["status"] == (
            "aborted"
        )

    def test_committed_data_is_never_aborted(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed"
        status, answer = call(f"{API}/local/jobs/{job['id']}/cancel", {})
        assert answer["job"]["status"] == "completed"
        time.sleep(0.2)
        assert not any(r["path"].endswith("/abort") for r in hub.requests)
        assert next(iter(hub.sessions.values()))["status"] == "committed"

    def test_unfinished_uploads_listed_and_discarded_through_the_hub_routes(
        self, http, hub, workspace
    ):
        call, server = http
        server.hub().uploader.seal_wait_s = 0.3
        run_folder(workspace)
        connect(call, hub)
        hub.seal_busy = 10_000
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"], timeout=30)
        status, listed = call(f"{API}/sessions?limit=10")
        assert status == 200 and [s["id"] for s in listed["items"]] == [job["session_id"]]
        status, answer = call(f"{API}/sessions/{job['session_id']}/abort", {})
        assert status == 201 or status == 200, answer
        deadline = time.time() + 5
        while time.time() < deadline:
            local = call(f"{API}/local/jobs/{job['id']}")[1]["job"]
            if local["status"] == "cancelled":
                break
            time.sleep(0.02)
        assert local["status"] == "cancelled"
        assert call(f"{API}/sessions?limit=10")[1]["items"] == []
        assert call(f"{API}/sessions/x/files", {})[0] == 404  # no raw chunk proxy


def test_minor5_chunks_follow_the_hubs_limit(http, hub, workspace):
    call, _ = http
    session_with(workspace, {"big.bin": b"x" * 5000})
    hub.limits = {"max_chunk_bytes": 1024}
    connect(call, hub)
    body, out = preview(call, workspace)
    job = wait_job(call, start(call, body, out)["id"])
    assert job["status"] == "completed", job
    assert max(hub.chunk_sizes) <= 1024


@pytest.mark.parametrize(
    "name", ["a:b.txt", unicodedata.normalize("NFD", "café.csv"), "Trials.CSV"]
)
def test_minor6_names_the_hub_refuses_are_found_at_preview(http, hub, workspace, name):
    call, _ = http
    files = {name: b"1"}
    if name == "Trials.CSV":
        files["trials.csv"] = b"2"
    try:
        session_with(workspace, files)
    except OSError:
        pytest.skip("this file system cannot hold that name")
    connect(call, hub)
    body = {
        "project_id": project_id(workspace),
        "root_id": root_id(call, workspace),
        "run_id": RUN,
        "experiment_id": "e1",
        "version_id": "v1",
    }
    status, out = call(f"{API}/local/upload-preview", body)
    assert status == 409 and out["error"]["code"] == "unsupported_path", out


def test_minor7_a_moved_session_keeps_its_identity(tmp_path):
    card = {
        "experiment": {"name": "demo"},
        "subject": {"id": "01"},
        "session": 1,
        "run": 1,
        "task": "demo",
        "created": "2026-10-09T10:00:00Z",
    }
    first, second = tmp_path / "a" / "run", tmp_path / "moved" / "run"
    for folder in (first, second):
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(json.dumps(card), encoding="utf-8")
    assert sync.client_session_id("rig1", first) == sync.client_session_id("rig1", second)
    assert sync.client_session_id("rig1", first) != sync.client_session_id("rig2", first)
    other = tmp_path / "b" / "run"
    other.mkdir(parents=True)
    (other / "session.json").write_text(json.dumps({**card, "run": 2}), encoding="utf-8")
    assert sync.client_session_id("rig1", other) != sync.client_session_id("rig1", first)


def test_minor10_an_install_command_starts_no_uploads(workspace, hub):
    from alhazen.cli.dashboard import DashboardServer

    server = DashboardServer(workspace)
    adapter = HubAdapter(workspace, server.data, background=False)
    try:
        assert not adapter.uploader._thread.is_alive()
    finally:
        adapter.close()
        server.server_close()


def test_minor13_another_hub_revokes_the_old_bearer_at_the_old_hub(http, hub):
    call, _ = http
    connect(call, hub)
    assert hub.tokens
    other = FakeHub()
    try:
        status, out = call(f"{API}/local/connect", {"url": other.base, "allow_http_loopback": True})
        assert status == 200 and out["previous_revoked"] is True
        assert hub.tokens == {}
        assert not any(r["auth"] for r in other.requests)
    finally:
        other.close()


def test_registration_happens_on_the_hub_page_not_through_the_rig(http, hub):
    call, _ = http
    connect(call, hub)
    before = len(hub.requests)
    status, out = call(f"{API}/auth/register", {"username": "carol", "password": "x" * 12})
    assert status == 409 and out["error"]["code"] == "register_on_hub"
    assert hub.base in out["error"]["message"]
    assert len(hub.requests) == before  # nothing sent: the rig never forges the hub's Origin


class TestInstallRecovery:
    def setup_release(self, call, hub, tmp_path):
        from tests.hub.rig_support import release_zip

        connect(call, hub)
        data, manifest = release_zip(tmp_path)
        exp, ver, sha = hub.add_release(data, manifest)
        return {
            "experiment_id": exp,
            "version_id": ver,
            "sha256": sha,
            "python": __import__("sys").executable,
            "trust_code": True,
        }

    def set_status(self, workspace, status, **extra):
        registry = workspace.directory / "hub" / "installs.json"
        entries = json.loads(registry.read_text(encoding="utf-8"))
        for entry in entries:
            entry.update(status=status, **extra)
        registry.write_text(json.dumps(entries), encoding="utf-8")
        return entries[0]

    def test_an_interruption_before_unpacking_is_cleared_and_reinstalled(
        self, http, hub, workspace, tmp_path
    ):
        call, _ = http
        body = self.setup_release(call, hub, tmp_path)
        assert call(f"{API}/local/install", body)[0] == 201
        record = self.set_status(workspace, "installing")
        # The folder never got there: as if the dashboard died before unpacking.
        import shutil
        import stat as st

        folder = Path(record["path"])
        for p in folder.rglob("*"):
            if p.is_file():
                p.chmod(st.S_IMODE(p.stat().st_mode) | st.S_IWUSR)
        shutil.rmtree(folder)
        status, out = call(f"{API}/local/install", body)
        assert status == 409 and out["error"]["code"] == "install_interrupted"
        status, out = call(f"{API}/local/install-recover", {"sha256": body["sha256"]})
        assert status == 200 and out["recovery"]["destination_state"] == "absent"
        assert out["install"] is None
        status, out = call(f"{API}/local/install", body)
        assert status == 201 and out["install"]["status"] == "registered"

    def test_a_committed_tree_is_completed_only_when_exact(self, http, hub, workspace, tmp_path):
        call, _ = http
        body = self.setup_release(call, hub, tmp_path)
        assert call(f"{API}/local/install", body)[0] == 201
        record = self.set_status(workspace, "installing")
        folder = Path(record["path"])
        (folder / "notes.txt").write_text("someone else's", encoding="utf-8")
        status, out = call(f"{API}/local/install-recover", {"sha256": body["sha256"]})
        assert status == 200 and out["install"]["error"]["code"] == "install_kept"
        assert (folder / "notes.txt").is_file() and (folder / "run.py").is_file()
        from tests.unit import test_workspace as base

        from alhazen.cli.workspace import Launch

        (folder / "configs").mkdir(exist_ok=True)
        (folder / "configs" / "rig-sim.yaml").write_bytes(base.RIG.read_bytes())
        with pytest.raises(ValueError, match="did not finish"):
            workspace.start(
                Launch(
                    project=record["project_id"],
                    mode="movie",
                    rig="configs/rig-sim.yaml",
                    extra_args="--task demo",
                )
            )
        assert workspace.active is None
        (folder / "configs" / "rig-sim.yaml").unlink()
        (folder / "configs").rmdir()
        (folder / "notes.txt").unlink()
        status, out = call(f"{API}/local/install-recover", {"sha256": body["sha256"]})
        assert status == 200 and out["install"]["status"] in ("installed", "registered")
        assert out["install"]["durable"] is False
