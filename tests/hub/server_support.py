"""Shared pieces of the hub service tests: settings on tmp_path, a fake clock,
a real app in-process, browser and bearer callers, synthetic packages and
sessions. No network, no real data: every user, package and session here is
synthetic.

PostgreSQL: set ALHAZEN_HUB_TEST_POSTGRES_URL to an EMPTY disposable database
(for example a local Unix-socket instance) to run the same tests against it;
each test then gets its own schema in that database.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alhazen.hub import admin
from alhazen.hub.app import create_app
from alhazen.hub.packages import build_bundle
from alhazen.hub.settings import HubLimits, HubSettings

ORIGIN = "http://127.0.0.1:8750"
API = "/api/hub/v1"
PASSWORD = "correct horse battery"
POSTGRES_ENV = "ALHAZEN_HUB_TEST_POSTGRES_URL"


class FakeClock:
    def __init__(self) -> None:
        # Real time at the start, so invites the operator helpers stamp with
        # the system clock are valid; tests then move it by hand.
        self.now = int(time.time() * 1000)

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * 1000)


def _database_url(tmp_path: Path) -> str:
    base = os.environ.get(POSTGRES_ENV)
    if not base:
        return f"sqlite:///{(tmp_path / 'hub.sqlite3').as_posix()}"
    # One schema per test in the given database, so tests never share rows.
    import sqlalchemy

    schema = "t_" + secrets.token_hex(6)
    engine = sqlalchemy.create_engine(base.replace("postgresql://", "postgresql+psycopg://", 1))
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
    engine.dispose()
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}options=-csearch_path%3D{schema}"


def make_settings(tmp_path: Path, **limit_overrides: Any) -> HubSettings:
    limits = replace(HubLimits(), **limit_overrides) if limit_overrides else HubLimits()
    return HubSettings(
        database_url=_database_url(tmp_path),
        artifact_root=(tmp_path / "artifacts").resolve(),
        public_origin=ORIGIN,
        min_free_bytes=1,
        limits=limits,
    )


class Hub:
    """One running service with helpers to act as its users and operator."""

    def __init__(self, settings: HubSettings, clock: FakeClock) -> None:
        self.settings = settings
        self.clock = clock
        self.app = create_app(settings, clock=clock, start_maintenance=False)
        self.client = TestClient(self.app, base_url=ORIGIN)

    @property
    def maintenance(self) -> Any:
        return self.app.state.maintenance

    def invite(self) -> str:
        return admin.create_invite(self.settings, actor="test-operator").code

    def register(self, username: str, password: str = PASSWORD) -> dict[str, Any]:
        response = self.client.post(
            f"{API}/auth/register",
            json={
                "username": username,
                "display_name": username.title(),
                "password": password,
                "invite_code": self.invite(),
            },
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 201, response.text
        return response.json()["user"]

    def browser(self, username: str, password: str = PASSWORD) -> Browser:
        return Browser(self, username, password)

    def bearer(self, username: str, password: str = PASSWORD) -> Bearer:
        return Bearer(self, username, password)


class Caller:
    hub: Hub
    client: TestClient

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(
            API + path, headers={**self.headers(), **kwargs.pop("headers", {})}, **kwargs
        )

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        if body is not None:
            kwargs["json"] = body
        return self.client.post(
            API + path, headers={**self.headers(), **kwargs.pop("headers", {})}, **kwargs
        )

    def patch(self, path: str, body: Any, **kwargs: Any) -> Any:
        return self.client.patch(
            API + path, json=body, headers={**self.headers(), **kwargs.pop("headers", {})}, **kwargs
        )

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.client.put(
            API + path, headers={**self.headers(), **kwargs.pop("headers", {})}, **kwargs
        )

    # -- workflows ---------------------------------------------------------

    def create_experiment(self, **fields: Any) -> dict[str, Any]:
        body = {"title": "Saccade bias", "summary": "Synthetic", "license": "MIT", **fields}
        response = self.post("/experiments", body)
        assert response.status_code == 201, response.text
        return response.json()["experiment"]

    def upload_version(self, experiment_id: str, bundle: Path) -> Any:
        return self.post(
            f"/experiments/{experiment_id}/versions",
            content=bundle.read_bytes(),
            headers={"Content-Type": "application/zip"},
        )

    def publish(self, experiment_id: str, version_id: str) -> Any:
        return self.post(
            f"/experiments/{experiment_id}/publish",
            {"version_id": version_id, "license_ack": True, "data_excluded_ack": True},
        )

    def upload_session(
        self,
        experiment_id: str,
        version_id: str,
        files: dict[str, bytes],
        *,
        client_id: str = "run-1",
        metadata: dict[str, Any] | None = None,
        chunk: int = 7,
        complete: bool = True,
    ) -> dict[str, Any]:
        response = self.post(
            "/sessions/init", init_body(experiment_id, version_id, files, client_id, metadata)
        )
        assert response.status_code in (200, 201), response.text
        session = response.json()
        for path, data in files.items():
            for offset in range(0, max(len(data), 1), chunk):
                part = data[offset : offset + chunk]
                r = self.put_chunk(session["id"], path, offset, part)
                assert r.status_code == 200, r.text
        if not complete:
            return session
        done = self.post(f"/sessions/{session['id']}/complete")
        assert done.status_code == 200, done.text
        return done.json()

    def put_chunk(
        self, session_id: str, path: str, offset: int, data: bytes, digest: str | None = None
    ) -> Any:
        return self.put(
            f"/sessions/{session_id}/files",
            params={"path": path, "offset": offset},
            content=data,
            headers={
                "X-Chunk-SHA256": digest or sha(data),
                "Content-Type": "application/octet-stream",
            },
        )


class Browser(Caller):
    """A signed-in web page: cookie, Origin and CSRF token."""

    def __init__(self, hub: Hub, username: str, password: str) -> None:
        self.hub = hub
        self.client = TestClient(hub.app, base_url=ORIGIN)
        response = self.client.post(
            f"{API}/auth/login",
            json={"username": username, "password": password},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]
        self.user = response.json()["user"]

    def headers(self) -> dict[str, str]:
        return {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}


class Bearer(Caller):
    """A rig or CLI: bearer token, no cookie, no Origin."""

    def __init__(self, hub: Hub, username: str, password: str) -> None:
        self.hub = hub
        self.client = TestClient(hub.app, base_url=ORIGIN)
        response = self.client.post(
            f"{API}/auth/token", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        self.token = response.json()["access_token"]
        self.user = response.json()["user"]

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def init_body(
    experiment_id: str,
    version_id: str,
    files: dict[str, bytes],
    client_id: str = "run-1",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "version_id": version_id,
        "client_session_id": client_id,
        "files": [{"path": p, "size": len(d), "sha256": sha(d)} for p, d in files.items()],
        "metadata": metadata
        if metadata is not None
        else {
            "subject_code": "S01",
            "mode": "run",
            "rig_alias": "bench",
            "started_at": "2026-10-09T10:00:00Z",
        },
        "consent": True,
    }


def make_bundle(
    tmp_path: Path,
    *,
    name: str = "saccade-bias",
    version: str = "1.0.0",
    license: str = "MIT",
    extra: dict[str, bytes] | None = None,
) -> Path:
    source = tmp_path / f"src-{name}-{version}-{secrets.token_hex(3)}"
    files = {
        "run.py": b"print('synthetic experiment')\n",
        "configs/task.yaml": f"trials: 10\nversion: {version}\n".encode(),
        **(extra or {}),
    }
    for rel, data in files.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    out = tmp_path / f"{name}-{version}-{secrets.token_hex(3)}.zip"
    meta = {
        "name": name,
        "version": version,
        "title": "Saccade bias",
        "description": "Synthetic.",
        "hardware": {"display": True, "eye_tracker": False, "reward": False},
        "license": license,
        "citations": [],
    }
    build_bundle(source, out, meta, sorted(files))
    return out


TRIALS = (
    b"trial_index,outcome,rt,label\n"
    b'0,CORRECT,0.31,=HYPERLINK("x")\n'
    b"1,WRONG,-0.5,plain\n"
    b"2,CORRECT,0.29,+cmd\n"
)


def session_files() -> dict[str, bytes]:
    return {
        "sub-01_ses-01_run-01_trials.csv": TRIALS,
        "session.json": json.dumps({"subject": "S01"}).encode(),
        "figures/empty.txt": b"",
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> HubSettings:
    value = make_settings(tmp_path)
    admin.init_database(value)
    return value


@pytest.fixture
def hub(settings: HubSettings, clock: FakeClock) -> Hub:
    return Hub(settings, clock)
