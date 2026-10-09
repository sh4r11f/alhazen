"""The dashboard's hub adapter over real loopback HTTP (cli/workspace_hub.py,
the /hub and /api/hub/v1 routes in dashboard.py) against a stand-in hub:
token/origin rules, sign-in without exposing the bearer, the exact proxy
allowlist, verified trusted installs, source packaging, and bound, resumable
session uploads. Synthetic users, releases and sessions only."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import types
from http.client import HTTPConnection
from pathlib import Path

import pytest
from tests.hub.rig_support import FakeHub, release_zip
from tests.unit import test_workspace as base
from tests.unit.test_workspace_upload import RUN, session

from alhazen.cli import workspace as workspace_module
from alhazen.cli.dashboard import DashboardServer
from alhazen.cli.workspace import Workspace
from alhazen.cli.workspace_hub import HubAdapter

GOOD_PROBE = {"alhazen_version": "2.13.0", "python_version": "3.11.4 (main)", "shared_rigs": []}


@pytest.fixture
def probes(monkeypatch):
    """The interpreter probe, recorded: it imports code from the folder, so
    the tests check it never runs before trust."""
    calls: list[tuple[str, str]] = []
    answer = dict(GOOD_PROBE)

    def probe(python, path):
        calls.append((python, path))
        return dict(answer)

    monkeypatch.setattr(workspace_module, "probe_interpreter", probe)
    return calls, answer


@pytest.fixture
def workspace(tmp_path, probes):
    root = tmp_path / "experiment"
    (root / "configs").mkdir(parents=True)
    (root / "configs/rig-sim.yaml").write_bytes(base.RIG.read_bytes())
    (root / "configs/task.yaml").write_text("speed: 3\n")
    (root / "run.py").write_text("print('run')\n")
    (root / "pyproject.toml").write_text('[project]\nname = "demo-experiment"\nversion = "0.1.0"\n')
    space = Workspace(tmp_path / "state")
    space.add(str(root), sys.executable)
    yield space
    space.close()


@pytest.fixture
def hub():
    server = FakeHub()
    yield server
    server.close()


@pytest.fixture
def http(workspace):
    server = DashboardServer(workspace)
    server._hub = HubAdapter(
        workspace, server.data, uploader_wait_s=0.02, backoff_s=(0.01, 0.01, 0.01)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, *, method=None, headers=None, raw=False, token=True):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        merged = {"X-Alhazen-Token": server.token} if token else {}
        merged.update(headers or {})
        data = None
        if body is not None:
            merged.setdefault("Content-Type", "application/json")
            data = json.dumps(body)
        conn.request(method or ("POST" if body is not None else "GET"), path, data, merged)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        if raw:
            return response.status, dict(response.getheaders()), payload
        return response.status, json.loads(payload) if payload else None

    yield call, server
    server.shutdown()
    server.server_close()
    server.close_hub()
    thread.join(timeout=5)


API = "/api/hub/v1"


def connect(call, hub, user="alice"):
    status, out = call(f"{API}/local/connect", {"url": hub.base, "allow_http_loopback": True})
    assert status == 200, out
    status, out = call(f"{API}/auth/login", {"username": user, "password": hub.users[user]})
    assert status == 200, out
    return out


def wait_job(call, job_id, statuses=("completed", "failed", "paused", "cancelled"), timeout=20):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        status, out = call(f"{API}/local/jobs/{job_id}")
        assert status == 200, out
        job = out["job"]
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {job}")


def project_id(workspace):
    return workspace.projects[0]["id"]


def run_folder(workspace) -> Path:
    return session(Path(workspace.projects[0]["path"]))


def root_id(call, workspace):
    status, out = call(f"/api/data/roots?project={project_id(workspace)}")
    assert status == 200, out
    return next(r["id"] for r in out["roots"] if r["kind"] == "real")


# -- routes and their protection ---------------------------------------------------


class TestRoutes:
    def test_workspace_root_is_unchanged_and_hub_bootstrap_has_no_secret(self, http):
        call, server = http
        status, headers, body = call("/", token=False, raw=True)
        assert status == 200 and b"<html" in body.lower()
        status, out = call("/hub/bootstrap.json", token=False)
        assert status == 200 and out["role"] == "rig"
        assert out["auth"]["header"] == "X-Alhazen-Token"
        assert server.token not in json.dumps(out)

    def test_hub_page_is_a_fixed_table(self, http):
        import re

        from alhazen.cli.dashboard import HUB_ASSETS, HUB_ASSETS_DIR

        call, _ = http
        status, headers, body = call("/hub", token=False, raw=True)
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        # Every file the page ships, and every asset the page refers to, is
        # in the table; nothing else is served.
        served = {name for name, _ in HUB_ASSETS.values()}
        shipped = {
            p.relative_to(HUB_ASSETS_DIR).as_posix()
            for p in HUB_ASSETS_DIR.rglob("*")
            if p.is_file()
        }
        assert shipped <= served
        referenced = set(re.findall(r'(?:href|src)="(/hub/assets/[^"]+)"', body.decode()))
        assert referenced <= set(HUB_ASSETS)
        for path in ("/hub/assets/../dashboard.py", "/hub/assets/secret.js", "/hub/x"):
            assert call(path, token=False, raw=True)[0] == 404

    def test_api_needs_the_token_host_and_origin(self, http):
        call, server = http
        assert call(f"{API}/local/status", token=False)[0] == 403
        status, _ = call(f"{API}/local/status", headers={"Origin": "https://evil.example"})
        assert status == 403
        status, _ = call(
            f"{API}/auth/login",
            {"username": "a", "password": "b"},
            headers={"Origin": "https://evil.example"},
        )
        assert status == 403
        assert call(f"{API}/local/status")[0] == 200

    def test_only_allowlisted_routes(self, http, hub):
        call, _ = http
        connect(call, hub)
        before = len(hub.requests)
        for path in (f"{API}/sessions/init", f"{API}/admin/users", f"{API}/../../etc/passwd"):
            status, out = call(path, {})
            assert status == 404, (path, out)
        status, out = call(f"{API}/catalog?query=x&callback=evil")
        assert status == 400 and out["error"]["code"] == "invalid_request"
        assert len(hub.requests) == before

    def test_patch_outside_the_hub_is_unchanged(self, http):
        call, _ = http
        assert call("/api/state", {}, method="PATCH", raw=True)[0] == 501

    def test_offline_guide(self, http, monkeypatch):
        call, _ = http
        monkeypatch.setitem(sys.modules, "alhazen.hub.documentation", None)
        status, out = call(f"{API}/guide")
        assert status == 503 and out["error"]["code"] == "not_available"
        fake = types.SimpleNamespace(global_guide=lambda: {"modes": [{"name": "run"}]})
        monkeypatch.setitem(sys.modules, "alhazen.hub.documentation", fake)
        # The frozen contract shape: {guide: global_guide()}, as central GET /guide.
        assert call(f"{API}/guide") == (200, {"guide": {"modes": [{"name": "run"}]}})


# -- connection and sign-in ---------------------------------------------------------


class TestSignIn:
    def test_connect_validates_the_address(self, http, hub):
        call, _ = http
        status, out = call(f"{API}/local/connect", {"url": hub.base})
        assert status == 400 and out["error"]["code"] == "invalid_url"
        status, out = call(f"{API}/local/connect", {"url": "https://u:p@hub.example.org"})
        assert status == 400
        status, out = call(
            f"{API}/local/connect", {"url": "http://127.0.0.1:9", "allow_http_loopback": True}
        )
        assert status == 502 and out["error"]["code"] == "hub_unreachable"
        assert call(f"{API}/local/status")[1]["state"] == "not_configured"

    def test_bearer_never_reaches_the_page(self, http, hub, workspace):
        call, server = http
        assert call(f"{API}/auth/me")[1]["error"]["code"] == "not_connected"
        out = connect(call, hub)
        assert out["user"]["username"] == "alice" and out["csrf_token"] is None
        token = next(iter(hub.tokens))
        pages = [call(f"{API}/local/status")[1], call(f"{API}/config")[1], out]
        assert all(token not in json.dumps(page) for page in pages)
        assert pages[0]["state"] == "signed_in"
        assert pages[1]["role"] == "rig" and pages[1]["server"]["role"] == "server"
        status, out = call(f"{API}/catalog?query=gabor&limit=5")
        assert status == 200 and out == {"items": [], "next_offset": None}
        sent = hub.requests[-1]
        assert sent["auth"] == f"Bearer {token}" and sent["cookie"] is None
        assert "X-Alhazen-Token" not in sent["headers"]
        assert sent["query"] == {"query": "gabor", "limit": "5"}
        stored = (workspace.directory / "hub" / "credential.json").read_text()
        assert token in stored
        if sys.platform != "win32":
            mode = stat.S_IMODE(os.stat(workspace.directory / "hub" / "credential.json").st_mode)
            assert mode == 0o600

    def test_wrong_password_passes_the_hubs_refusal(self, http, hub):
        call, _ = http
        call(f"{API}/local/connect", {"url": hub.base, "allow_http_loopback": True})
        status, out = call(f"{API}/auth/login", {"username": "alice", "password": "nope"})
        assert status == 401 and out["error"]["code"] == "invalid_credentials"

    def test_a_rejected_bearer_is_forgotten(self, http, hub):
        call, _ = http
        connect(call, hub)
        hub.tokens.clear()
        status, out = call(f"{API}/catalog")
        assert status == 401 and out["error"]["code"] == "unauthenticated"
        assert call(f"{API}/local/status")[1]["state"] == "signed_out"

    def test_logout_revokes_and_forgets(self, http, hub):
        call, _ = http
        connect(call, hub)
        status, out = call(f"{API}/auth/logout", {})
        assert status == 200 and out["revoked"] is True
        assert hub.tokens == {}
        assert call(f"{API}/local/status")[1]["state"] == "signed_out"

    def test_logout_when_the_hub_is_down_says_so(self, http, hub):
        call, _ = http
        connect(call, hub)
        hub.redirect = True  # every answer is now a refusal the client will not follow
        status, out = call(f"{API}/auth/logout", {})
        assert status == 200 and out["revoked"] is False
        assert call(f"{API}/local/status")[1]["state"] == "signed_out"

    def test_redirects_are_not_followed(self, http, hub):
        call, _ = http
        connect(call, hub)
        hub.redirect = True
        status, out = call(f"{API}/catalog")
        assert status == 502 and out["error"]["code"] == "hub_redirect"

    def test_another_hub_forgets_the_sign_in(self, http, hub):
        call, _ = http
        connect(call, hub)
        other = FakeHub()
        try:
            status, out = call(
                f"{API}/local/connect", {"url": other.base, "allow_http_loopback": True}
            )
            assert status == 200 and out["state"] == "signed_out"
        finally:
            other.close()


def test_downloads_are_sandboxed_attachments(http, hub):
    call, _ = http
    connect(call, hub)
    status, headers, body = call(f"{API}/data/sessions/s1/export?format=csv", raw=True)
    assert status == 200 and body == b"trial,rt\n1,0.2\n"
    assert headers["Content-Type"] == "application/octet-stream"  # the hub said text/html
    assert headers["Content-Disposition"] == "attachment; filename*=UTF-8''trials.csv"
    assert headers["Content-Security-Policy"].startswith("sandbox")
    assert headers["X-Content-Type-Options"] == "nosniff"


# -- installs ---------------------------------------------------------------------------


class TestInstall:
    def release(self, hub, tmp_path, **kw):
        data, manifest = release_zip(tmp_path, **kw)
        exp, ver, sha = hub.add_release(data, manifest)
        return {"experiment_id": exp, "version_id": ver, "sha256": sha}

    def downloads(self, hub):
        return [r for r in hub.requests if r["path"].endswith("/download")]

    def test_trust_is_required_and_nothing_runs_before_it(self, http, hub, tmp_path, probes):
        call, _ = http
        connect(call, hub)
        probes[0].clear()
        body = {**self.release(hub, tmp_path), "python": sys.executable}
        status, out = call(f"{API}/local/install", {**body, "trust_code": False})
        assert status == 400 and out["error"]["code"] == "trust_required"
        assert self.downloads(hub) == [] and probes[0] == []

    def test_the_reviewed_digest_must_be_the_listed_one(
        self, http, hub, tmp_path, probes, workspace
    ):
        call, _ = http
        connect(call, hub)
        probes[0].clear()
        body = {**self.release(hub, tmp_path), "python": sys.executable, "trust_code": True}
        status, out = call(f"{API}/local/install", {**body, "sha256": "a" * 64})
        assert status == 409 and out["error"]["code"] == "hash_mismatch"
        assert self.downloads(hub) == [] and probes[0] == []
        assert not (workspace.directory / "hub" / "experiments").exists()

    def test_install_registers_a_read_only_versioned_release(
        self, http, hub, tmp_path, probes, workspace
    ):
        call, _ = http
        connect(call, hub)
        probes[0].clear()
        body = {**self.release(hub, tmp_path), "python": sys.executable, "trust_code": True}
        status, out = call(f"{API}/local/install", body)
        assert status == 201, out
        install = out["install"]
        assert install["status"] == "registered" and install["sha256"] == body["sha256"]
        assert install["workspace_url"] == f"/?project={install['project_id']}&view=run"
        folder = (
            workspace.directory
            / "hub"
            / "experiments"
            / "demo-task"
            / f"1.0.0-{body['sha256'][:12]}"
        )
        assert (folder / "run.py").is_file()
        assert not os.access(folder / "run.py", os.W_OK) or sys.platform == "win32"
        assert probes[0] and all(path == str(folder.resolve()) for _, path in probes[0])
        assert any(p["id"] == install["project_id"] for p in workspace.projects)
        # Idempotent: the same digest again downloads nothing.
        count = len(self.downloads(hub))
        status, again = call(f"{API}/local/install", body)
        assert status == 201 and again["install"]["project_id"] == install["project_id"]
        assert len(self.downloads(hub)) == count
        status, out = call(f"{API}/local/projects")
        assert any(i["install"] and i["install"]["sha256"] == body["sha256"] for i in out["items"])

    def test_an_incompatible_interpreter_leaves_it_installed_not_registered(
        self, http, hub, tmp_path, probes, workspace
    ):
        call, _ = http
        connect(call, hub)
        probes[1]["alhazen_version"] = "2.12.0"
        body = {**self.release(hub, tmp_path), "python": sys.executable, "trust_code": True}
        status, out = call(f"{API}/local/install", body)
        assert status == 400 and out["error"]["code"] == "incompatible"
        assert "2.13.0" in out["error"]["message"]
        installed = call(f"{API}/local/status")[1]["installed"]
        assert (
            installed[0]["status"] == "installed"
            and installed[0]["error"]["code"] == "incompatible"
        )
        assert len(workspace.projects) == 1
        probes[1]["alhazen_version"] = "2.13.0"
        count = len(self.downloads(hub))
        status, out = call(f"{API}/local/install", body)
        assert status == 201 and out["install"]["status"] == "registered"
        assert len(self.downloads(hub)) == count

    def test_unsupported_platform_is_refused_before_unpacking(self, http, hub, tmp_path, workspace):
        call, _ = http
        connect(call, hub)
        other = next(p for p in ("linux", "darwin", "win32") if p != sys.platform)
        body = {
            **self.release(hub, tmp_path, platforms=[other]),
            "python": sys.executable,
            "trust_code": True,
        }
        status, out = call(f"{API}/local/install", body)
        assert status == 409 and out["error"]["code"] == "unsupported_platform"
        assert not (workspace.directory / "hub" / "experiments" / "demo-task").exists()

    def test_install_needs_an_explicit_interpreter_and_no_active_run(
        self, http, hub, tmp_path, workspace
    ):
        call, _ = http
        connect(call, hub)
        body = {**self.release(hub, tmp_path), "trust_code": True}
        assert call(f"{API}/local/install", {**body, "python": "python3"})[0] == 400
        workspace.active = "some-run"
        try:
            status, out = call(f"{API}/local/install", {**body, "python": sys.executable})
            assert status == 409 and out["error"]["code"] == "run_active"
        finally:
            workspace.active = None
        assert self.downloads(hub) == []


# -- source packages ------------------------------------------------------------------


def test_package_preview_and_upload_never_publish(http, hub, workspace):
    call, _ = http
    connect(call, hub)
    root = Path(workspace.projects[0]["path"])
    (root / "data").mkdir()
    (root / "data" / "participants.tsv").write_text("participant_id\n")
    status, out = call(f"{API}/local/package-preview", {"project_id": project_id(workspace)})
    assert status == 200, out
    paths = [f["path"] for f in out["files"]]
    assert "run.py" in paths and "data/participants.tsv" not in paths
    assert "configs/rig-sim.yaml" not in paths
    assert any(e["path"] == "data/participants.tsv" for e in out["excluded"])
    metadata = {**out["metadata"], "license": "MIT"}
    body = {
        "project_id": project_id(workspace),
        "experiment_id": "e1",
        "metadata": metadata,
        "files": paths,
    }
    status, out = call(f"{API}/local/package-upload", body)
    assert status == 400 and out["error"]["code"] == "confirmation_required"
    status, out = call(
        f"{API}/local/package-upload",
        {**body, "files": [*paths, "data/participants.tsv"], "confirmed": True},
    )
    assert status == 400
    status, out = call(f"{API}/local/package-upload", {**body, "confirmed": True})
    assert status == 201, out
    assert out["version"]["manifest"]["name"] == "demo-experiment"
    assert not any("publish" in r["path"] for r in hub.requests)


# -- session uploads ---------------------------------------------------------------------


def preview(call, workspace, **extra):
    body = {
        "project_id": project_id(workspace),
        "root_id": root_id(call, workspace),
        "run_id": RUN,
        "experiment_id": "e1",
        "version_id": "v1",
        **extra,
    }
    status, out = call(f"{API}/local/upload-preview", body)
    assert status == 200, out
    return body, out


def start(call, body, out):
    status, answer = call(
        f"{API}/local/upload", {**body, "preview_id": out["preview_id"], "consent": True}
    )
    assert status in (200, 202), answer
    return answer["job"]


class TestUpload:
    def test_preview_upload_and_receipt(self, http, hub, workspace):
        call, _ = http
        folder = run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        assert {f["path"] for f in out["files"]} == {
            "session.json",
            "sub-01_ses-001_run-01_trials.csv",
            "figures/summary.png",
            "manifest.yaml",
        }
        assert out["recipient"]["user"]["username"] == "alice"
        assert out["metadata"]["subject_code"] == "01" and out["metadata"]["mode"] == "run"
        assert out["privacy"]["warning"]
        status, refused = call(f"{API}/local/upload", {**body, "preview_id": out["preview_id"]})
        assert status == 400 and refused["error"]["code"] == "consent_required"
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed", job
        assert job["receipt"]["status"] == "committed"
        stored = next(iter(hub.sessions.values()))
        assert stored["owner"] == "alice"
        for f in stored["files"]:
            assert bytes(stored["data"][f["path"]]) == (folder / f["path"]).read_bytes()
        # The local files are untouched and nothing was added to the session.
        assert sorted(p.name for p in folder.rglob("*") if p.is_file()) == sorted(
            ["session.json", "sub-01_ses-001_run-01_trials.csv", "summary.png", "manifest.yaml"]
        )
        # The same session again is the same job, not a second upload.
        body2, out2 = preview(call, workspace)
        again = start(call, body2, out2)
        assert again["id"] == job["id"] and again["status"] == "completed"
        assert len(hub.sessions) == 1
        status, listed = call(f"{API}/local/sessions?project_id={project_id(workspace)}")
        assert listed["items"][0]["job"]["status"] == "completed"

    def test_a_changed_session_needs_a_new_preview(self, http, hub, workspace):
        call, _ = http
        folder = run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        (folder / "session.json").write_text('{"changed": true}')
        status, answer = call(
            f"{API}/local/upload", {**body, "preview_id": out["preview_id"], "consent": True}
        )
        assert status == 409 and answer["error"]["code"] == "preview_stale"
        status, answer = call(
            f"{API}/local/upload", {**body, "preview_id": "made-up", "consent": True}
        )
        assert status == 409 and answer["error"]["code"] == "preview_stale"
        assert hub.sessions == {}

    def test_another_account_cannot_use_a_preview(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub, "alice")
        body, out = preview(call, workspace)
        connect(call, hub, "bob")
        status, answer = call(
            f"{API}/local/upload", {**body, "preview_id": out["preview_id"], "consent": True}
        )
        assert status == 409 and answer["error"]["code"] == "auth_context_changed"
        assert hub.sessions == {}

    def test_waits_while_a_session_runs(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        workspace.active = "a-run"
        try:
            job = start(call, body, out)
            job = wait_job(call, job["id"], statuses=("waiting",))
            time.sleep(0.2)
            assert wait_job(call, job["id"], statuses=("waiting",))["status"] == "waiting"
            assert not any(r["path"].startswith("/sessions") for r in hub.requests)
        finally:
            workspace.active = None
        assert wait_job(call, job["id"])["status"] == "completed"

    def test_preview_refused_while_a_session_runs(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        workspace.active = "a-run"
        try:
            status, out = call(
                f"{API}/local/upload-preview",
                {"project_id": project_id(workspace), "root_id": "x", "run_id": RUN},
            )
            assert status == 409 and out["error"]["code"] == "run_active"
        finally:
            workspace.active = None

    def test_transient_failures_retry_with_the_same_identity(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.fail_puts = 2
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed", job
        assert len(hub.sessions) == 1

    def test_a_job_never_runs_under_another_account(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub, "alice")
        body, out = preview(call, workspace)
        workspace.active = "a-run"
        try:
            job = start(call, body, out)
            wait_job(call, job["id"], statuses=("waiting",))
            connect(call, hub, "bob")
        finally:
            workspace.active = None
        # Bob cannot see alice's job ...
        assert call(f"{API}/local/jobs/{job['id']}")[0] == 404
        assert call(f"{API}/local/jobs")[1]["items"] == []
        time.sleep(0.3)
        # ... and nothing reached the hub under bob (or anyone).
        assert hub.sessions == {}
        connect(call, hub, "alice")
        job = wait_job(call, job["id"])
        assert job["status"] == "completed", job
        assert next(iter(hub.sessions.values()))["owner"] == "alice"

    def test_logout_pauses_and_sign_in_resumes(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        workspace.active = "a-run"
        try:
            job = start(call, body, out)
            wait_job(call, job["id"], statuses=("waiting",))
            status, answer = call(f"{API}/auth/logout", {})
            assert answer["paused_jobs"] == 1
        finally:
            workspace.active = None
        time.sleep(0.2)
        assert hub.sessions == {}
        connect(call, hub)
        assert wait_job(call, job["id"])["status"] == "completed"

    def test_a_file_changed_before_hashing_pauses(self, http, hub, workspace):
        call, _ = http
        folder = run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        workspace.active = "a-run"
        try:
            job = start(call, body, out)
            wait_job(call, job["id"], statuses=("waiting",))
            (folder / "session.json").write_text('{"edited": 1}')
        finally:
            workspace.active = None
        job = wait_job(call, job["id"])
        assert job["status"] == "paused" and job["error"]["code"] == "local_changed"
        assert hub.sessions == {}
        status, answer = call(f"{API}/local/jobs/{job['id']}/resume", {})
        assert status == 409 and answer["error"]["code"] == "preview_required"

    def test_cancel_keeps_local_files(self, http, hub, workspace):
        call, _ = http
        folder = run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        workspace.active = "a-run"
        try:
            job = start(call, body, out)
            wait_job(call, job["id"], statuses=("waiting",))
            status, answer = call(f"{API}/local/jobs/{job['id']}/cancel", {})
            assert status == 200
        finally:
            workspace.active = None
        assert wait_job(call, job["id"])["status"] == "cancelled"
        assert (folder / "session.json").is_file()
        status, answer = call(f"{API}/local/jobs/{job['id']}/resume", {})
        assert status == 409 and answer["error"]["code"] == "preview_required"


def test_interrupted_jobs_resume_after_a_restart(tmp_path, workspace, hub):
    run_folder(workspace)
    server = DashboardServer(workspace)
    adapter = HubAdapter(workspace, server.data, uploader_wait_s=0.02, backoff_s=(0.01,))
    try:
        adapter.handle("POST", "/local/connect", {}, {"url": hub.base, "allow_http_loopback": True})
        adapter.handle(
            "POST", "/auth/login", {}, {"username": "alice", "password": hub.users["alice"]}
        )
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
        workspace.active = "a-run"
        _, answer = adapter.handle(
            "POST", "/local/upload", {}, {**body, "preview_id": out["preview_id"], "consent": True}
        )
        job_id = answer["job"]["id"]
        deadline = time.time() + 5
        while adapter.outbox.load(job_id)["status"] != "waiting" and time.time() < deadline:
            time.sleep(0.02)
    finally:
        adapter.close()
    job = adapter.outbox.load(job_id)
    assert job["status"] == "paused" and job["error"]["code"] == "interrupted"
    workspace.active = None
    again = HubAdapter(workspace, server.data, uploader_wait_s=0.02, backoff_s=(0.01,))
    try:
        deadline = time.time() + 10
        while again.outbox.load(job_id)["status"] != "completed" and time.time() < deadline:
            time.sleep(0.02)
        assert again.outbox.load(job_id)["status"] == "completed"
    finally:
        again.close()
        server.server_close()


def test_a_damaged_job_record_is_reported_not_hidden(http, workspace):
    call, _ = http
    outbox = workspace.directory / "hub" / "outbox"
    (outbox / "abc123.json").write_text("{not json")
    status, out = call(f"{API}/local/status")
    assert status == 200
    assert any("abc123.json" in p for p in out["outbox_problems"])
    assert (outbox / "abc123.json").read_text() == "{not json"


def test_reindex_is_an_allowlisted_bearer_write(http, hub, workspace):
    call, _ = http
    run_folder(workspace)
    connect(call, hub)
    body, out = preview(call, workspace)
    job = wait_job(call, start(call, body, out)["id"])
    session_id = job["session_id"]
    status, answer = call(f"{API}/data/sessions/{session_id}/reindex", {})
    assert status == 202 and answer["index"]["status"] == "pending"
    sent = hub.requests[-1]
    assert sent["method"] == "POST" and sent["auth"].startswith("Bearer ")
    assert sent["cookie"] is None and "X-Alhazen-Token" not in sent["headers"]
    # Not a GET, and not without a sign-in.
    assert call(f"{API}/data/sessions/{session_id}/reindex")[0] == 404
    call(f"{API}/auth/logout", {})
    status, answer = call(f"{API}/data/sessions/{session_id}/reindex", {})
    assert status == 401 and answer["error"]["code"] == "unauthenticated"


class TestReceipts:
    @pytest.mark.parametrize(
        "override",
        [
            {"manifest_sha256": "0" * 64},
            {"total_bytes": 1},
            {"file_count": 99},
            {"client_session_id": "rig-someone-else"},
            {"version_id": "v-other"},
        ],
    )
    def test_a_receipt_for_anything_else_is_not_completion(self, http, hub, workspace, override):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.receipt_override = override
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "failed", job
        assert job["error"]["code"] == "receipt_mismatch"
        assert job["receipt"] is None
        field = next(iter(override))
        assert field in job["error"]["message"]

    def test_the_receipt_matches_the_shared_digest(self, http, hub, workspace):
        from alhazen.hub import protocol

        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed", job
        stored = next(iter(hub.sessions.values()))
        assert set(stored["metadata"]) == set(protocol.METADATA_KEYS)
        assert job["receipt"]["manifest_sha256"] == protocol.manifest_sha256(
            "e1", "v1", stored["files"], out["metadata"]
        )

    def test_sealing_in_progress_is_retried(self, http, hub, workspace):
        call, _ = http
        run_folder(workspace)
        connect(call, hub)
        hub.seal_busy = 2
        body, out = preview(call, workspace)
        job = wait_job(call, start(call, body, out)["id"])
        assert job["status"] == "completed", job
        assert hub.seal_busy == 0
