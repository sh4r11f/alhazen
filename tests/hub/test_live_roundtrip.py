"""One real round trip through every Experiment Hub component, over loopback HTTP.

Nothing here stands in for an interface. The test starts:

* the central hub as ``alhazen hub serve`` (real uvicorn, its own process), with
  a database initialised and invites created through ``alhazen.hub.admin``;
* a clean rig as ``alhazen dashboard --hub --no-browser`` (its own process,
  its own state folder), driven only through the dashboard's HTTP API with the
  workspace token read from its ``server.json`` holder record.

Then, in order: two synthetic users register with admin invites; the author
packs the source-checked scaffold (``alhazen new`` + the documentation
fixture) with the real package builder and publishes one pinned version; a
later private version does not move the publication; the collector pins it in
their library; the rig connects, signs in as the collector, trusts and
installs the release with this test's interpreter; the existing Workspace
launch API runs a headless 2-trial simulate session on ``alhazen/laptop``; the
completed session is previewed, consented and uploaded by the rig's job
worker; the collector queries the indexed trials, exports CSV and JSON and
downloads every original file, each checked against the rig's own bytes; the
author and an anonymous caller are refused the collector's data.

Portable by default (SQLite, all paths under ``tmp_path``, loopback only, no
browser, no hardware, synthetic users and data). Set
``ALHAZEN_HUB_TEST_POSTGRES_URL`` to an EMPTY disposable PostgreSQL database
(the convention of tests/hub/server_support.py) to run the hub on it: the test
gets its own schema and passes the URL to the service through the
environment, never through a file.

Tokens, passwords and process logs are never printed; logs stay in
``tmp_path`` for a failing run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from alhazen._scaffold import scaffold  # noqa: E402
from alhazen.hub import admin  # noqa: E402
from alhazen.hub.packages import build_bundle, suggest_files  # noqa: E402
from alhazen.hub.settings import load_settings  # noqa: E402
from tests.hub.server_support import POSTGRES_ENV, _database_url  # noqa: E402

pytestmark = pytest.mark.slow

API = "/api/hub/v1"
SOURCE = Path(__file__).resolve().parents[2] / "src"
DOCS_FIXTURE = Path(__file__).parent / "fixtures" / "documentation" / "scaffold" / "docs"
DB_ENV = "ALHAZEN_HUB_E2E_DATABASE_URL"  # only inside the hub process's environment
SUBJECT = "SYNTH01"
TRIALS = 2
# Generous bounds for a loaded CI machine; every wait below names what it waits for.
START_S = 60.0
RUN_S = 240.0
UPLOAD_S = 180.0
INDEX_S = 60.0


# -- small, safe helpers ----------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for(what: str, check: Callable[[], Any], timeout: float, every: float = 0.25) -> Any:
    """Poll ``check`` until it returns something truthy; fail naming ``what``."""
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            value = check()
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            last_error = type(exc).__name__
            value = None
        if value:
            return value
        time.sleep(every)
    pytest.fail(f"timed out after {timeout:.0f} s waiting for {what} (last error: {last_error})")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def child_env(**extra: str) -> dict[str, str]:
    """This checkout's source first on the path, so both processes (and the
    interpreter the rig probes and runs) import exactly the code under test."""
    env = {k: v for k, v in os.environ.items() if k not in (POSTGRES_ENV, DB_ENV)}
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(SOURCE)] + ([inherited] if inherited else []))
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra)
    return env


def stop(process: subprocess.Popen[bytes] | None, timeout: float = 15.0) -> None:
    """Ask a child to stop (SIGINT, the way an operator does), then insist."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout)


def safe_cell(text: str) -> str:
    """The hub's documented CSV export rule (docs/hub/server.md): a cell that
    a spreadsheet would evaluate is prefixed with an apostrophe, unless it is
    a plain number."""
    try:
        float(text)
        is_number = text.strip() == text and text not in ("", "nan", "inf", "-inf")
    except ValueError:
        is_number = False
    if text.startswith(("=", "+", "-", "@", "\t", "\r")) and not is_number:
        return "'" + text
    return text


def canonical_manifest_digest(
    experiment_id: str, version_id: str, files: list[dict[str, Any]], metadata: dict[str, Any]
) -> str:
    """The receipt identity as docs/hub/server.md defines it, computed here
    from the rig's own files, independently of the server's code."""
    keys = ("subject_code", "mode", "rig_alias", "started_at")
    body = {
        "experiment_id": experiment_id,
        "version_id": version_id,
        "files": sorted(
            ({"path": f["path"], "sha256": f["sha256"], "size": f["size"]} for f in files),
            key=lambda f: f["path"],
        ),
        "metadata": {k: metadata.get(k) for k in keys},
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(text.encode("utf-8"))


# -- the two processes -----------------------------------------------------------------------


class CentralHub:
    """``alhazen hub serve`` on a loopback port, configured under tmp_path."""

    def __init__(self, root: Path) -> None:
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        root.mkdir(parents=True)
        self.config = root / "hub.toml"
        database = _database_url(root)
        self.env = child_env(**{DB_ENV: database})
        self.config.write_text(
            "\n".join(
                [
                    "[server]",
                    f'public_origin = "{self.origin}"',
                    "",
                    "[database]",
                    f'url_env = "{DB_ENV}"',
                    "",
                    "[storage]",
                    f'artifact_root = "{(root / "artifacts").as_posix()}"',
                    "min_free_bytes = 1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.settings = load_settings(self.config, environ={DB_ENV: database})
        self.backend = "postgresql" if database.startswith("postgresql") else "sqlite"
        admin.init_database(self.settings)
        self.log = root / "hub.log"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "alhazen",
                    "hub",
                    "serve",
                    "--config",
                    str(self.config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                ],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        wait_for("the hub's /readyz", self._ready, START_S)

    def _ready(self) -> bool:
        if self.process is not None and self.process.poll() is not None:
            pytest.fail(f"the hub exited with {self.process.returncode}; see {self.log}")
        return httpx.get(self.origin + API + "/readyz", timeout=5).status_code == 200

    def invite(self) -> str:
        return admin.create_invite(self.settings, actor="e2e-test", note="synthetic").code


class Rig:
    """``alhazen dashboard --hub --no-browser`` with its own state folder.

    The workspace token is read from the holder record's URL fragment
    (server.json), the way the browser page gets it; it is never printed."""

    def __init__(self, root: Path) -> None:
        self.state = root / "rig-state"
        self.log = root / "dashboard.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.client: httpx.Client | None = None

    def start(self) -> None:
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "alhazen",
                    "dashboard",
                    "--hub",
                    "--no-browser",
                    "--state-dir",
                    str(self.state),
                    "--port",
                    "0",
                ],
                env=child_env(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        url = wait_for("the dashboard's holder record", self._holder_url, START_S)
        parts = urlsplit(url)
        token = parts.fragment.removeprefix("token=")
        assert token and parts.hostname == "127.0.0.1"
        self.client = httpx.Client(
            base_url=f"{parts.scheme}://{parts.netloc}",
            headers={"X-Alhazen-Token": token},
            timeout=60,
            follow_redirects=False,
        )

    def _holder_url(self) -> str | None:
        if self.process is not None and self.process.poll() is not None:
            pytest.fail(f"the dashboard exited with {self.process.returncode}; see {self.log}")
        record = json.loads((self.state / "server.json").read_text(encoding="utf-8"))
        return record.get("url")

    def call(self, method: str, path: str, *, expect: int | tuple[int, ...], **kw: Any) -> Any:
        assert self.client is not None
        response = self.client.request(method, path, **kw)
        wanted = (expect,) if isinstance(expect, int) else expect
        assert response.status_code in wanted, (path, response.status_code, _error(response))
        return response.json()

    def hub(self, method: str, route: str, *, expect: int | tuple[int, ...], **kw: Any) -> Any:
        return self.call(method, API + route, expect=expect, **kw)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        stop(self.process)


def _error(response: Any) -> Any:
    """A refusal's code and message (never a token: the hub and the rig keep
    those out of error bodies)."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    return body.get("error", body) if isinstance(body, dict) else body


# -- synthetic people and source --------------------------------------------------------------


class Account:
    def __init__(self, hub: CentralHub, username: str) -> None:
        self.username = username
        self.password = secrets.token_urlsafe(24)  # 32 characters, never printed
        response = httpx.post(
            hub.origin + API + "/auth/register",
            json={
                "username": username,
                "display_name": username.replace("_", " ").title(),
                "password": self.password,
                "invite_code": hub.invite(),
            },
            headers={"Origin": hub.origin},
            timeout=30,
        )
        assert response.status_code == 201, _error(response)
        self.user = response.json()["user"]
        token = httpx.post(
            hub.origin + API + "/auth/token",
            json={"username": username, "password": self.password},
            timeout=30,
        )
        assert token.status_code == 200, _error(token)
        self.client = httpx.Client(
            base_url=hub.origin + API,
            headers={"Authorization": "Bearer " + token.json()["access_token"]},
            timeout=60,
            follow_redirects=False,
        )


def scaffold_package(root: Path, version: str) -> tuple[Path, dict[str, Any]]:
    """``alhazen new fixation_demo`` plus the source-checked documentation
    fixture, packed by the real builder from its own file suggestion."""
    source = scaffold("fixation_demo", root / f"src-{version}")
    shutil.copytree(DOCS_FIXTURE, source / "docs")
    metadata = {
        "name": "fixation-demo",
        "version": version,
        "title": "Fixation hold: source-checked example",
        "description": "Synthetic end-to-end test package; not a validated experiment.",
        "entrypoint": "run.py",
        "python_min": "3.10",
        "alhazen_min": "2.13.0",
        "platforms": ["linux", "darwin", "win32"],
        "hardware": {"display": True, "eye_tracker": True, "reward": False},
        "license": "MIT",
        "citations": [],
        "documentation": "docs/experiment.json",
    }
    bundle = root / f"fixation-demo-{version}.zip"
    build_bundle(source, bundle, metadata, suggest_files(source))
    return bundle, metadata


# -- the test -------------------------------------------------------------------------------------


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[CentralHub, Rig]]:
    hub = CentralHub(tmp_path / "hub")
    rig = Rig(tmp_path / "rig")
    try:
        hub.start()
        rig.start()
        yield hub, rig
    finally:
        rig.close()
        stop(hub.process)


def test_publish_install_simulate_upload_query_export(
    stack: tuple[CentralHub, Rig], tmp_path: Path
) -> None:
    hub, rig = stack
    author = Account(hub, "author_e2e")
    collector = Account(hub, "collector_e2e")
    anonymous = httpx.Client(base_url=hub.origin + API, timeout=30, follow_redirects=False)
    try:
        # 1. The author publishes one pinned source release.
        bundle, metadata = scaffold_package(tmp_path / "author", "0.1.0")
        response = author.client.post(
            "/experiments",
            json={
                "title": metadata["title"],
                "summary": "Synthetic round-trip fixture.",
                "description": metadata["description"],
                "license": "MIT",
                "citations": [],
                "tags": ["synthetic"],
            },
        )
        assert response.status_code == 201, _error(response)
        experiment_id = response.json()["experiment"]["id"]
        response = author.client.post(
            f"/experiments/{experiment_id}/versions",
            content=bundle.read_bytes(),
            headers={"Content-Type": "application/zip"},
        )
        assert response.status_code == 201, _error(response)
        version = response.json()["version"]
        assert version["sha256"] == sha256(bundle.read_bytes())
        response = author.client.post(
            f"/experiments/{experiment_id}/publish",
            json={"version_id": version["id"], "license_ack": True, "data_excluded_ack": True},
        )
        assert response.status_code == 200, _error(response)

        # A later private version does not move the publication.
        later, _ = scaffold_package(tmp_path / "author", "0.2.0")
        response = author.client.post(
            f"/experiments/{experiment_id}/versions",
            content=later.read_bytes(),
            headers={"Content-Type": "application/zip"},
        )
        assert response.status_code == 201, _error(response)
        later_id = response.json()["version"]["id"]
        catalog = anonymous.get("/catalog").json()["items"]
        (listed,) = [i for i in catalog if i["experiment"]["id"] == experiment_id]
        assert listed["version"]["id"] == version["id"]
        assert listed["version"]["sha256"] == version["sha256"]
        public = anonymous.get(f"/experiments/{experiment_id}").json()
        assert later_id not in json.dumps(public)
        doc = anonymous.get(f"/experiments/{experiment_id}/versions/{version['id']}/documentation")
        assert doc.status_code == 200 and doc.json()["documentation"]["tasks"], _error(doc)

        # 2. The collector pins the published release.
        response = collector.client.post(
            "/library", json={"experiment_id": experiment_id, "version_id": version["id"]}
        )
        assert response.status_code in (200, 201), _error(response)

        # 3. A clean rig: connect, sign in, trust and install with this interpreter.
        status = rig.hub("GET", "/local/status", expect=200)
        assert status["state"] == "not_configured" and status["installed"] == []
        rig.hub(
            "POST",
            "/local/connect",
            json={"url": hub.origin, "allow_http_loopback": True},
            expect=200,
        )
        signed = rig.hub(
            "POST",
            "/auth/login",
            json={"username": collector.username, "password": collector.password},
            expect=200,
        )
        assert signed["user"]["id"] == collector.user["id"]
        status = rig.hub("GET", "/local/status", expect=200)
        assert status["state"] == "signed_in" and status["user"]["id"] == collector.user["id"]
        assert not [key for key in status if "token" in key.lower()]  # the bearer stays server-side
        installed = rig.hub(
            "POST",
            "/local/install",
            json={
                "experiment_id": experiment_id,
                "version_id": version["id"],
                "sha256": version["sha256"],
                "trust_code": True,
                "python": sys.executable,
            },
            expect=201,
        )["install"]
        assert installed["sha256"] == version["sha256"]
        assert installed["version_id"] == version["id"]
        assert installed["status"] == "registered" and installed["project_id"]
        project_id = installed["project_id"]

        # 4. The existing Workspace launch API: a headless simulate session.
        run = rig.call(
            "POST",
            "/api/runs",
            json={
                "project": project_id,
                "mode": "simulate",
                "rig": "alhazen/laptop",
                "headless": True,
                "seed": 23,
                "subject": SUBJECT,
                "session": 1,
                "trials": TRIALS,
            },
            expect=201,
        )

        def finished() -> dict[str, Any] | None:
            state = rig.call("GET", "/api/state", expect=200)
            (record,) = [r for r in state["runs"] if r["id"] == run["id"]]
            return record if record["status"] not in ("running", "stopping") else None

        record = wait_for("the simulate session to finish", finished, RUN_S, every=1.0)
        assert record["status"] == "completed" and record["returncode"] == 0, (
            record["status"],
            record.get("returncode"),
            f"console log under {rig.state}",
        )
        # Pending seam (docs/hub/integration-test.md): run records do not yet
        # carry the pinned release (experiment, version, sha256); assert it here
        # once the rig's provenance snapshot lands.

        # 5. Preview, consent and upload the completed session.
        sessions = rig.hub("GET", "/local/sessions", params={"project_id": project_id}, expect=200)
        done = [s for s in sessions["items"] if s["complete"] and s["mode"] == "simulate"]
        assert len(done) == 1, [(s["run_id"], s["complete"]) for s in sessions["items"]]
        local = done[0]
        selection = {
            "project_id": project_id,
            "root_id": local["root_id"],
            "run_id": local["run_id"],
        }
        preview = rig.hub("POST", "/local/upload-preview", json=selection, expect=200)
        assert preview["recipient"]["user"]["id"] == collector.user["id"]
        assert preview["recipient"]["base_url"] == hub.origin
        assert (preview["experiment_id"], preview["version_id"]) == (experiment_id, version["id"])
        assert preview["privacy"]["warning"]
        job = rig.hub(
            "POST",
            "/local/upload",
            json={
                **selection,
                "experiment_id": experiment_id,
                "version_id": version["id"],
                "preview_id": preview["preview_id"],
                "consent": True,
            },
            expect=202,
        )["job"]

        def settled() -> dict[str, Any] | None:
            current = rig.hub("GET", f"/local/jobs/{job['id']}", expect=200)["job"]
            return (
                current
                if current["status"] in ("completed", "failed", "cancelled", "paused")
                else None
            )

        job = wait_for("the upload job to settle", settled, UPLOAD_S, every=0.5)
        assert job["status"] == "completed", (job["status"], job.get("error"))
        receipt = job["receipt"]
        assert receipt["status"] == "committed"
        session_id = receipt["id"]

        # The rig's own files: every one in the completed session folder.
        folder = _session_folder(project_id, local, rig)
        originals = {
            p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*"))
            if p.is_file()
        }
        listing = [{"path": k, "size": len(v), "sha256": sha256(v)} for k, v in originals.items()]
        assert receipt["file_count"] == len(originals)
        assert receipt["total_bytes"] == sum(len(v) for v in originals.values())

        # 6. The collector's private view: metadata, receipt identity, artifacts.
        detail = collector.client.get(f"/data/sessions/{session_id}")
        assert detail.status_code == 200, _error(detail)
        detail = detail.json()
        assert detail["session"]["status"] == "committed"
        assert detail["session"]["metadata"]["subject_code"] == SUBJECT
        assert detail["session"]["metadata"]["mode"] == "simulate"
        assert sorted(a["path"] for a in detail["artifacts"]) == sorted(originals)
        assert {a["path"]: a["sha256"] for a in detail["artifacts"]} == {
            f["path"]: f["sha256"] for f in listing
        }
        assert receipt["manifest_sha256"] == canonical_manifest_digest(
            experiment_id, version["id"], listing, detail["session"]["metadata"]
        )
        listed_ids = [
            s["id"]
            for s in collector.client.get(
                "/data/sessions", params={"experiment_id": experiment_id}
            ).json()["items"]
        ]
        assert listed_ids == [session_id]

        # 7. The derived index: exactly the rig's trial rows.
        (trials_name,) = [k for k in originals if k.endswith("_trials.csv")]
        local_rows = list(csv.DictReader(io.StringIO(originals[trials_name].decode("utf-8"))))
        local_header = next(csv.reader(io.StringIO(originals[trials_name].decode("utf-8"))))
        assert len(local_rows) == TRIALS

        def indexed() -> dict[str, Any] | None:
            page = collector.client.get(f"/data/sessions/{session_id}/trials").json()
            return page if page["index"]["status"] == "indexed" else None

        page = wait_for("the trial index", indexed, INDEX_S, every=0.5)
        assert page["columns"] == local_header
        assert [item["values"] for item in page["items"]] == local_rows
        assert {item["source_path"] for item in page["items"]} == {trials_name}

        # 8. Exports: CSV (spreadsheet-safe) and JSON, both equal to the rig's rows.
        exported = collector.client.get(
            f"/data/sessions/{session_id}/export", params={"format": "csv"}
        )
        assert exported.status_code == 200, _error(exported)
        assert "attachment" in exported.headers["content-disposition"]
        rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8"))))
        assert rows[0] == local_header
        assert rows[1:] == [[safe_cell(r[c]) for c in local_header] for r in local_rows]
        as_json = collector.client.get(
            f"/data/sessions/{session_id}/export", params={"format": "json"}
        )
        assert as_json.status_code == 200, _error(as_json)
        body = as_json.json()
        assert body["session_id"] == session_id and body["columns"] == local_header
        assert [r["values"] for r in body["rows"]] == local_rows

        # The same export through the rig's proxy is byte for byte the hub's.
        assert rig.client is not None
        relayed = rig.client.get(
            f"{API}/data/sessions/{session_id}/export", params={"format": "csv"}
        )
        assert relayed.status_code == 200 and relayed.content == exported.content

        # 9. Every original file, byte for byte and by its announced digest.
        for path, data in originals.items():
            got = collector.client.get(f"/data/sessions/{session_id}/files", params={"path": path})
            assert got.status_code == 200, (path, _error(got))
            assert got.content == data, path
            assert got.headers["x-alhazen-sha256"] == sha256(data), path

        # 10. Nobody else reads the collector's data.
        private = [
            f"/data/sessions/{session_id}",
            f"/data/sessions/{session_id}/trials",
            f"/data/sessions/{session_id}/export?format=csv",
            f"/data/sessions/{session_id}/files?path={trials_name}",
        ]
        for route in private:
            denied = author.client.get(route)
            assert denied.status_code == 404, (route, denied.status_code)
            assert session_id not in denied.text
            refused = anonymous.get(route)
            assert refused.status_code == 401, (route, refused.status_code)
        assert author.client.get("/data/sessions").json()["items"] == []
        assert anonymous.get("/data/sessions").status_code == 401

        print(
            f"live round trip OK ({hub.backend}): 1 published version, "
            f"{len(originals)} files / {receipt['total_bytes']} bytes committed, "
            f"{len(page['items'])} indexed trials, CSV+JSON exports and originals verified, "
            "author and anonymous refused"
        )
    finally:
        anonymous.close()
        author.client.close()
        collector.client.close()


def _session_folder(project_id: str, local: dict[str, Any], rig: Rig) -> Path:
    """The completed session's folder on the rig, from the data roots the
    dashboard itself reports (never a path guessed by this test)."""
    roots = rig.call("GET", "/api/data/roots", params={"project": project_id}, expect=200)
    (root,) = [r for r in roots["roots"] if r["id"] == local["root_id"]]
    folder = Path(root["path"]) / local["run_id"]
    assert folder.is_dir() and (folder / "manifest.yaml").is_file()
    assert rig.state in folder.resolve().parents  # the installed release's data, on this rig
    return folder
