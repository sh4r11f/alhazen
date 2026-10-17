"""Both standalone hosts over real sockets: the local host in a thread, and the server as
it is deployed (``alhazen platform serve`` in its own process, behind waitress), with two
accounts, a restart, and the data still there afterwards."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, experiment, zip_of

from alhazen.experiment_platform.local import LocalPlatform
from alhazen.experiment_platform.store import FileStore

pytest.importorskip("waitress")

PASSWORD = "integration password one"


def request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_the_local_host_serves_its_user_over_a_real_socket(tmp_path: Path):
    store = FileStore(tmp_path / "store", create=True)
    add_version(store, "lab", "dots", experiment())
    platform = LocalPlatform(store, name="Me")
    platform.start()
    try:
        assert platform.url.startswith(f"http://127.0.0.1:{platform.port}/#token=")
        status, _, _ = request(platform.port, "GET", "/api/catalogue")
        assert status == 401
        token = {"X-Alhazen-Token": platform.token}
        status, _, body = request(platform.port, "GET", "/api/catalogue", headers=token)
        assert status == 200 and json.loads(body)["published"]["lab"]["dots"]
        status, _, body = request(
            platform.port,
            "POST",
            "/api/forks",
            body=b'{"publisher":"lab","exp":"dots"}',
            headers={**token, "Content-Type": "application/json"},
        )
        assert status == 200 and json.loads(body)["id"].startswith("dots-")
    finally:
        platform.shutdown()


def cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alhazen", "platform", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture()
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    port = free_port()
    config = tmp_path / "platform.toml"
    store_dir = tmp_path / "server-store"
    made = cli(
        "server-init",
        "--store",
        str(store_dir),
        "--config",
        str(config),
        "--public-url",
        f"http://127.0.0.1:{port}",
        "--port",
        str(port),
    )
    assert made.returncode == 0, made.stderr
    add_version(FileStore(store_dir), "lab", "dots", experiment())
    for user in ("ada", "ben"):
        added = cli(
            "users",
            "add",
            user,
            "--name",
            user.capitalize(),
            "--config",
            str(config),
            "--password-stdin",
            stdin=PASSWORD + "\n",
        )
        assert added.returncode == 0, added.stderr

    def start() -> subprocess.Popen[str]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "alhazen", "platform", "serve", "--config", str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if request(port, "GET", "/api/session")[0] == 200:
                    return proc
            except OSError:
                time.sleep(0.2)
        proc.kill()
        raise AssertionError(
            "the server did not answer: " + (proc.stdout.read() if proc.stdout else "")
        )

    procs = [start()]
    yield port, start, procs
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=20)


def sign_in(port: int, user: str) -> dict[str, str]:
    status, headers, body = request(
        port,
        "POST",
        "/api/login",
        body=json.dumps({"username": user, "password": PASSWORD}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Alhazen-Platform": "1",
            "Origin": f"http://127.0.0.1:{port}",
        },
    )
    assert status == 200, body
    return {
        "Cookie": headers["set-cookie"].split(";")[0],
        "X-Alhazen-CSRF": json.loads(body)["csrf"],
    }


def test_the_server_keeps_two_users_apart_and_survives_a_restart(server):  # type: ignore[no-untyped-def]
    port, start, procs = server
    assert request(port, "GET", "/api/catalogue")[0] == 403
    ada, ben = sign_in(port, "ada"), sign_in(port, "ben")
    status, _, body = request(
        port,
        "POST",
        "/api/forks",
        body=b'{"publisher":"lab","exp":"dots"}',
        headers={**ada, "Content-Type": "application/json"},
    )
    assert status == 200
    fork = json.loads(body)
    status, _, body = request(
        port,
        "POST",
        f"/api/forks/{fork['id']}/revisions?message=fewer",
        body=zip_of(experiment(**{"configs/task.yaml": b"n_trials: 4\n"})),
        headers={**ada, "Content-Type": "application/zip"},
    )
    assert status == 200
    handle = json.loads(body)["fork"]["head"]["path"]
    assert json.loads(request(port, "GET", "/api/forks", headers=ben)[2]) == {"forks": {}}
    assert request(port, "GET", f"/api/bundles/{handle}/meta", headers=ben)[0] == 404
    assert request(port, "GET", f"/api/forks/{fork['id']}/docs", headers=ben)[0] == 404
    # a cookie without the CSRF token cannot change anything
    assert (
        request(port, "DELETE", f"/api/forks/{fork['id']}", headers={"Cookie": ada["Cookie"]})[0]
        == 403
    )

    procs[0].terminate()
    procs[0].wait(timeout=20)
    procs.append(start())
    # sessions and copies are durable: the same cookie still works after a restart
    status, _, body = request(port, "GET", "/api/forks", headers=ada)
    assert status == 200 and json.loads(body)["forks"][fork["id"]]["head"]["path"] == handle
