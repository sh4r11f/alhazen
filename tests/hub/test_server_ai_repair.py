"""Repair from a rig's run log: a runtime failure no static check caught
goes back to the draft as a repair job; accepting the repaired package makes
the next version of the same experiment. Secrets in the log are removed
before it is stored or sent. Scripted provider and the server's test kit;
SQLite by default, PostgreSQL with ALHAZEN_HUB_TEST_POSTGRES_URL."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.hub import ai_fakes
from tests.hub.test_server_ai import (
    OPENAI_KEY,
    AIHub,
    ai_settings,
    draft,
    job_of,
    signed_in,
    to_generated,
)

from alhazen.hub.ai.providers import ProviderError
from alhazen.hub.ai.redact import clean_log, redact
from alhazen.hub.schema import ai_jobs

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "src/fixation_flash/task.py", line 88, in draw\n'
    "    self.display.draw_disc(self.target)\n"
    "AttributeError: 'SimulatedDisplay' object has no attribute 'draw_disc'\n"
)
SECRET_URL = "https://hub.example.org/api/x?alt=media&token=abc123SECRETtoken99"
BEARER = "Authorization: Bearer ya29.SECRETBEARERVALUE1234"


@pytest.fixture
def ai(tmp_path: Path, clock: Any) -> AIHub:
    return AIHub(ai_settings(tmp_path), clock, ai_fakes.ScriptedProvider())


def accepted(ai: AIHub, who: Any) -> tuple[str, dict[str, Any]]:
    draft_id = to_generated(ai, who)
    r = who.post(f"/ai/drafts/{draft_id}/accept", {})
    assert r.status_code == 200, r.text
    return draft_id, r.json()


def repair(who: Any, draft_id: str, log: str = TRACEBACK, **body: Any) -> Any:
    return who.post(f"/ai/drafts/{draft_id}/repair", {"log": log, **body})


class TestRepair:
    def test_repair_after_a_failed_run_makes_the_next_version(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        draft_id, first = accepted(ai, ada)
        exp_id, v1 = first["experiment"]["id"], first["version"]
        assert v1["version"] == "0.1.0"
        v1_bytes = ada.get(f"/experiments/{exp_id}/versions/{v1['id']}/download").content

        ai.provider.requests.clear()
        ai.provider.script = [ai_fakes.repair_text()]
        log = "\n".join([TRACEBACK, SECRET_URL, BEARER, f"echo {OPENAI_KEY}", "\x1b[31mred\x1b[0m"])
        r = repair(ada, draft_id, log, notes="It crashed in the first trial.")
        assert r.status_code == 202, r.text
        assert r.json()["job"]["kind"] == "repair"
        assert draft(ada, draft_id)["draft"]["status"] == "generating"
        ai.drain()

        view = draft(ada, draft_id)
        job = job_of(view, "source")
        assert view["draft"]["status"] == "generated" and job["status"] == "done"
        sent = ai.provider.sent_text()
        assert "draw_disc" in sent and "It crashed in the first trial." in sent
        for secret in ("abc123SECRETtoken99", "SECRETBEARERVALUE1234", OPENAI_KEY, "\x1b"):
            assert secret not in sent
        assert "BASE FILES" in sent and "run.py" in sent
        repaired = job["disclosed"]["repair"]
        assert repaired["log_redactions"] == 3
        assert repaired["base"]["version_id"] == v1["id"]
        assert {f["path"] for f in repaired["base"]["files"]} >= {"run.py", "configs/task.yaml"}
        assert repaired["log_bytes"] == len(job_log(ai, job["id"]).encode())
        assert "SECRET" not in job_log(ai, job["id"])

        second = ada.post(f"/ai/drafts/{draft_id}/accept", {})
        assert second.status_code == 200, second.text
        v2 = second.json()["version"]
        assert second.json()["experiment"]["id"] == exp_id
        assert v2["version"] == "0.1.1" and v2["id"] != v1["id"] and v2["ai_assisted"] is True
        # The first version is untouched.
        again = ada.get(f"/experiments/{exp_id}/versions/{v1['id']}/download").content
        assert again == v1_bytes
        listed = draft(ada, draft_id)["versions"]
        assert [v["version"] for v in listed] == ["0.1.0", "0.1.1"]
        assert all(v["experiment_id"] == exp_id for v in listed)
        assert draft(ada, draft_id)["draft"]["version_id"] == v2["id"]
        assert [e["id"] for e in ada.get("/experiments").json()["items"]] == [exp_id]

    def test_a_kit_bump_is_kept(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        draft_id, _ = accepted(ai, ada)
        ai.provider.script = [ai_fakes.repair_text(version="0.2.0")]
        repair(ada, draft_id)
        ai.drain()
        r = ada.post(f"/ai/drafts/{draft_id}/accept", {})
        assert r.json()["version"]["version"] == "0.2.0"

    def test_repair_of_a_generated_draft_uses_the_generated_package(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        draft_id = to_generated(ai, ada)
        source_job = draft(ada, draft_id)["draft"]["source_job_id"]
        ai.provider.script = [ai_fakes.repair_text()]
        assert repair(ada, draft_id).status_code == 202
        ai.drain()
        job = job_of(draft(ada, draft_id), "source")
        assert job["disclosed"]["repair"]["base"] == {
            **job["disclosed"]["repair"]["base"],
            "draft_id": draft_id,
            "job_id": source_job,
        }
        r = ada.post(f"/ai/drafts/{draft_id}/accept", {})
        assert r.status_code == 200 and r.json()["version"]["version"] == "0.1.0"

    def test_a_failed_repair_leaves_the_accepted_version_in_place(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        draft_id, first = accepted(ai, ada)
        ai.provider.script = [ProviderError("quota", "out of credit")]
        repair(ada, draft_id)
        ai.drain()
        view = draft(ada, draft_id)
        assert view["draft"]["status"] == "accepted"
        assert view["draft"]["version_id"] == first["version"]["id"]
        failed = [j for j in view["jobs"] if j["kind"] == "repair"][0]
        assert failed["error"]["code"] == "provider_quota"
        r = ada.post(f"/ai/drafts/{draft_id}/accept", {})
        assert r.status_code == 409 and r.json()["error"]["code"] == "already_accepted"
        ai.provider.script = [ai_fakes.repair_text()]
        assert repair(ada, draft_id).status_code == 202  # may try again

    def test_a_failed_regeneration_keeps_the_earlier_package_acceptable(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        draft_id = to_generated(ai, ada)
        ai.provider.script = [ProviderError("timeout", "slow")]
        ada.post(f"/ai/drafts/{draft_id}/generate", {})
        ai.drain()
        assert draft(ada, draft_id)["draft"]["status"] == "generated"
        assert ada.post(f"/ai/drafts/{draft_id}/accept", {}).status_code == 200

    def test_refusals(self, ai: AIHub) -> None:
        ada = signed_in(ai)
        ai.provider.script = [ai_fakes.plan_text()]
        planned = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai"}).json()
        ai.drain()
        draft_id = planned["draft"]["id"]
        r = repair(ada, draft_id)
        assert r.status_code == 409 and r.json()["error"]["code"] == "repair_unavailable"
        r = ada.post(f"/ai/drafts/{draft_id}/repair", {})
        assert r.status_code == 400
        r = repair(ada, draft_id, "x" * (64 * 1024 + 1))
        assert r.status_code == 400 and r.json()["error"]["code"] == "log_too_large"
        r = repair(ada, draft_id, TRACEBACK, extra=1)
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_field"
        bob = signed_in(ai, "bob")
        assert repair(bob, draft_id).status_code == 404
        csrf_less = ada.client.post(
            f"/api/hub/v1/ai/drafts/{draft_id}/repair",
            json={"log": TRACEBACK},
            headers={"Origin": "http://127.0.0.1:8750"},
        )
        assert csrf_less.status_code == 403

    def test_repair_jobs_count_against_the_limits(self, tmp_path: Path, clock: Any) -> None:
        provider = ai_fakes.ScriptedProvider()
        ai = AIHub(ai_settings(tmp_path, max_active_jobs_per_user=1), clock, provider)
        ada = signed_in(ai)
        draft_id, _ = accepted(ai, ada)
        assert repair(ada, draft_id).status_code == 202
        busy = ada.post("/ai/drafts", {"prompt": "x", "provider": "openai"})
        assert busy.status_code == 429 and busy.json()["error"]["code"] == "ai_jobs_busy"


def job_log(ai: AIHub, job_id: str) -> str:
    import json

    with ai.service.db.transaction() as conn:
        row = conn.execute(select(ai_jobs.c.request_json).where(ai_jobs.c.id == job_id)).one()
    return str(json.loads(row.request_json)["log"])


class TestRedaction:
    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            (SECRET_URL, "abc123SECRETtoken99"),
            ("GET https://s.example/o?X-Goog-Signature=deadbeefcafe0123", "deadbeefcafe0123"),
            (BEARER, "SECRETBEARERVALUE1234"),
            ("key=sk-proj-AAAAAAAAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAAAAAAAA"),
            ("using AIzaSyA1234567890abcdefghijklmnopqrst", "AIzaSyA1234567890"),
            ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghij"),
            ("HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz", "hf_abcdefghij"),
            ("git clone https://user:s3cretpass@github.com/x/y.git", "s3cretpass"),
            ("password = hunter22", "hunter22"),
            ("api_key: 'abcd1234efgh'", "abcd1234efgh"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop", "eyJzdWIi"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ],
    )
    def test_secrets_are_removed(self, text: str, secret: str) -> None:
        cleaned, count = redact(text)
        assert secret not in cleaned and count >= 1 and "[redacted" in cleaned

    def test_what_a_repair_needs_is_kept(self) -> None:
        keep = (
            TRACEBACK
            + "sha256 170226a4852f8b055961f1567fa634b2182c1b7769cf8a6493f4b601912fb479\n"
            + "max_tokens=16000 trials: 10 Basic usage\n"
        )
        assert redact(keep) == (keep, 0)

    def test_known_values_and_cleaning(self) -> None:
        cleaned, count = redact("a mystoredkey123 b mystoredkey123", ["mystoredkey123"])
        assert cleaned == "a [redacted stored-key] b [redacted stored-key]" and count == 2
        assert clean_log("a\x1b[31mred\x1b[0m\r\nb\x07\tc") == "ared\nb\tc"
