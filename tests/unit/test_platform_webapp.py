"""The HTTP interface of both standalone hosts, called as WSGI (no network): the local
host's token, Host and Origin checks, the server's sessions and CSRF, error mapping,
limits and security headers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.platform_support import add_version, call, experiment, web_dir, zip_of

from alhazen.experiment_platform import accounts as accounts_module
from alhazen.experiment_platform.accounts import Accounts
from alhazen.experiment_platform.errors import InvalidRequest
from alhazen.experiment_platform.local import LocalGate
from alhazen.experiment_platform.server import (
    ServerConfig,
    ServerGate,
    build_app,
    load_config,
    validate,
)
from alhazen.experiment_platform.service import PlatformService
from alhazen.experiment_platform.store import FileStore
from alhazen.experiment_platform.webapp import PlatformApp

TOKEN = "t" * 43
HOST = "127.0.0.1:8000"


@pytest.fixture()
def store(tmp_path: Path) -> FileStore:
    s = FileStore(tmp_path / "store", create=True)
    add_version(s, "lab", "dots", experiment())
    return s


@pytest.fixture()
def local(tmp_path: Path, store: FileStore) -> PlatformApp:
    return PlatformApp(
        PlatformService(store),
        LocalGate(8000, TOKEN, "Me"),
        web_dir=web_dir(tmp_path),
        max_upload=50_000,
    )


def api(app: PlatformApp, method: str, path: str, **kw):  # type: ignore[no-untyped-def]
    headers = {"X-Alhazen-Token": TOKEN, **kw.pop("headers", {})}
    return call(app, method, path, headers=headers, **kw)


class TestLocalHost:
    def test_the_page_and_its_assets_are_served_with_strict_headers(self, local: PlatformApp):
        page = call(local, "GET", "/")
        assert page.status == 200 and b"Alhazen" in page.body
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert (
            call(local, "GET", "/assets/platform.js")
            .headers["content-type"]
            .startswith("text/javascript")
        )
        assert call(local, "GET", "/assets/../store/platform-store.json").status == 404
        again = call(local, "GET", "/", headers={"If-None-Match": page.headers["etag"]})
        assert again.status == 304

    def test_the_api_needs_the_token(self, local: PlatformApp):
        assert call(local, "GET", "/api/catalogue").status == 401
        assert (
            call(local, "GET", "/api/catalogue", headers={"X-Alhazen-Token": "wrong"}).status == 401
        )
        assert (
            api(local, "GET", "/api/catalogue").json()["published"]["lab"]["dots"]["title"]
            == "Dots"
        )

    def test_a_foreign_host_or_origin_is_refused(self, local: PlatformApp):
        assert api(local, "GET", "/api/catalogue", host="evil.example:8000").status == 403
        assert call(local, "GET", "/", host="attacker.test").status == 403
        refused = api(
            local,
            "POST",
            "/api/forks",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
            body=b'{"publisher": "lab", "exp": "dots"}',
        )
        assert refused.status == 403

    def test_the_session_describes_a_single_local_user(self, local: PlatformApp):
        session = api(local, "GET", "/api/session").json()
        assert session["kind"] == "local" and session["viewer"]["name"] == "Me"
        assert (
            session["capabilities"]["changeWithMana"] is False
            and session["capabilities"]["uploadRevision"]
        )
        assert (
            api(
                local,
                "POST",
                "/api/login",
                headers={"Content-Type": "application/json"},
                body=b"{}",
            ).status
            == 403
        )

    def test_fork_upload_publish_round_trip(self, local: PlatformApp):
        created = api(
            local,
            "POST",
            "/api/forks",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"publisher": "lab", "exp": "dots"}).encode(),
        ).json()
        fork_id = created["id"]
        handle = created["fork"]["head"]["path"]
        meta = api(local, "GET", f"/api/bundles/{handle}/meta").json()
        chunk = json.loads(api(local, "GET", f"/api/bundles/{handle}/chunks/0").body)
        assert meta["fileCount"] == len(chunk) == 4
        upload = api(
            local,
            "POST",
            f"/api/forks/{fork_id}/revisions",
            query="message=fewer+trials",
            headers={"Content-Type": "application/zip"},
            body=zip_of(experiment(**{"configs/task.yaml": b"n_trials: 10\n"})),
        )
        assert upload.status == 200 and upload.json()["revision"]["message"] == "fewer trials"
        published = api(
            local,
            "POST",
            f"/api/forks/{fork_id}/publish",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"title": "Mine", "category": "motion", "version": "0.1.0"}).encode(),
        )
        assert published.json()["listing"]["publisher"]["uid"] == "local"
        archive = call(
            local,
            "GET",
            f"/api/bundles/{published.json()['listing']['bundle']['path']}/archive",
            query=f"token={TOKEN}",
        )
        assert archive.status == 200 and archive.headers["content-type"] == "application/zip"
        assert api(local, "DELETE", f"/api/forks/{fork_id}").status == 200

    def test_errors_map_to_statuses_without_leaking_paths(self, local: PlatformApp, tmp_path: Path):
        missing = api(local, "GET", "/api/catalogue/lab/nothing/docs")
        assert missing.status == 404 and missing.json()["error"]["kind"] == "NotFound"
        assert str(tmp_path) not in missing.body.decode()
        assert api(local, "POST", "/api/forks", body=b"{}").status == 400  # not JSON content type
        assert api(local, "PUT", "/api/forks").status == 405
        assert api(local, "GET", "/api/nothing").status == 404
        too_big = api(
            local,
            "POST",
            "/api/forks/x/revisions",
            headers={"Content-Type": "application/zip"},
            body=b"0" * 60_000,
            query="message=m",
        )
        assert too_big.status == 413

    def test_an_unexpected_error_is_a_generic_500(self, local: PlatformApp, monkeypatch):
        def boom(*a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret detail /home/someone")

        monkeypatch.setattr(local.service, "catalogue", boom)
        response = api(local, "GET", "/api/catalogue")
        assert response.status == 500 and b"secret" not in response.body


PASSWORD = "correct horse battery"


@pytest.fixture()
def server(tmp_path: Path, store: FileStore, monkeypatch) -> tuple[PlatformApp, Accounts]:
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})  # fast in tests
    accounts = Accounts(store.root / "accounts.sqlite3")
    accounts.add_user("ada", "Ada", PASSWORD)
    accounts.add_user("ben", "Ben", PASSWORD)
    config = validate(ServerConfig(store=store.root, host="127.0.0.1", port=8000))
    return PlatformApp(
        PlatformService(store), ServerGate(config, accounts), web_dir=web_dir(tmp_path)
    ), accounts


def login(app: PlatformApp, user: str, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return call(
        app,
        "POST",
        "/api/login",
        headers={
            "Content-Type": "application/json",
            "X-Alhazen-Platform": "1",
            "Origin": "http://127.0.0.1:8000",
        },
        body=json.dumps({"username": user, "password": password}).encode(),
    )


def as_user(response) -> dict[str, str]:  # type: ignore[no-untyped-def]
    cookie = response.headers["set-cookie"].split(";")[0]
    return {"Cookie": cookie, "X-Alhazen-CSRF": response.json()["csrf"]}


class TestServerHost:
    def test_sign_in_sets_a_strict_session_cookie(self, server):
        app, _ = server
        response = login(app, "ada")
        assert response.status == 200 and response.json()["viewer"]["name"] == "Ada"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        session = call(app, "GET", "/api/session", headers=as_user(response)).json()
        assert session["viewer"]["name"] == "Ada" and session["csrf"]

    def test_failures_say_the_same_thing_and_are_throttled(self, server):
        app, _ = server
        wrong = login(app, "ada", "wrong password!!")
        nobody = login(app, "nobody", "wrong password!!")
        assert wrong.status == nobody.status == 401
        assert wrong.json()["error"]["message"] == nobody.json()["error"]["message"]
        for _ in range(5):
            login(app, "ada", "wrong password!!")
        assert "Too many" in login(app, "ada").json()["error"]["message"]

    def test_the_catalogue_is_private_until_signed_in(self, server):
        app, _ = server
        assert call(app, "GET", "/api/catalogue").status == 403
        assert call(app, "GET", "/api/session").json()["viewer"] is None

    def test_changes_need_the_csrf_token_and_the_page_header(self, server):
        app, _ = server
        headers = as_user(login(app, "ada"))
        body = json.dumps({"publisher": "lab", "exp": "dots"}).encode()
        no_csrf = call(
            app,
            "POST",
            "/api/forks",
            body=body,
            headers={"Cookie": headers["Cookie"], "Content-Type": "application/json"},
        )
        assert no_csrf.status == 403
        assert (
            call(
                app,
                "POST",
                "/api/forks",
                body=body,
                headers={**headers, "Content-Type": "application/json"},
            ).status
            == 200
        )
        no_header = call(
            app,
            "POST",
            "/api/login",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"username": "ada", "password": PASSWORD}).encode(),
        )
        assert no_header.status == 403

    def test_two_users_never_see_each_others_copies(self, server):
        app, _ = server
        ada, ben = as_user(login(app, "ada")), as_user(login(app, "ben"))
        created = call(
            app,
            "POST",
            "/api/forks",
            headers={**ada, "Content-Type": "application/json"},
            body=json.dumps({"publisher": "lab", "exp": "dots"}).encode(),
        ).json()
        fork_id, handle = created["id"], created["fork"]["head"]["path"]
        assert call(app, "GET", "/api/forks", headers=ben).json() == {"forks": {}}
        for method, path in [
            ("GET", f"/api/forks/{fork_id}/docs"),
            ("GET", f"/api/bundles/{handle}/meta"),
            ("GET", f"/api/bundles/{handle}/archive"),
            ("DELETE", f"/api/forks/{fork_id}"),
        ]:
            assert call(app, method, path, headers=ben).status == 404, path
        assert fork_id in call(app, "GET", "/api/forks", headers=ada).json()["forks"]

    def test_signing_out_ends_the_session(self, server):
        app, _ = server
        headers = as_user(login(app, "ada"))
        assert call(app, "POST", "/api/logout", headers=headers).status == 200
        assert call(app, "GET", "/api/catalogue", headers=headers).status == 403

    def test_a_disabled_user_loses_their_sessions(self, server):
        app, accounts = server
        headers = as_user(login(app, "ben"))
        accounts.set_disabled("ben", True)
        assert call(app, "GET", "/api/session", headers=headers).json()["viewer"] is None
        assert login(app, "ben").status == 401

    def test_a_foreign_host_is_refused(self, server):
        app, _ = server
        assert call(app, "GET", "/", host="rebound.example:8000").status == 403


class TestServerConfig:
    def test_a_network_address_without_tls_is_refused(self, tmp_path: Path):
        with pytest.raises(InvalidRequest, match="tls_terminated_by_proxy"):
            validate(ServerConfig(store=tmp_path, host="0.0.0.0", public_url="https://lab.example"))
        with pytest.raises(InvalidRequest, match="https"):
            validate(
                ServerConfig(
                    store=tmp_path,
                    host="0.0.0.0",
                    tls_terminated_by_proxy=True,
                    public_url="http://lab.example",
                )
            )
        ok = validate(
            ServerConfig(
                store=tmp_path,
                host="0.0.0.0",
                tls_terminated_by_proxy=True,
                public_url="https://lab.example/platform",
            )
        )
        assert ok.secure and ok.prefix == "/platform"

    def test_unknown_settings_are_refused(self, tmp_path: Path):
        path = tmp_path / "platform.toml"
        path.write_text('[server]\nstore = "s"\nport = 8770\nallow_everything = true\n')
        with pytest.raises(InvalidRequest, match="allow_everything"):
            load_config(path)

    def test_a_relative_store_is_relative_to_the_file(self, tmp_path: Path):
        path = tmp_path / "platform.toml"
        path.write_text('[server]\nstore = "data"\n')
        assert load_config(path).store == (tmp_path / "data").resolve()


class TestAccounts:
    def test_passwords_are_hashed_and_rules_apply(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
        accounts = Accounts(tmp_path / "a.sqlite3")
        accounts.add_user("ada", "Ada", PASSWORD)
        raw = (tmp_path / "a.sqlite3").read_bytes()
        assert PASSWORD.encode() not in raw
        with pytest.raises(InvalidRequest, match="12"):
            accounts.add_user("bob", "Bob", "short")
        with pytest.raises(InvalidRequest, match="User names"):
            accounts.add_user("Bad Name", "x", PASSWORD)
        token, csrf, principal = accounts.login("ADA", PASSWORD)
        assert accounts.session(token) == (principal, csrf)
        accounts.set_password("ada", "another long password")
        assert accounts.session(token) is None
        assert [u["username"] for u in accounts.users()] == ["ada"]


def _built(store: FileStore, monkeypatch, *, public: bool) -> PlatformApp:
    monkeypatch.setattr(accounts_module, "_SCRYPT", {"n": 2**10, "r": 8, "p": 1})
    Accounts(store.root / "accounts.sqlite3").add_user("ada", "Ada", PASSWORD)
    return build_app(
        validate(
            ServerConfig(store=store.root, host="127.0.0.1", port=8000, public_catalogue=public)
        )
    )


class TestPublicCatalogue:
    """``public_catalogue = true``: anyone can read the catalogue, its documentation and its
    published files; copies and every change still need an account."""

    def test_a_visitor_reads_the_catalogue_its_documentation_and_files(self, store, monkeypatch):
        app = _built(store, monkeypatch, public=True)
        session = call(app, "GET", "/api/session").json()
        assert session["viewer"] is None and session["publicCatalogue"] is True
        assert not any(session["capabilities"][k] for k in ("fork", "publish", "uploadRevision"))
        listing = call(app, "GET", "/api/catalogue").json()["published"]["lab"]["dots"]
        handle = listing["bundle"]["path"]
        assert call(app, "GET", "/api/catalogue/lab/dots/docs").status == 200
        assert call(app, "GET", f"/api/bundles/{handle}/meta").status == 200
        assert call(app, "GET", f"/api/bundles/{handle}/chunks/0").status == 200
        assert call(app, "GET", f"/api/bundles/{handle}/archive").status == 200
        assert call(app, "GET", "/api/forks").json() == {"forks": {}}

    def test_a_visitor_cannot_copy_or_change_anything(self, store, monkeypatch):
        app = _built(store, monkeypatch, public=True)
        json_headers = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8000"}
        fork = call(
            app,
            "POST",
            "/api/forks",
            headers=json_headers,
            body=json.dumps({"publisher": "lab", "exp": "dots"}).encode(),
        )
        assert fork.status == 403 and "Sign in" in fork.json()["error"]["message"]
        for method, path in [
            ("POST", "/api/forks/x/publish"),
            ("POST", "/api/forks/x/unpublish"),
            ("DELETE", "/api/forks/x"),
            ("GET", "/api/forks/x/docs"),
        ]:
            assert call(app, method, path, headers=json_headers, body=b"{}").status == 403, path
        upload = call(
            app,
            "POST",
            "/api/forks/x/revisions",
            headers={"Content-Type": "application/zip"},
            body=zip_of(experiment()),
            query="message=m",
        )
        assert upload.status == 403
        # a signed-in user still copies as before
        user = as_user(login(app, "ada"))
        made = call(
            app,
            "POST",
            "/api/forks",
            headers={**user, "Content-Type": "application/json"},
            body=json.dumps({"publisher": "lab", "exp": "dots"}).encode(),
        )
        assert made.status == 200

    def test_without_the_setting_the_catalogue_stays_private(self, store, monkeypatch):
        app = _built(store, monkeypatch, public=False)
        assert call(app, "GET", "/api/session").json()["publicCatalogue"] is False
        assert call(app, "GET", "/api/catalogue").status == 403
        assert call(app, "GET", "/api/catalogue/lab/dots/docs").status == 403
        listing_handle = "L~lab~dots~" + store.listing("lab", "dots")["bundle"]["id"]
        assert call(app, "GET", f"/api/bundles/{listing_handle}/meta").status == 403
