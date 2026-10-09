"""Hub service accounts: invites, sign-in, sessions, CSRF/Origin, throttles
and operator recovery (review gate M1). Synthetic users only."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.hub.server_support import (
    API,
    ORIGIN,
    PASSWORD,
    Hub,
)

from alhazen.hub import admin
from alhazen.hub.app import cookie_name
from alhazen.hub.database import Database
from alhazen.hub.schema import audit_events
from alhazen.hub.settings import AuthPolicy


def login(hub, username="ada", password=PASSWORD, origin=ORIGIN, **kwargs):
    headers = {"Origin": origin} if origin is not None else {}
    return hub.client.post(
        f"{API}/auth/login",
        json={"username": username, "password": password},
        headers=headers,
        **kwargs,
    )


class TestConfig:
    def test_config_names_role_and_limits_without_private_values(self, hub):
        body = hub.client.get(f"{API}/config").json()
        assert body["role"] == "server" and body["api_version"] == 1
        assert body["registration_mode"] == "invite"
        assert body["limits"]["max_chunk_bytes"] == 8 * 1024 * 1024
        text = str(body)
        assert "sqlite" not in text and str(hub.settings.artifact_root) not in text

    def test_health_and_api_headers(self, hub):
        response = hub.client.get(f"{API}/healthz")
        assert response.json() == {"status": "ok"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_unknown_route_uses_the_error_shape(self, hub):
        response = hub.client.get(f"{API}/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestRegistration:
    def test_invite_registers_once_and_never_twice(self, hub):
        code = hub.invite()
        body = {"username": "Ada", "display_name": "Ada", "password": PASSWORD, "invite_code": code}
        first = hub.client.post(f"{API}/auth/register", json=body, headers={"Origin": ORIGIN})
        assert first.status_code == 201
        assert first.json()["user"]["username"] == "ada"
        assert "set-cookie" not in first.headers  # sign in is a separate step
        again = hub.client.post(
            f"{API}/auth/register", json={**body, "username": "bob"}, headers={"Origin": ORIGIN}
        )
        assert again.status_code == 400 and again.json()["error"]["code"] == "invalid_invite"

    @pytest.mark.parametrize("code", ["", "inv-nonsense-code", None])
    def test_bad_invites_are_refused_generically(self, hub, code):
        response = hub.client.post(
            f"{API}/auth/register",
            json={
                "username": "ada",
                "display_name": "Ada",
                "password": PASSWORD,
                "invite_code": code,
            },
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_invite"

    def test_expired_and_revoked_invites_fail(self, hub, settings):
        expired = admin.create_invite(settings, actor="op", expires_days=1)
        revoked = admin.create_invite(settings, actor="op")
        assert admin.revoke_invite(settings, revoked.id, actor="op")
        hub.clock.advance(2 * 86400)
        for invite, name in ((expired, "ada"), (revoked, "bob")):
            response = hub.client.post(
                f"{API}/auth/register",
                json={
                    "username": name,
                    "display_name": "X",
                    "password": PASSWORD,
                    "invite_code": invite.code,
                },
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 400, response.text

    def test_username_taken_keeps_the_invite_unused(self, hub, settings):
        hub.register("ada")
        code = hub.invite()
        body = {"username": "ADA", "display_name": "A", "password": PASSWORD, "invite_code": code}
        taken = hub.client.post(f"{API}/auth/register", json=body, headers={"Origin": ORIGIN})
        assert taken.status_code == 409 and taken.json()["error"]["code"] == "username_taken"
        ok = hub.client.post(
            f"{API}/auth/register", json={**body, "username": "bob"}, headers={"Origin": ORIGIN}
        )
        assert ok.status_code == 201

    @pytest.mark.parametrize(
        "field, value",
        [
            ("username", "a"),
            ("username", "has space"),
            ("password", "short"),
            ("password", "x" * 1025),
            ("display_name", ""),
            ("display_name", "tab\tname"),
        ],
    )
    def test_field_rules(self, hub, field, value):
        body = {
            "username": "ada",
            "display_name": "Ada",
            "password": PASSWORD,
            "invite_code": hub.invite(),
        }
        body[field] = value
        response = hub.client.post(f"{API}/auth/register", json=body, headers={"Origin": ORIGIN})
        assert response.status_code == 400

    @pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
    def test_register_requires_the_exact_origin(self, hub, origin):
        headers = {"Origin": origin} if origin else {}
        response = hub.client.post(
            f"{API}/auth/register",
            json={
                "username": "ada",
                "display_name": "Ada",
                "password": PASSWORD,
                "invite_code": hub.invite(),
            },
            headers=headers,
        )
        assert response.status_code == 403

    def test_json_only(self, hub):
        response = hub.client.post(
            f"{API}/auth/register",
            content=b"username=ada",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 415


class TestBrowserSessions:
    def test_login_sets_a_strict_http_only_cookie_and_csrf(self, hub):
        hub.register("ada")
        response = login(hub)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("alhazen_hub=")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            assert flag in cookie
        assert "Domain" not in cookie and "Secure" not in cookie  # loopback development only
        assert response.json()["csrf_token"]
        me = hub.client.get(f"{API}/auth/me")
        assert me.json()["user"]["username"] == "ada"
        assert me.json()["csrf_token"] == response.json()["csrf_token"]

    def test_https_deployments_use_a_host_only_secure_cookie(self, settings):
        secure = replace(settings, public_origin="https://hub.example.org")
        assert secure.secure_cookies and cookie_name(secure) == "__Host-alhazen_hub"

    @pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
    def test_login_needs_the_exact_origin(self, hub, origin):
        hub.register("ada")
        assert login(hub, origin=origin).status_code == 403

    def test_wrong_password_and_unknown_user_answer_alike(self, hub):
        hub.register("ada")
        wrong = login(hub, password="not the password")
        unknown = login(hub, username="nobody")
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_cookie_writes_need_origin_and_csrf(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        body = {"title": "X"}
        no_token = page.client.post(f"{API}/experiments", json=body, headers={"Origin": ORIGIN})
        assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "csrf_failed"
        no_origin = page.client.post(
            f"{API}/experiments", json=body, headers={"X-CSRF-Token": page.csrf}
        )
        assert no_origin.status_code == 403
        bad_origin = page.client.post(
            f"{API}/experiments",
            json=body,
            headers={"X-CSRF-Token": page.csrf, "Origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403
        assert page.post("/experiments", body).status_code == 201

    def test_logout_revokes_the_cookie(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        stolen = page.client.cookies.get("alhazen_hub")
        assert page.post("/auth/logout").status_code == 200
        other = hub.client
        other.cookies.set("alhazen_hub", stolen)
        assert other.get(f"{API}/auth/me").status_code == 401

    def test_idle_and_absolute_expiry(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        hub.clock.advance(59 * 60)
        assert page.get("/auth/me").status_code == 200
        hub.clock.advance(61 * 60)
        assert page.get("/auth/me").status_code == 401
        page = hub.browser("ada")
        for _ in range(14):  # used every 55 minutes (770 min), still ends at 12 h
            hub.clock.advance(55 * 60)
            last = page.get("/auth/me")
        assert last.status_code == 401

    def test_new_login_rotates_and_revokes_the_previous_cookie(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        old = page.client.cookies.get("alhazen_hub")
        page.client.post(
            f"{API}/auth/login",
            json={"username": "ada", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert page.client.cookies.get("alhazen_hub") != old
        hub.client.cookies.set("alhazen_hub", old)
        assert hub.client.get(f"{API}/auth/me").status_code == 401


class TestBearer:
    def test_token_route_issues_a_bearer_without_a_cookie(self, hub):
        hub.register("ada")
        response = hub.client.post(
            f"{API}/auth/token", json={"username": "ada", "password": PASSWORD}
        )
        assert response.status_code == 200 and "set-cookie" not in response.headers
        token = response.json()["access_token"]
        me = hub.client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["user"]["username"] == "ada" and me.json()["csrf_token"] is None

    def test_bearer_writes_need_no_csrf(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        assert rig.post("/experiments", {"title": "X"}).status_code == 201

    def test_cookie_and_bearer_together_are_refused(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        rig = hub.bearer("ada")
        response = page.client.get(f"{API}/auth/me", headers=rig.headers())
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ambiguous_credentials"
        token = page.client.post(
            f"{API}/auth/token", json={"username": "ada", "password": PASSWORD}
        )
        assert token.status_code == 400

    def test_a_bearer_is_not_a_cookie_and_back(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        hub.client.cookies.set("alhazen_hub", rig.token)
        assert hub.client.get(f"{API}/auth/me").status_code == 401
        page = hub.browser("ada")
        raw = page.client.cookies.get("alhazen_hub")
        fresh = TestClient(hub.app, base_url=ORIGIN)
        response = fresh.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {raw}"})
        assert response.status_code == 401

    def test_logout_revokes_the_bearer(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        assert rig.post("/auth/logout").status_code == 200
        assert rig.get("/auth/me").status_code == 401

    def test_bearer_lifetime(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        hub.clock.advance(11.9 * 3600)
        assert rig.get("/auth/me").status_code == 200
        hub.clock.advance(0.2 * 3600)
        assert rig.get("/auth/me").status_code == 401


class TestThrottles:
    def test_account_failures_throttle_then_recover(self, hub):
        hub.register("ada")
        for _ in range(5):
            assert login(hub, password="wrong password!").status_code == 401
        blocked = login(hub)
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
        hub.clock.advance(15 * 60 + 1)
        assert login(hub).status_code == 200

    def test_address_failures_throttle_across_accounts(self, hub):
        for n in range(20):
            login(hub, username=f"nobody{n}", password="wrong password!")
        assert login(hub, username="someone-else", password="wrong password!").status_code == 429

    def test_global_hash_admission(self, tmp_path, clock):
        from tests.hub.server_support import make_settings

        tight = replace(make_settings(tmp_path), auth=AuthPolicy(hashes_per_minute=3))
        admin.init_database(tight)
        service = Hub(tight, clock)
        service.register("ada")  # one hash
        assert login(service).status_code == 200
        assert login(service).status_code == 200
        assert login(service).status_code == 429
        clock.advance(61)
        assert login(service).status_code == 200


class TestOperatorRecovery:
    def test_reset_password_revokes_every_session_and_is_audited(self, hub, settings):
        hub.register("ada")
        page = hub.browser("ada")
        rig = hub.bearer("ada")
        assert admin.reset_password(settings, "ada", "a brand new passphrase", actor="op") == 2
        assert page.get("/auth/me").status_code == 401
        assert rig.get("/auth/me").status_code == 401
        assert login(hub).status_code == 401
        assert login(hub, password="a brand new passphrase").status_code == 200
        db = Database(settings)
        with db.engine.connect() as conn:
            actions = [r.action for r in conn.execute(select(audit_events)).all()]
        db.dispose()
        assert "user.reset_password" in actions and "invite.create" in actions

    def test_disable_and_enable(self, hub, settings):
        hub.register("ada")
        rig = hub.bearer("ada")
        admin.disable_user(settings, "ada", actor="op")
        assert rig.get("/auth/me").status_code == 401
        assert login(hub).status_code == 401
        admin.enable_user(settings, "ada", actor="op")
        assert login(hub).status_code == 200

    def test_admin_refusals(self, settings):
        with pytest.raises(admin.AdminError):
            admin.reset_password(settings, "nobody", "a brand new passphrase", actor="op")
        with pytest.raises(admin.AdminError):
            admin.create_invite(settings, actor="")
        with pytest.raises(admin.AdminError):
            admin.reset_password(settings, "nobody", "short", actor="op")
