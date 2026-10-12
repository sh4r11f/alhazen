"""Automatic setup over the dashboard's hub routes: ``setup: "auto"`` on an
install builds the release's Python environment in the background, registers
the release with it and reports each state to the page; a failure, a second
request and a closed app each leave a record that says what happened.

Real loopback HTTP against the stand-in hub, as in test_rig_adapter.py; uv is
stood in for by tests/hub/setup_support.py. Synthetic users and releases."""

# The fixtures are test_rig_adapter's, imported below; a test that takes one
# as a parameter is not redefining it.
# ruff: noqa: F811

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from tests.hub.rig_support import release_zip
from tests.hub.setup_support import fake_uv
from tests.hub.test_rig_adapter import (  # noqa: F401 - fixtures
    API,
    connect,
    http,
    hub,
    probes,
    workspace,
)

from alhazen.cli import workspace_hub
from alhazen.cli.workspace_hub import HubAdapter
from alhazen.hub.environments import SetupError, Tools, interpreter_of

LOCK = 'version = 1\nrequires-python = ">=3.10"\n\n[[package]]\nname = "numpy"\nversion = "2.2.0"\n'
GIT_LOCK = LOCK + (
    '\n[[package]]\nname = "alhazen-vision"\nversion = "2.15.0"\n'
    'source = { git = "https://github.com/sh4r11f/alhazen?tag=v2.15.0#0c1d899" }\n'
)


@pytest.fixture
def uv(tmp_path, monkeypatch):
    """This computer "has" uv and git, and uv is the stand-in. Returns a
    function that picks what the stand-in does next."""
    fake_uv(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_hub, "find_tools", lambda: Tools(Path("uv"), Path("git")))

    def behave(mode):
        monkeypatch.setenv("FAKE_UV", mode)

    behave("ok")
    return behave


def add_release(hub, tmp_path, *, lock=LOCK, name="demo-task"):
    """Publish a release on the stand-in hub; the body an install sends."""
    files = {".python-version": "3.11\n"}
    if lock is not None:
        files["uv.lock"] = lock
    data, manifest = release_zip(tmp_path, name=name, extra_files=files)
    exp, ver, sha = hub.add_release(data, manifest)
    return {"experiment_id": exp, "version_id": ver, "sha256": sha, "trust_code": True}


def installed(call, sha):
    status, out = call(f"{API}/local/status")
    assert status == 200, out
    return next(i for i in out["installed"] if i["sha256"] == sha)


def wait_setup(call, sha, timeout=30):
    """The install once its environment is no longer being built."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        install = installed(call, sha)
        if (install["environment"] or {}).get("state") != "building":
            return install
        time.sleep(0.02)
    raise AssertionError(f"still setting up after {timeout} s: {install}")


def downloads(hub):
    return [r for r in hub.requests if r["path"].endswith("/download")]


def test_an_install_builds_its_environment_and_registers_with_it(
    http,
    hub,
    tmp_path,
    probes,
    workspace,
    uv,
):
    call, _ = http
    connect(call, hub)
    probes[0].clear()
    body = add_release(hub, tmp_path)
    status, out = call(f"{API}/local/install", {**body, "setup": "auto"})
    # Accepted, not finished: the files are in place and the build has begun.
    assert status == 202, out
    assert out["install"]["status"] == "installed"
    assert out["install"]["environment"]["state"] == "building"
    assert out["install"]["environment"]["python_version"] == "3.11"

    install = wait_setup(call, body["sha256"])
    assert install["status"] == "registered" and install["error"] is None
    assert install["environment"]["state"] == "ready"
    assert install["environment"]["detail"] is None
    # The interpreter is the one of the environment built for this release,
    # under the workspace's hub folder, and it is the one that was probed.
    python = interpreter_of(workspace.directory / "hub" / "envs" / body["sha256"][:12])
    assert install["python"] == str(python) and python.is_file()
    assert {used for used, _ in probes[0]} == {str(python)}
    project = next(p for p in workspace.projects if p["id"] == install["project_id"])
    assert project["python"] == str(python)

    # Asking again changes nothing and downloads nothing.
    count = len(downloads(hub))
    status, again = call(f"{API}/local/install", {**body, "setup": "auto"})
    assert status == 201 and again["install"]["status"] == "registered"
    assert again["install"]["environment"]["started_at"] == install["environment"]["started_at"]
    assert len(downloads(hub)) == count
    status, again = call(f"{API}/local/setup", {"sha256": body["sha256"]})
    assert status == 200 and again["install"]["environment"]["state"] == "ready"


def test_the_two_ways_to_get_an_interpreter_exclude_each_other(http, hub, tmp_path, uv):
    call, _ = http
    connect(call, hub)
    body = add_release(hub, tmp_path)
    status, out = call(f"{API}/local/install", {**body, "setup": "auto", "python": sys.executable})
    assert status == 400 and "not both" in out["error"]["message"]
    status, out = call(f"{API}/local/install", {**body, "setup": "yes"})
    assert status == 400 and '"auto"' in out["error"]["message"]
    # Neither: the refusal names both ways.
    status, out = call(f"{API}/local/install", body)
    assert status == 400 and 'setup: "auto"' in out["error"]["message"]
    assert downloads(hub) == []


def test_a_computer_without_uv_is_told_before_anything_is_downloaded(
    http,
    hub,
    tmp_path,
    monkeypatch,
    workspace,
):
    call, _ = http
    connect(call, hub)

    def no_uv():
        raise SetupError("setup_unavailable", "This computer has no uv")

    monkeypatch.setattr(workspace_hub, "find_tools", no_uv)
    status, out = call(f"{API}/local/status")
    assert out["setup"] == {"available": False, "reason": "This computer has no uv", "git": False}
    body = add_release(hub, tmp_path)
    status, out = call(f"{API}/local/install", {**body, "setup": "auto"})
    assert status == 409 and out["error"]["code"] == "setup_unavailable"
    assert downloads(hub) == []
    assert not (workspace.directory / "hub" / "experiments").exists()


def test_the_page_is_told_that_setup_is_available(http, hub, uv):
    call, _ = http
    connect(call, hub)
    assert call(f"{API}/local/status")[1]["setup"] == {
        "available": True,
        "reason": None,
        "git": True,
    }


def test_a_release_without_a_lock_file_stays_installed_for_a_chosen_interpreter(
    http,
    hub,
    tmp_path,
    probes,
    uv,
):
    call, _ = http
    connect(call, hub)
    body = add_release(hub, tmp_path, lock=None)
    status, out = call(f"{API}/local/install", {**body, "setup": "auto"})
    assert status == 409 and out["error"]["code"] == "setup_unavailable"
    assert "uv.lock" in out["error"]["message"]
    # The files are installed and trusted; the reason is kept with them.
    install = installed(call, body["sha256"])
    assert install["status"] == "installed" and install["environment"] is None
    assert install["error"]["code"] == "setup_unavailable"
    # The operator's own interpreter still registers it, with no new download.
    count = len(downloads(hub))
    status, out = call(f"{API}/local/install", {**body, "python": sys.executable})
    assert status == 201 and out["install"]["status"] == "registered"
    assert out["install"]["error"] is None and len(downloads(hub)) == count


def test_a_git_source_without_git_is_refused_naming_the_package(
    http,
    hub,
    tmp_path,
    monkeypatch,
    uv,
):
    call, _ = http
    connect(call, hub)
    monkeypatch.setattr(workspace_hub, "find_tools", lambda: Tools(Path("uv"), None))
    body = add_release(hub, tmp_path, lock=GIT_LOCK)
    status, out = call(f"{API}/local/install", {**body, "setup": "auto"})
    assert status == 409 and out["error"]["code"] == "git_missing"
    assert "alhazen-vision" in out["error"]["message"]
    assert installed(call, body["sha256"])["error"]["code"] == "git_missing"
    # With git it is set up; the record says what came from git.
    monkeypatch.setattr(workspace_hub, "find_tools", lambda: Tools(Path("uv"), Path("git")))
    status, out = call(f"{API}/local/setup", {"sha256": body["sha256"]})
    assert status == 202, out
    install = wait_setup(call, body["sha256"])
    assert install["status"] == "registered"
    assert install["environment"]["git_packages"] == ["alhazen-vision"]


def test_a_failed_build_is_reported_and_can_be_started_again(
    http,
    hub,
    tmp_path,
    probes,
    workspace,
    uv,
):
    call, _ = http
    connect(call, hub)
    probes[0].clear()
    body = add_release(hub, tmp_path)
    uv("fail")
    assert call(f"{API}/local/install", {**body, "setup": "auto"})[0] == 202
    install = wait_setup(call, body["sha256"])
    assert install["status"] == "installed" and install["environment"]["state"] == "failed"
    assert install["error"]["code"] == "setup_failed"
    # The cause uv gave reaches the page.
    assert "connection refused" in install["error"]["message"]
    # Nothing was probed or registered with a half-built environment.
    assert probes[0] == [] and len(workspace.projects) == 1

    uv("ok")
    status, out = call(f"{API}/local/setup", {"sha256": body["sha256"]})
    assert status == 202 and out["install"]["environment"]["state"] == "building"
    assert out["install"]["error"] is None
    install = wait_setup(call, body["sha256"])
    assert install["status"] == "registered" and install["error"] is None


def test_an_interpreter_that_cannot_run_the_release_is_reported_after_the_build(
    http,
    hub,
    tmp_path,
    probes,
    uv,
):
    call, _ = http
    connect(call, hub)
    # The environment's alhazen is older than the release's floor (2.13.0).
    probes[1]["alhazen_version"] = "2.12.0"
    body = add_release(hub, tmp_path)
    assert call(f"{API}/local/install", {**body, "setup": "auto"})[0] == 202
    install = wait_setup(call, body["sha256"])
    assert install["environment"]["state"] == "ready"
    assert install["status"] == "installed" and install["error"]["code"] == "incompatible"


def test_one_setup_at_a_time_and_no_session_meanwhile(
    http,
    hub,
    tmp_path,
    uv,
    workspace,
):
    call, server = http
    connect(call, hub)
    first = add_release(hub, tmp_path, name="demo-task")
    second = add_release(hub, tmp_path, name="other-task")
    uv("hang")
    assert call(f"{API}/local/install", {**first, "setup": "auto"})[0] == 202
    # The page sees the build's latest line while it runs.
    deadline = time.time() + 30
    while time.time() < deadline:
        detail = installed(call, first["sha256"])["environment"]["detail"]
        if detail == "Downloading psychopy (64.0MiB)":
            break
        time.sleep(0.02)
    assert detail == "Downloading psychopy (64.0MiB)"

    # A second setup is refused before its release is downloaded.
    count = len(downloads(hub))
    status, out = call(f"{API}/local/install", {**second, "setup": "auto"})
    assert status == 409 and out["error"]["code"] == "setup_busy"
    assert len(downloads(hub)) == count
    status, out = call(f"{API}/local/setup", {"sha256": first["sha256"]})
    assert status == 409 and out["error"]["code"] == "setup_busy"

    # No session starts while the build uses the disk and the network.
    status, out = call("/api/runs", {})
    assert status == 400 and "being set up" in out["error"]
    assert workspace.active is None

    # Closing the app stops the build, and the record says so.
    server.close_hub()
    record = json.loads(
        (workspace.directory / "hub" / "installs.json").read_text(encoding="utf-8")
    )[0]
    assert record["environment"]["state"] == "failed"
    assert record["error"]["code"] == "setup_cancelled"
    assert record["status"] == "installed"


def test_a_setup_cut_short_by_a_crash_is_marked_at_the_next_start(
    http,
    hub,
    tmp_path,
    workspace,
    uv,
):
    call, server = http
    connect(call, hub)
    body = add_release(hub, tmp_path)
    assert call(f"{API}/local/install", {**body, "setup": "auto"})[0] == 202
    wait_setup(call, body["sha256"])
    # What a killed process leaves: a record that still says "building".
    path = workspace.directory / "hub" / "installs.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    records[0]["environment"]["state"] = "building"
    records[0]["status"] = "installed"
    path.write_text(json.dumps(records), encoding="utf-8")
    server.close_hub()

    restarted = HubAdapter(workspace, server.data, uploader_wait_s=0.02)
    try:
        install = restarted._install_public(restarted.installs.record(body["sha256"]))
        assert install["environment"]["state"] == "failed"
        assert install["error"]["code"] == "setup_interrupted"
        assert "Start it again" in install["error"]["message"]
        assert not restarted.setting_up()
    finally:
        restarted.close()


def test_setup_needs_a_trusted_install(http, hub, uv):
    call, _ = http
    connect(call, hub)
    status, out = call(f"{API}/local/setup", {"sha256": "a" * 64})
    assert status == 409 and out["error"]["code"] == "not_trusted"
    status, out = call(f"{API}/local/setup", {"sha256": "nonsense"})
    assert status == 400


def test_setup_waits_for_a_running_session(http, hub, tmp_path, workspace, uv):
    call, _ = http
    connect(call, hub)
    body = add_release(hub, tmp_path)
    workspace.active = "some-run"
    try:
        status, out = call(f"{API}/local/install", {**body, "setup": "auto"})
        assert status == 409 and out["error"]["code"] == "run_active"
        status, out = call(f"{API}/local/setup", {"sha256": body["sha256"]})
        assert status == 409 and out["error"]["code"] == "run_active"
    finally:
        workspace.active = None
    assert downloads(hub) == []
