"""Machine tokens (alhazen.experiment_platform.machines and the routes in webapp): a rig's
dashboard reaches its owner's library on the local or the server host with a bearer token
made on the Machines page, can do nothing else with it, and reports when it closes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, call, experiment, web_dir

from alhazen.experiment_platform import accounts as accounts_module
from alhazen.experiment_platform.accounts import Accounts
from alhazen.experiment_platform.library import LibraryClient, LibraryError, LibrarySource
from alhazen.experiment_platform.local import LocalGate, LocalPlatform
from alhazen.experiment_platform.machines import MAX_EVENTS, Machines
from alhazen.experiment_platform.server import ServerConfig, ServerGate, validate
from alhazen.experiment_platform.service import PlatformService
from alhazen.experiment_platform.store import FileStore
from alhazen.experiment_platform.webapp import PlatformApp

PASSWORD = "correct horse battery"
CLOSED = {
    "event": "dashboard-closed",
    "machine": "booth-2.local",
    "alhazen": "3.5.0",
    "startedAt": "2026-10-19T10:00:00+00:00",
    "closedAt": "2026-10-19T12:00:00+00:00",
    "reason": "shut down from the page",
    "jobs": ["1792455795541", "bad id!"],
    "sessions": [{"experiment": "Dots", "task": "main", "outcomes": {"CORRECT": 7, "x": "no"}}],
}


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    s = FileStore(tmp_path / "store", create=True)
    add_version(s, "lab", "dots", experiment())
    return s


@pytest.fixture()
def server(tmp_path: Path, store: FileStore, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    accounts = Accounts(store.root / "accounts.sqlite3")
    accounts.add_user("ada", "Ada", PASSWORD)
    accounts.add_user("ben", "Ben", PASSWORD)
    config = validate(ServerConfig(store=store.root, host="127.0.0.1", port=8000))
    app = PlatformApp(
        PlatformService(store),
        ServerGate(config, accounts),
        web_dir=web_dir(tmp_path),
        machines=Machines(store.root / "machines.sqlite3"),
    )
    return app, accounts


def signed_in(app: PlatformApp, user: str) -> dict[str, str]:
    response = call(
        app,
        "POST",
        "/api/login",
        headers={"Content-Type": "application/json", "X-Alhazen-Platform": "1"},
        body=json.dumps({"username": user, "password": PASSWORD}).encode(),
    )
    assert (
        response.json()["capabilities"]["machineTokens"] is True
    )  # the page shows Machines at once
    cookie = response.headers["set-cookie"].split(";")[0]
    return {"Cookie": cookie, "X-Alhazen-CSRF": response.json()["csrf"], "X-Alhazen-Platform": "1"}


def post(app, path, headers, body):  # type: ignore[no-untyped-def]
    return call(
        app,
        "POST",
        path,
        headers={**headers, "Content-Type": "application/json"},
        body=json.dumps(body).encode(),
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestServerMachines:
    def test_a_token_is_shown_once_stored_hashed_and_lists_only_its_owners_copies(
        self, server, store
    ):
        app, _ = server
        ada, ben = signed_in(app, "ada"), signed_in(app, "ben")
        assert call(app, "GET", "/api/session", headers=ada).json()["capabilities"]["machineTokens"]
        assert call(app, "GET", "/api/session").json()["capabilities"]["machineTokens"] is False
        fork_id = post(app, "/api/forks", ada, {"publisher": "lab", "exp": "dots"}).json()["id"]
        post(app, "/api/forks", ben, {"publisher": "lab", "exp": "dots"})
        made = post(app, "/api/machines", ada, {"name": "Booth 2"}).json()
        token = made["token"]
        assert token.startswith("alm_") and made["machine"]["name"] == "Booth 2"
        raw = (store.root / "machines.sqlite3").read_bytes()
        assert token.encode() not in raw
        listed = call(app, "GET", "/api/machines", headers=ada).json()
        assert [m["name"] for m in listed["machines"]] == ["Booth 2"] and "token" not in json.dumps(
            listed
        )
        assert call(app, "GET", "/api/machines", headers=ben).json()["machines"] == []
        forks = call(app, "GET", "/api/forks", headers=bearer(token)).json()["forks"]
        assert list(forks) == [fork_id]
        handle = forks[fork_id]["head"]["path"]
        archive = call(app, "GET", f"/api/bundles/{handle}/archive", headers=bearer(token))
        assert archive.status == 200 and archive.headers["content-type"] == "application/zip"

    def test_a_token_does_nothing_but_read_the_library_and_report(self, server):
        app, _ = server
        ada = signed_in(app, "ada")
        token = post(app, "/api/machines", ada, {"name": "Booth 2"}).json()["token"]
        assert call(app, "GET", "/api/catalogue", headers=bearer(token)).status == 403
        for method, path, body in [
            ("POST", "/api/forks", {"publisher": "lab", "exp": "dots"}),
            ("POST", "/api/machines", {"name": "another"}),
            ("GET", "/api/machines", None),
            ("GET", "/api/session", None),
        ]:
            got = call(
                app,
                method,
                path,
                headers={**bearer(token), "Content-Type": "application/json"},
                body=json.dumps(body).encode() if body else b"",
            )
            assert got.status == 403, (method, path, got.status)
        handle = "L~lab~dots~" + "0" * 10
        assert (
            call(app, "GET", f"/api/bundles/{handle}/archive", headers=bearer(token)).status == 403
        )

    def test_a_report_is_kept_cleaned_and_shown_to_its_owner_only(self, server):
        app, _ = server
        ada, ben = signed_in(app, "ada"), signed_in(app, "ben")
        token = post(app, "/api/machines", ada, {"name": "Booth 2"}).json()["token"]
        kept = post(app, "/api/machines/events", bearer(token), CLOSED).json()["event"]
        assert kept["machine"] == "Booth 2" and kept["reportedName"] == "booth-2.local"
        assert kept["jobs"] == ["1792455795541"] and kept["sessions"][0]["outcomes"] == {
            "CORRECT": 7
        }
        (ev,) = call(app, "GET", "/api/machines", headers=ada).json()["events"]
        assert ev["sessions"][0]["experiment"] == "Dots"
        assert call(app, "GET", "/api/machines", headers=ben).json()["events"] == []
        # a signed-in browser cannot pose as a machine
        assert post(app, "/api/machines/events", ada, CLOSED).status == 403
        assert post(app, "/api/machines/events", bearer(token), {"event": "nope"}).status == 400

    def test_revoking_or_disabling_cuts_the_machine_off(self, server):
        app, accounts = server
        ada = signed_in(app, "ada")
        one = post(app, "/api/machines", ada, {"name": "One"}).json()
        two = post(app, "/api/machines", ada, {"name": "Two"}).json()
        gone = call(app, "DELETE", f"/api/machines/{one['machine']['id']}", headers=ada)
        assert gone.status == 200
        assert call(app, "GET", "/api/forks", headers=bearer(one["token"])).status == 401
        assert call(app, "GET", "/api/forks", headers=bearer(two["token"])).status == 200
        ben = signed_in(app, "ben")
        assert (
            call(app, "DELETE", f"/api/machines/{two['machine']['id']}", headers=ben).status == 404
        )
        accounts.set_disabled("ada", True)
        assert call(app, "GET", "/api/forks", headers=bearer(two["token"])).status == 401
        assert call(app, "GET", "/api/forks", headers=bearer("alm_made-up")).status == 401

    def test_changes_from_the_browser_still_need_the_csrf_token(self, server):
        app, _ = server
        ada = signed_in(app, "ada")
        no_csrf = {k: v for k, v in ada.items() if k != "X-Alhazen-CSRF"}
        assert post(app, "/api/machines", no_csrf, {"name": "x"}).status == 403


def test_events_are_bounded_per_person(tmp_path: Path):
    machines = Machines(tmp_path / "m.sqlite3")
    record, _ = machines.create("u1", "Rig")
    for _ in range(MAX_EVENTS + 5):
        machines.record("u1", record["id"], "Rig", CLOSED)
    assert len(machines.events("u1", limit=10_000)) == MAX_EVENTS


def test_a_rig_connects_to_a_real_local_platform_with_a_machine_token(tmp_path: Path):
    """End to end over HTTP: the token survives the launch token, the client lists, downloads
    and reports, and a revoked token is told to connect again."""
    import urllib.request

    store = FileStore(tmp_path / "store", create=True)
    add_version(store, "lab", "dots", experiment())
    platform = LocalPlatform(store, name="Me")
    platform.start()

    def browser(method: str, path: str, body: dict | None = None):  # type: ignore[no-untyped-def]
        request = urllib.request.Request(
            platform.origin + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Alhazen-Token": platform.token, "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())

    try:
        fork_id = browser("POST", "/api/forks", {"publisher": "lab", "exp": "dots"})["id"]
        made = browser("POST", "/api/machines", {"name": "Booth 2"})
        source = LibrarySource.from_url(platform.origin, made["token"], copies=tmp_path / "c")
        client = LibraryClient(source)
        forks = client.forks()
        assert list(forks) == [fork_id]
        assert client.archive(forks[fork_id]["head"]["path"])[:2] == b"PK"
        assert client.report(CLOSED)["ok"] is True
        assert browser("GET", "/api/machines")["events"][0]["machine"] == "Booth 2"
        browser("DELETE", f"/api/machines/{made['machine']['id']}")
        with pytest.raises(LibraryError, match="Connect the library again"):
            client.forks()
    finally:
        platform.shutdown()


def test_a_local_session_offers_machine_tokens_only_when_the_host_keeps_them(tmp_path: Path, store):
    plain = PlatformApp(
        PlatformService(store), LocalGate(8000, "t" * 43, "Me"), web_dir=web_dir(tmp_path)
    )
    caps = call(plain, "GET", "/api/session", headers={"X-Alhazen-Token": "t" * 43}).json()[
        "capabilities"
    ]
    assert caps["machineTokens"] is False
