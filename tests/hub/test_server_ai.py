"""AI-assisted authoring on the hub service: keys, drafts, jobs, disclosure,
acceptance, limits and failure codes, with a scripted provider and a
minimal authoring kit (tests/hub/ai_fakes.py). SQLite by default; set
ALHAZEN_HUB_TEST_POSTGRES_URL to run the same tests on PostgreSQL. No
network, no real provider.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text

from alhazen.hub import admin
from alhazen.hub.ai import keys as ai_keys_module
from alhazen.hub.ai.jobs import claim_next
from alhazen.hub.ai.providers import ProviderError
from alhazen.hub.app import create_app
from alhazen.hub.schema import SCHEMA_VERSION, SchemaError, ai_jobs, ai_keys, audit_events
from alhazen.hub.settings import AISettings, HubSettings
from tests.hub import ai_fakes
from tests.hub.server_support import Hub, make_bundle, make_settings

SECRET = base64.b64encode(b"k" * 32).decode()
OPENAI_KEY = "sk-test-0123456789abcdefWXYZ"


def ai_settings(tmp_path: Path, **ai: Any) -> HubSettings:
    settings = make_settings(tmp_path)
    settings = replace(settings, ai=AISettings(key_secret=SECRET, **ai))
    admin.init_database(settings)
    return settings


class AIHub(Hub):
    def __init__(self, settings: HubSettings, clock: Any, provider: ai_fakes.ScriptedProvider):
        super().__init__(settings, clock)
        self.provider = provider
        runner = self.app.state.ai_worker.runner
        runner.kit = ai_fakes.kit()
        runner.client_factory = provider.factory()

    @property
    def worker(self) -> Any:
        return self.app.state.ai_worker

    @property
    def service(self) -> Any:
        return self.app.state.hub

    def drain(self) -> int:
        return int(self.worker.drain())


@pytest.fixture
def provider() -> ai_fakes.ScriptedProvider:
    return ai_fakes.ScriptedProvider()


@pytest.fixture
def ai(tmp_path: Path, clock: Any, provider: ai_fakes.ScriptedProvider) -> AIHub:
    return AIHub(ai_settings(tmp_path), clock, provider)


def signed_in(ai: AIHub, name: str = "ada", key: bool = True) -> Any:
    ai.register(name)
    who = ai.browser(name)
    if key:
        r = who.put("/ai/keys/openai", json={"key": OPENAI_KEY})
        assert r.status_code == 200, r.text
    return who


def new_draft(who: Any, **body: Any) -> dict[str, Any]:
    r = who.post("/ai/drafts", {"prompt": "A gap saccade task", "provider": "openai", **body})
    assert r.status_code == 202, r.text
    return r.json()


def draft(who: Any, draft_id: str) -> dict[str, Any]:
    r = who.get(f"/ai/drafts/{draft_id}")
    assert r.status_code == 200, r.text
    return r.json()


def job_of(view: dict[str, Any], kind: str) -> dict[str, Any]:
    """The draft's current job of a kind (by its pointer, not list order:
    the fake clock gives jobs equal creation times)."""
    wanted = view["draft"]["plan_job_id" if kind == "plan" else "source_job_id"]
    return next(j for j in view["jobs"] if j["id"] == wanted)


def to_generated(ai: AIHub, who: Any) -> str:
    ai.provider.script = [ai_fakes.plan_text(), ai_fakes.source_text()]
    draft_id = new_draft(who)["draft"]["id"]
    ai.drain()
    r = who.post(f"/ai/drafts/{draft_id}/generate", {})
    assert r.status_code == 202, r.text
    ai.drain()
    assert draft(who, draft_id)["draft"]["status"] == "generated"
    return draft_id


# -- status and keys ------------------------------------------------------------


class TestStatusAndKeys:
    def test_disabled_without_a_wrapping_key(self, hub: Hub) -> None:
        hub.register("ada")
        ada = hub.browser("ada")
        status = ada.get("/ai/status").json()
        assert status["enabled"] is False
        assert {p["id"] for p in status["providers"]} == {
            "openai",
            "anthropic",
            "google",
            "openrouter",
        }
        r = ada.put("/ai/keys/openai", json={"key": OPENAI_KEY})
        assert r.status_code == 403 and r.json()["error"]["code"] == "ai_disabled"
        r = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "ai_disabled"

    def test_key_is_encrypted_never_returned_never_logged(
        self, ai: AIHub, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        ada = signed_in(ai, key=False)
        r = ada.put("/ai/keys/openai", json={"key": OPENAI_KEY})
        assert r.status_code == 200
        body = r.json()
        assert body["provider"] == "openai" and body["hint"] == "WXYZ" and body["set"] is True
        assert OPENAI_KEY not in r.text
        status = ada.get("/ai/status")
        assert OPENAI_KEY not in status.text
        assert status.json()["keys"][0]["hint"] == "WXYZ"
        with ai.service.db.transaction() as conn:
            row = conn.execute(select(ai_keys)).one()
            audit = " ".join(r.detail + r.action for r in conn.execute(select(audit_events)))
        assert OPENAI_KEY not in row.ciphertext and "WXYZ" not in row.ciphertext
        assert OPENAI_KEY not in audit and "ai.key.set" in audit
        assert OPENAI_KEY not in caplog.text
        # Decrypts only for its owner and provider.
        assert ai_keys_module.reveal(ai.service, row.user_id, "openai") == OPENAI_KEY

    def test_rotate_and_delete(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        first = ada.get("/ai/status").json()["keys"][0]
        ai.clock.advance(5)
        r = ada.put("/ai/keys/openai", json={"key": "sk-other-key-000000001234"})
        assert r.json()["hint"] == "1234" and r.json()["rotated_at"] != first["rotated_at"]
        assert r.json()["created_at"] == first["created_at"]
        assert ada.client.delete("/api/hub/v1/ai/keys/openai", headers=ada.headers()).status_code == 204
        assert ada.client.delete("/api/hub/v1/ai/keys/openai", headers=ada.headers()).status_code == 404
        assert ada.get("/ai/status").json()["keys"] == []

    def test_key_shape_and_provider_checks(self, ai: AIHub) -> None:
        ada = signed_in(ai, key=False)
        for bad in ("short", "has a space in it 1234567", 12345, ""):
            r = ada.put("/ai/keys/openai", json={"key": bad})
            assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_key"
            assert str(bad) not in r.json()["error"]["message"] or bad == ""
        r = ada.put("/ai/keys/mistral", json={"key": OPENAI_KEY})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_provider"

    def test_verify_refused_key_is_not_stored(self, ai: AIHub) -> None:
        ada = signed_in(ai, key=False)
        ai.provider.verify_error = ProviderError("auth", "refused", status=401)
        r = ada.put("/ai/keys/openai", json={"key": OPENAI_KEY, "verify": True})
        assert r.status_code == 400 and r.json()["error"]["code"] == "key_rejected"
        assert ada.get("/ai/status").json()["keys"] == []
        ai.provider.verify_error = None
        r = ada.put("/ai/keys/openai", json={"key": OPENAI_KEY, "verify": True})
        assert r.status_code == 200 and r.json()["verified"] is True

    def test_cookie_write_needs_csrf(self, ai: AIHub) -> None:
        ada = signed_in(ai, key=False)
        r = ada.client.put(
            "/api/hub/v1/ai/keys/openai",
            json={"key": OPENAI_KEY},
            headers={"Origin": "http://127.0.0.1:8750"},
        )
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"

    def test_a_copied_ciphertext_does_not_decrypt_for_another_user(self, ai: AIHub) -> None:
        signed_in(ai, "ada")
        bob = signed_in(ai, "bob", key=False)
        with ai.service.db.transaction() as conn:
            row = conn.execute(select(ai_keys)).one()
            conn.execute(
                ai_keys.insert().values(
                    user_id=bob.user["id"],
                    provider="openai",
                    ciphertext=row.ciphertext,
                    key_hint=row.key_hint,
                    created_at=row.created_at,
                    rotated_at=row.rotated_at,
                )
            )
        created = new_draft(bob)
        ai.drain()
        job = bob.get(f"/ai/jobs/{created['job']['id']}").json()["job"]
        assert job["status"] == "failed" and job["error"]["code"] == "key_unreadable"
        assert ai.provider.requests == []


# -- drafts and jobs ----------------------------------------------------------------


class TestDraftLifecycle:
    def test_plan_then_source_then_accept(self, ai: AIHub, tmp_path: Path) -> None:
        ada = signed_in(ai)
        created = new_draft(ada)
        assert created["draft"]["status"] == "planning" and created["job"]["status"] == "queued"
        assert ai.drain() == 1
        view = draft(ada, created["draft"]["id"])
        assert view["draft"]["status"] == "planned"
        assert view["plan"]["title"] == ai_fakes.PLAN["title"]
        job = view["jobs"][0]
        assert job["status"] == "done" and job["usage"] == {"input_tokens": 10, "output_tokens": 5}
        assert job["disclosed"]["start_from"] is None and job["disclosed"]["calls"] == 1
        assert job["disclosed"]["sent_bytes"] > 0
        assert ai.provider.keys == [OPENAI_KEY]

        ai.provider.script = [ai_fakes.source_text()]
        r = ada.post(
            f"/ai/drafts/{created['draft']['id']}/generate", {"plan_edits": {"title": "Gap task"}}
        )
        assert r.status_code == 202
        ai.drain()
        view = draft(ada, created["draft"]["id"])
        assert view["draft"]["status"] == "generated" and view["plan"]["title"] == "Gap task"
        result = job_of(view, "source")["result"]
        assert result["valid"] is True
        assert {f["path"] for f in result["files"]} == {"run.py", "configs/task.yaml", "README.md"}
        assert result["manifest"]["title"] == "Gap task"

        r = ada.post(
            f"/ai/drafts/{created['draft']['id']}/accept",
            {"title": "Gap task", "summary": "Mine", "license": "MIT"},
        )
        assert r.status_code == 200, r.text
        accepted = r.json()
        experiment, version = accepted["experiment"], accepted["version"]
        assert version["ai_assisted"] is True and version["ai_draft_id"] == created["draft"]["id"]
        assert experiment["title"] == "Gap task" and experiment["package_name"] == "gap-saccade"
        own = ada.get("/experiments").json()["items"]
        assert [e["id"] for e in own] == [experiment["id"]]
        detail = ada.get(f"/experiments/{experiment['id']}").json()
        assert detail["versions"][0]["ai_assisted"] is True
        download = ada.get(f"/experiments/{experiment['id']}/versions/{version['id']}/download")
        assert download.status_code == 200 and download.content[:2] == b"PK"
        assert ai.client.get("/api/hub/v1/catalog").json()["items"] == []
        ai.register("bob")
        bob = ai.browser("bob")
        assert bob.get(f"/experiments/{experiment['id']}").status_code == 404
        assert (
            bob.get(f"/experiments/{experiment['id']}/versions/{version['id']}/download").status_code
            == 404
        )
        assert bob.get(f"/ai/drafts/{created['draft']['id']}").status_code == 404
        again = ada.post(f"/ai/drafts/{created['draft']['id']}/accept", {"title": "Gap task"})
        assert again.status_code == 409 and again.json()["error"]["code"] == "already_accepted"
        assert draft(ada, created["draft"]["id"])["draft"]["status"] == "accepted"

    def test_drafts_are_private_to_their_owner(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        created = new_draft(ada)
        bob = signed_in(ai, "bob")
        assert bob.get("/ai/drafts").json()["items"] == []
        assert bob.get(f"/ai/jobs/{created['job']['id']}").status_code == 404
        assert bob.post(f"/ai/jobs/{created['job']['id']}/cancel").status_code == 404
        assert ada.get("/ai/drafts").json()["items"][0]["id"] == created["draft"]["id"]

    def test_key_required_and_validation(self, ai: AIHub) -> None:
        ada = signed_in(ai, key=False)
        r = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "key_required"
        ada.put("/ai/keys/openai", json={"key": OPENAI_KEY})
        r = ada.post("/ai/drafts", {"prompt": "x" * 8001, "provider": "openai"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "prompt_too_long"
        r = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai", "model": "../etc"})
        assert r.status_code == 400
        r = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai", "extra": 1})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_field"
        assert ai.provider.requests == []

    def test_invalid_plan_reports_and_draft_returns_to_describing(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        ai.provider.script = ["not json", "still not json"]
        created = new_draft(ada)
        ai.drain()
        view = draft(ada, created["draft"]["id"])
        job = view["jobs"][0]
        assert view["draft"]["status"] == "describing"
        assert job["error"]["code"] == "generation_invalid" and job["error"]["status"] == 422
        assert job["result"]["report"]["problems"] == ["answer is not a plan"]
        assert len(ai.provider.requests) == 2  # the repair round
        r = ada.post(f"/ai/drafts/{created['draft']['id']}/generate", {})
        assert r.status_code == 409 and r.json()["error"]["code"] == "plan_required"
        r = ada.post(f"/ai/drafts/{created['draft']['id']}/plan", {"prompt": "Try again"})
        assert r.status_code == 202
        ai.drain()
        assert draft(ada, created["draft"]["id"])["draft"]["status"] == "planned"

    def test_generated_source_that_is_not_a_package_is_refused(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        ai.provider.script = [
            ai_fakes.plan_text(),
            ai_fakes.source_text(files={"task.py": "x = 1\n"}),  # no run.py
        ]
        created = new_draft(ada)
        ai.drain()
        ada.post(f"/ai/drafts/{created['draft']['id']}/generate", {})
        ai.drain()
        view = draft(ada, created["draft"]["id"])
        job = job_of(view, "source")
        assert view["draft"]["status"] == "planned"
        assert job["error"]["code"] == "generation_invalid" and "run.py" in job["result"]["package_error"]
        r = ada.post(f"/ai/drafts/{created['draft']['id']}/accept", {"title": "x"})
        assert r.status_code == 409
        assert not list((ai.settings.artifact_root / "ai-drafts").rglob("*.zip"))

    @pytest.mark.parametrize(
        ("kind", "code", "status"),
        [
            ("quota", "provider_quota", 402),
            ("timeout", "provider_timeout", 504),
            ("auth", "key_rejected", 409),
            ("other", "provider_error", 502),
            ("invalid", "provider_error", 502),
        ],
    )
    def test_provider_failures_map_to_codes(
        self, ai: AIHub, kind: str, code: str, status: int
    ) -> None:
        ada = signed_in(ai)
        ai.provider.script = [ProviderError(kind, f"{kind} happened")]
        created = new_draft(ada)
        ai.drain()
        job = ada.get(f"/ai/jobs/{created['job']['id']}").json()["job"]
        assert job["status"] == "failed"
        assert job["error"] == {"code": code, "status": status, "message": f"{kind} happened"}
        assert job["disclosed"]["calls"] == 1

    def test_discard_removes_generated_packages_but_never_an_accepted_version(
        self, ai: AIHub
    ) -> None:
        ada = signed_in(ai)
        draft_id = to_generated(ai, ada)
        folder = ai.settings.artifact_root / "ai-drafts" / draft_id
        assert list(folder.glob("*.zip"))
        accepted = ada.post(f"/ai/drafts/{draft_id}/accept", {"title": "Kept"}).json()
        r = ada.client.delete(f"/api/hub/v1/ai/drafts/{draft_id}", headers=ada.headers())
        assert r.status_code == 204
        assert not folder.exists()
        assert ada.get("/ai/drafts").json()["items"] == []
        exp, ver = accepted["experiment"]["id"], accepted["version"]["id"]
        assert ada.get(f"/experiments/{exp}/versions/{ver}/download").status_code == 200

    def test_discarded_draft_starts_no_jobs(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        created = new_draft(ada)
        ada.client.delete(f"/api/hub/v1/ai/drafts/{created['draft']['id']}", headers=ada.headers())
        assert ai.drain() == 0
        job = ada.get(f"/ai/jobs/{created['job']['id']}").json()["job"]
        assert job["status"] == "cancelled"
        r = ada.post(f"/ai/drafts/{created['draft']['id']}/plan", {})
        assert r.status_code == 409 and r.json()["error"]["code"] == "draft_discarded"


class TestDisclosure:
    def test_start_from_own_version_sends_exactly_its_files(self, ai: AIHub, tmp_path: Path) -> None:
        ada = signed_in(ai)
        eid = ada.create_experiment()["id"]
        bundle = make_bundle(tmp_path, extra={"task.py": b"GAP = 200  # own source\n"})
        vid = ada.upload_version(eid, bundle).json()["version"]["id"]
        created = new_draft(ada, start_from={"experiment_id": eid, "version_id": vid})
        ai.drain()
        job = draft(ada, created["draft"]["id"])["jobs"][0]
        start = job["disclosed"]["start_from"]
        assert start["experiment_id"] == eid and start["version_id"] == vid
        paths = sorted(f["path"] for f in start["files"])
        assert paths == ["configs/task.yaml", "run.py", "task.py"]
        sent = ai.provider.sent_text()
        for path in paths:
            assert f"FILE {path}:" in sent
        assert "GAP = 200  # own source" in sent
        assert start["bytes"] == sum(f["bytes"] for f in start["files"])
        assert OPENAI_KEY not in sent

    def test_another_users_private_version_cannot_be_a_start(self, ai: AIHub, tmp_path: Path) -> None:
        bob = signed_in(ai, "bob")
        eid = bob.create_experiment()["id"]
        vid = bob.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        ada = signed_in(ai, "ada")
        r = ada.post(
            "/ai/drafts",
            {
                "prompt": "fork",
                "provider": "openai",
                "start_from": {"experiment_id": eid, "version_id": vid},
            },
        )
        assert r.status_code == 404
        assert ai.provider.requests == []

    def test_a_published_version_can_start_until_it_is_unpublished(
        self, ai: AIHub, tmp_path: Path
    ) -> None:
        bob = signed_in(ai, "bob")
        eid = bob.create_experiment()["id"]
        vid = bob.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        assert bob.publish(eid, vid).status_code == 200
        ada = signed_in(ai, "ada")
        start = {"experiment_id": eid, "version_id": vid}
        new_draft(ada, start_from=start)
        ai.drain()
        assert "FILE run.py:" in ai.provider.sent_text()
        ai.provider.requests.clear()
        queued = new_draft(ada, start_from=start)
        assert bob.post(f"/experiments/{eid}/unpublish").status_code == 200
        ai.drain()
        job = ada.get(f"/ai/jobs/{queued['job']['id']}").json()["job"]
        assert job["error"]["code"] == "start_unavailable"
        assert ai.provider.requests == []


class TestJobsMechanics:
    def test_cancel_a_queued_job(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        created = new_draft(ada)
        r = ada.post(f"/ai/jobs/{created['job']['id']}/cancel")
        assert r.status_code == 200 and r.json()["job"]["status"] == "cancelled"
        assert ai.drain() == 0
        assert draft(ada, created["draft"]["id"])["draft"]["status"] == "describing"
        assert ai.provider.requests == []
        assert ada.post(f"/ai/jobs/{created['job']['id']}/cancel").status_code == 200

    def test_cancel_while_running_records_nothing_and_stops_further_calls(
        self, ai: AIHub
    ) -> None:
        ada = signed_in(ai)
        holder: dict[str, str] = {}

        def cancel_then_answer() -> str:
            ada.post(f"/ai/jobs/{holder['job']}/cancel")
            return "not json"  # forces the kit's repair call, which must not happen

        ai.provider.script = [cancel_then_answer, ai_fakes.plan_text()]
        created = new_draft(ada)
        holder["job"] = created["job"]["id"]
        ai.drain()
        job = ada.get(f"/ai/jobs/{holder['job']}").json()["job"]
        assert job["status"] == "cancelled" and job["result"] is None
        assert len(ai.provider.requests) == 1
        assert draft(ada, created["draft"]["id"])["draft"]["status"] == "describing"

    def test_a_lapsed_lease_is_retaken_once_then_failed(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        created = new_draft(ada)
        first = claim_next(ai.service)
        assert first is not None and first[0] == created["job"]["id"]
        assert claim_next(ai.service) is None  # leased
        ai.clock.advance(ai.settings.ai.job_lease_seconds + 1)
        second = claim_next(ai.service)
        assert second is not None and second[1] != first[1]
        # The first holder can no longer record anything.
        assert ai.worker.runner.run(*first) == "superseded"
        ai.clock.advance(ai.settings.ai.job_lease_seconds + 1)
        assert claim_next(ai.service) is None
        job = ada.get(f"/ai/jobs/{created['job']['id']}").json()["job"]
        assert job["status"] == "failed" and job["error"]["code"] == "lease_lost"
        assert job["attempts"] == 2

    def test_active_and_daily_limits(self, tmp_path: Path, clock: Any) -> None:
        provider = ai_fakes.ScriptedProvider()
        ai = AIHub(
            ai_settings(tmp_path, max_active_jobs_per_user=2, max_jobs_per_day=3), clock, provider
        )
        ada = signed_in(ai)
        new_draft(ada)
        new_draft(ada)
        r = ada.post("/ai/drafts", {"prompt": "third", "provider": "openai"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "ai_jobs_busy"
        assert "Retry-After" in r.headers
        ai.drain()
        new_draft(ada)
        ai.drain()
        r = ada.post("/ai/drafts", {"prompt": "fourth", "provider": "openai"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "ai_daily_limit"
        clock.advance(24 * 3600 + 1)
        ada = ai.browser("ada")  # the browser session idled out meanwhile
        new_draft(ada)
        # Another user has their own allowance.
        bob = signed_in(ai, "bob")
        new_draft(bob)


class TestLibraryRemove:
    def test_remove_unpins_only(self, ai: AIHub, tmp_path: Path) -> None:
        bob = signed_in(ai, "bob", key=False)
        eid = bob.create_experiment()["id"]
        vid = bob.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        bob.publish(eid, vid)
        ada = signed_in(ai, "ada", key=False)
        assert ada.post("/library", {"experiment_id": eid, "version_id": vid}).status_code == 200
        assert len(ada.get("/library").json()["items"]) == 1
        r = ada.client.delete(f"/api/hub/v1/library/{eid}", headers=ada.headers())
        assert r.status_code == 204
        assert ada.get("/library").json()["items"] == []
        assert ada.client.delete(f"/api/hub/v1/library/{eid}", headers=ada.headers()).status_code == 204
        assert ai.client.get("/api/hub/v1/catalog").json()["items"][0]["experiment"]["id"] == eid
        r = ada.client.delete(f"/api/hub/v1/library/{eid}", headers={"Origin": "http://127.0.0.1:8750"})
        assert r.status_code == 403


class TestSchemaThree:
    def test_a_schema_two_database_migrates(self, tmp_path: Path, clock: Any) -> None:
        settings = make_settings(tmp_path)
        admin.init_database(settings)
        service = Hub(settings, clock)
        service.register("ada")
        ada = service.browser("ada")
        eid = ada.create_experiment()["id"]
        vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
        service.app.state.hub.db.dispose()
        engine = create_engine(settings.database_url.replace("postgresql://", "postgresql+psycopg://"))
        with engine.begin() as conn:  # make it a schema-2 database again
            for table in ("hub_ai_jobs", "hub_ai_drafts", "hub_ai_keys"):
                conn.exec_driver_sql(f"DROP TABLE {table}")
            conn.exec_driver_sql("ALTER TABLE hub_versions DROP COLUMN ai_draft_id")
            conn.execute(text("UPDATE hub_schema SET value = '2' WHERE key = 'schema_version'"))
        engine.dispose()
        with pytest.raises(SchemaError, match="migrate"):
            create_app(settings)
        assert admin.migrate_database(settings) == SCHEMA_VERSION == 3
        service = Hub(settings, clock)
        ada = service.browser("ada")
        versions = ada.get(f"/experiments/{eid}").json()["versions"]
        assert versions[0]["id"] == vid and versions[0]["ai_assisted"] is False
        with service.app.state.hub.db.transaction() as conn:
            assert conn.execute(select(ai_jobs)).all() == []


class TestSettings:
    def _config(self, tmp_path: Path, ai_block: str) -> Path:
        config = tmp_path / "hub.toml"
        config.write_text(
            '[server]\npublic_origin = "http://127.0.0.1:8750"\n'
            '[database]\nurl = "sqlite:///hub.sqlite3"\n[storage]\nartifact_root = "art"\n'
            + ai_block,
            encoding="utf-8",
        )
        return config

    def test_ai_section(self, tmp_path: Path) -> None:
        from alhazen.hub.settings import SettingsError, load_settings

        plain = load_settings(self._config(tmp_path, ""))
        assert plain.ai.enabled is False
        config = self._config(
            tmp_path,
            '[ai]\nkey_secret_env = "HUB_AI"\nmax_jobs_per_day = 5\n'
            '[ai.providers.openai]\nbase_url = "http://127.0.0.1:9/v1"\n'
            'models = ["m1", "m2"]\ndefault_model = "m2"\n',
        )
        settings = load_settings(config, environ={"HUB_AI": SECRET})
        assert settings.ai.enabled and settings.ai.max_jobs_per_day == 5
        assert settings.ai.providers["openai"].default_model == "m2"
        assert SECRET not in repr(settings)
        for block, message in (
            ('[ai]\nkey_secret = "short"\n', "32 random bytes"),
            ('[ai]\nkey_secret_env = "MISSING"\n', "not set"),
            ('[ai]\nmystery = 1\n', "unknown key"),
            ('[ai.providers.acme]\nbase_url = "https://x.org"\n', "unknown AI provider"),
            ('[ai.providers.openai]\nbase_url = "http://example.org/v1"\n', "plain http"),
            ('[ai]\nworkers = 0\n', "positive integer"),
        ):
            with pytest.raises(SettingsError, match=message):
                load_settings(self._config(tmp_path, block), environ={})
        assert os.environ.get("HUB_AI") is None
