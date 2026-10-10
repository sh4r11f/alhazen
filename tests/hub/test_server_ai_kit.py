"""The hub's AI jobs with the REAL authoring kit (alhazen.hub.ai.author)
and its FakeProvider (tests/hub/ai_support.py): plan, source, acceptance and
download end to end, failure mapping, and disclosure as the kit reports it.
SQLite by default, PostgreSQL with ALHAZEN_HUB_TEST_POSTGRES_URL. No network.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from tests.hub import ai_support
from tests.hub.server_support import Hub, make_bundle
from tests.hub.test_server_ai import OPENAI_KEY, ai_settings, draft, job_of

from alhazen.hub.ai.jobs import AuthorKit


class KitHub(Hub):
    def __init__(self, settings: Any, clock: Any, fake: ai_support.FakeProvider) -> None:
        super().__init__(settings, clock)
        self.fake = fake
        runner = self.app.state.ai_worker.runner
        runner.kit = AuthorKit.default()
        runner.client_factory = lambda info, key, model: fake

    def drain(self) -> int:
        return int(self.app.state.ai_worker.drain())


@pytest.fixture
def fake() -> ai_support.FakeProvider:
    return ai_support.FakeProvider()


@pytest.fixture
def kh(tmp_path: Path, clock: Any, fake: ai_support.FakeProvider) -> KitHub:
    return KitHub(ai_settings(tmp_path), clock, fake)


def user(kh: KitHub, name: str = "ada") -> Any:
    kh.register(name)
    who = kh.browser(name)
    assert who.put("/ai/keys/openai", json={"key": OPENAI_KEY}).status_code == 200
    return who


def start(who: Any, **body: Any) -> dict[str, Any]:
    r = who.post(
        "/ai/drafts",
        {
            "prompt": ai_support.FIXTURES.joinpath("prompt.txt").read_text("utf-8")[:8000],
            "provider": "openai",
            **body,
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def test_plan_source_accept_download_with_the_real_kit(kh: KitHub) -> None:
    ada = user(kh)
    created = start(ada)
    kh.drain()
    view = draft(ada, created["draft"]["id"])
    assert view["draft"]["status"] == "planned", view["jobs"]
    plan = view["plan"]
    plan_job = job_of(view, "plan")
    assert plan_job["usage"]["input_tokens"] > 0
    assert plan_job["disclosed"]["authoring_context"]["context"]
    assert plan_job["disclosed"]["start_from"] is None
    assert OPENAI_KEY not in kh.fake.sent_text()

    r = ada.post(f"/ai/drafts/{created['draft']['id']}/generate", {})
    assert r.status_code == 202
    kh.drain()
    view = draft(ada, created["draft"]["id"])
    source = job_of(view, "source")
    assert view["draft"]["status"] == "generated", source
    result = source["result"]
    assert result["valid"] is True and result["report"]["ok"] is True
    paths = {f["path"] for f in result["files"]}
    assert {"run.py", "docs/experiment.json", "docs/ai-provenance.json", "LICENSE"} <= paths

    accepted = ada.post(
        f"/ai/drafts/{created['draft']['id']}/accept",
        {"title": plan["title"], "license": "Apache-2.0"},
    )
    assert accepted.status_code == 200, accepted.text
    exp, ver = accepted.json()["experiment"], accepted.json()["version"]
    assert ver["ai_assisted"] is True and ver["manifest"]["ai_assisted"] is True
    assert exp["license"] == ver["manifest"]["license"] == "Apache-2.0"
    download = ada.get(f"/experiments/{exp['id']}/versions/{ver['id']}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        stored_manifest = json.loads(archive.read("alhazen-package.json"))
        assert "ai_assisted" not in stored_manifest  # the package format refuses it
        assert "Apache" in archive.read("LICENSE").decode()
        provenance = json.loads(archive.read("docs/ai-provenance.json"))
    assert provenance["ai_assisted"] is True
    docs = ada.get(f"/experiments/{exp['id']}/versions/{ver['id']}/documentation")
    assert docs.status_code == 200 and docs.json()["documentation"] is not None


def test_rules_invalid_source_is_generation_invalid_with_the_kit_report(kh: KitHub) -> None:
    ada = user(kh)
    created = start(ada)
    kh.drain()
    kh.fake.behaviours = ["rules_invalid"]
    ada.post(f"/ai/drafts/{created['draft']['id']}/generate", {})
    kh.drain()
    view = draft(ada, created["draft"]["id"])
    source = job_of(view, "source")
    assert view["draft"]["status"] == "planned"
    assert source["error"]["code"] == "generation_invalid"
    report = source["result"]["report"]
    assert report["ok"] is False and any(not c["ok"] for c in report["checks"])


@pytest.mark.parametrize(
    ("behaviour", "code"),
    [("quota", "provider_quota"), ("timeout", "provider_timeout"), ("auth", "key_rejected")],
)
def test_fake_provider_errors_map(kh: KitHub, behaviour: str, code: str) -> None:
    kh.fake.behaviours = [behaviour]
    ada = user(kh)
    created = start(ada)
    kh.drain()
    job = ada.get(f"/ai/jobs/{created['job']['id']}").json()["job"]
    assert job["status"] == "failed" and job["error"]["code"] == code


def test_start_from_disclosure_matches_what_the_kit_sent(kh: KitHub, tmp_path: Path) -> None:
    ada = user(kh)
    eid = ada.create_experiment()["id"]
    bundle = make_bundle(tmp_path, extra={"task.py": b"MARKER = 'own-source-17'\n"})
    vid = ada.upload_version(eid, bundle).json()["version"]["id"]
    created = start(ada, start_from={"experiment_id": eid, "version_id": vid})
    kh.drain()
    job = draft(ada, created["draft"]["id"])["jobs"][0]
    disclosed = job["disclosed"]["start_from"]
    assert disclosed["version_id"] == vid
    sent = kh.fake.sent_text()
    assert "own-source-17" in sent
    for item in disclosed["files"]:
        assert item["path"] in sent


def test_repair_through_the_real_kit_seam(kh: KitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """``author.repair_from_run(client, bundle, log, ctx)`` is stubbed (the
    kit's version lands separately) on top of the real kit's bundle type and
    packaging, so the server's half is exercised against the real shapes."""
    from alhazen.hub.ai import author

    seen: dict[str, Any] = {}

    def repair_from_run(client: Any, bundle: Any, log: str, ctx: Any) -> Any:
        seen["bundle"], seen["log"] = bundle, log
        client.complete(
            [{"role": "user", "content": "repair:\n" + log}],
            json_schema={"title": "alhazen_source", "type": "object"},
            max_tokens=100,
        )
        files = dict(bundle.files)
        files["README.md"] = files.get("README.md", b"") + b"\nRepaired after a run.\n"
        metadata = {
            k: v for k, v in bundle.manifest.items() if k not in ("files", "schema_version")
        }
        metadata["version"] = "0.1.1"
        archive, info = author.bundle_archive(files, metadata)
        return author.GeneratedBundle(
            files=files,
            manifest=info.manifest,
            report=bundle.report,
            archive=archive,
            sha256=info.sha256,
        )

    monkeypatch.setattr(author, "repair_from_run", repair_from_run, raising=False)
    ada = user(kh)
    created = start(ada)
    kh.drain()
    ada.post(f"/ai/drafts/{created['draft']['id']}/generate", {})
    kh.drain()
    first = ada.post(f"/ai/drafts/{created['draft']['id']}/accept", {}).json()
    log = "AttributeError: 'SimulatedDisplay' object has no attribute 'draw_disc'"
    assert ada.post(f"/ai/drafts/{created['draft']['id']}/repair", {"log": log}).status_code == 202
    kh.drain()
    assert isinstance(seen["bundle"], author.GeneratedBundle)
    assert "docs/ai-provenance.json" in seen["bundle"].files and seen["log"] == log
    second = ada.post(f"/ai/drafts/{created['draft']['id']}/accept", {})
    assert second.status_code == 200, second.text
    version = second.json()["version"]
    assert version["version"] == "0.1.1"
    assert second.json()["experiment"]["id"] == first["experiment"]["id"]
    docs = ada.get(
        f"/experiments/{first['experiment']['id']}/versions/{version['id']}/documentation"
    )
    assert docs.status_code == 200
