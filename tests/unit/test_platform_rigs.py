"""Machine rig configs on the platform: rigsync's rules, the SQLite store and the routes
(docs/design/machine-rig-configs.md §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.platform_support import call, web_dir
from tests.unit.test_platform_machines import bearer, post, signed_in
from tests.unit.test_platform_machines import server as server  # noqa: F401 - fixture
from tests.unit.test_platform_machines import store as store  # noqa: F401 - fixture

from alhazen.experiment_platform import rigsync
from alhazen.experiment_platform.rigstore import RigStore
from alhazen.experiment_platform.webapp import PlatformApp

MID = "mc_0123456789abcdef"
A = "monitor:\n  width_px: 1920\n"
B = "monitor:\n  width_px: 2560\n"
C = "monitor:\n  width_px: 1280\n"


def write(cid: str, text: str, base: int, name: str = "booth") -> dict:
    return {
        "id": cid,
        "name": name,
        "text": text,
        "sha256": rigsync.sha256_text(text),
        "base": base,
    }


def first(text: str = A) -> dict:
    record, results = rigsync.apply_push(None, MID, {"writes": [write("c1", text, 0)]}, 1)
    assert results == {"c1": {"ok": True, "revision": 1}}
    return record


class TestMachineSide:
    def test_a_first_sync_creates_the_record_synced(self):
        record = first()
        config = record["configs"]["c1"]
        assert (config["revision"], config["state"], config["updatedBy"]) == (
            1,
            "synced",
            "machine",
        )
        assert config["sha256"] == rigsync.sha256_text(A) and record["seenAt"] == 1

    def test_a_write_against_an_old_revision_changes_nothing(self):
        record = first()
        record = rigsync.owner_put(
            record, MID, "c1", name="booth", text=B, base=1, based_on=rigsync.sha256_text(A), now=2
        )
        after, results = rigsync.apply_push(record, MID, {"writes": [write("c1", C, 1)]}, 3)
        assert results["c1"] == {"ok": False, "conflict": True, "revision": 2}
        assert after["configs"]["c1"]["text"] == B and after["configs"]["c1"]["state"] == "pending"

    def test_a_conflict_report_keeps_both_versions(self):
        record = rigsync.owner_put(
            first(), MID, "c1", name="booth", text=B, base=1, based_on=None, now=2
        )
        after, _ = rigsync.apply_push(record, MID, {"conflicts": [{"id": "c1", "text": C}]}, 3)
        config = after["configs"]["c1"]
        assert config["text"] == B and config["conflict"]["text"] == C and config["revision"] == 2

    def test_acks_take_or_refuse_a_platform_edit_and_ignore_stale_ones(self):
        record = rigsync.owner_put(
            first(), MID, "c1", name="booth", text=B, base=1, based_on=None, now=2
        )
        stale, _ = rigsync.apply_push(
            record, MID, {"acks": [{"id": "c1", "revision": 1, "ok": True}]}, 3
        )
        assert stale["configs"]["c1"]["state"] == "pending"
        refused, _ = rigsync.apply_push(
            record, MID, {"acks": [{"id": "c1", "revision": 2, "ok": False, "reason": "bad"}]}, 3
        )
        assert refused["configs"]["c1"]["state"] == "rejected"
        assert refused["configs"]["c1"]["rejection"]["reason"] == "bad"
        taken, _ = rigsync.apply_push(
            record, MID, {"acks": [{"id": "c1", "revision": 2, "ok": True}]}, 3
        )
        assert taken["configs"]["c1"]["state"] == "synced"

    def test_a_malformed_push_changes_nothing(self):
        record = first()
        with pytest.raises(rigsync.RigSyncError):
            rigsync.apply_push(record, MID, {"writes": [write("c1", B, 1), {"id": "c2"}]}, 2)
        assert record["configs"]["c1"]["text"] == A

    def test_two_configs_cannot_share_a_name(self):
        record = first()
        _after, results = rigsync.apply_push(record, MID, {"writes": [write("c2", B, 0)]}, 2)
        assert results["c2"]["ok"] is False and "named 'booth'" in results["c2"]["reason"]

    def test_running_reports_and_the_default(self):
        record = first()
        push = {
            "default": "c1",
            "defaultRevSeen": 0,
            "running": {"c1": {"name": "booth", "text": A, "problem": "x" * 5000}},
        }
        after, _ = rigsync.apply_push(record, MID, push, 5)
        assert after["default"] == "c1" and after["defaultRev"] == 1
        assert after["configs"]["c1"]["running"]["sha256"] == rigsync.sha256_text(A)
        assert len(after["configs"]["c1"]["running"]["problem"]) == rigsync.MAX_REASON
        # The platform's newer choice stands against a machine that has not seen it.
        moved = rigsync.apply_push(after, MID, {"writes": [write("c2", B, 0, "eyelink")]}, 6)[0]
        moved = rigsync.owner_default(moved, MID, "c2", base=1, now=7)
        kept, _ = rigsync.apply_push(moved, MID, {"default": "c1", "defaultRevSeen": 1}, 8)
        assert kept["default"] == "c2" and kept["defaultBy"] == "platform"

    def test_firebase_dropped_fields_are_restored(self):
        stored = {"id": MID, "configs": {"c1": {"name": "booth", "text": A, "revision": 3}}}
        record = rigsync.normalize(stored, MID)
        assert record["configs"]["c1"]["deleted"] is False and record["defaultRev"] == 0
        assert record["configs"]["c1"]["conflict"] is None and record["templates"] is None


class TestOwnerSide:
    def test_an_edit_is_pending_with_what_it_started_from(self):
        record = rigsync.owner_put(
            first(), MID, "c1", name="booth", text=B, base=1, based_on=rigsync.sha256_text(A), now=2
        )
        config = record["configs"]["c1"]
        assert (config["state"], config["revision"], config["updatedBy"]) == (
            "pending",
            2,
            "platform",
        )
        assert config["basedOn"] == rigsync.sha256_text(A)

    def test_a_stale_edit_is_refused(self):
        with pytest.raises(rigsync.RigSyncError) as caught:
            rigsync.owner_put(
                first(), MID, "c1", name="booth", text=B, base=0, based_on=None, now=2
            )
        assert caught.value.kind == "conflict"

    def test_settling_a_conflict_marks_the_machines_version_as_known(self):
        record = rigsync.owner_put(
            first(), MID, "c1", name="booth", text=B, base=1, based_on=None, now=2
        )
        record, _ = rigsync.apply_push(record, MID, {"conflicts": [{"id": "c1", "text": C}]}, 3)
        mine = rigsync.owner_resolve(record, MID, "c1", keep="machine", now=4)["configs"]["c1"]
        assert (
            mine["text"] == C and mine["basedOn"] == rigsync.sha256_text(C) and not mine["conflict"]
        )
        theirs = rigsync.owner_resolve(record, MID, "c1", keep="platform", now=4)["configs"]["c1"]
        assert theirs["text"] == B and theirs["basedOn"] == rigsync.sha256_text(C)
        assert theirs["revision"] == 3

    def test_deleting_waits_for_the_machine_and_never_takes_the_default(self):
        record = first()
        record, _ = rigsync.apply_push(
            record,
            MID,
            {
                "writes": [write("c2", B, 0, "eyelink")],
                "default": "c1",
                "defaultRevSeen": 0,
                "running": {"c2": {"name": "eyelink", "text": B}},
            },
            2,
        )
        with pytest.raises(rigsync.RigSyncError, match="default"):
            rigsync.owner_delete(record, MID, "c1", base=1, now=3)
        pending = rigsync.owner_delete(record, MID, "c2", base=1, now=3)
        assert pending["configs"]["c2"]["deleted"] and pending["configs"]["c2"]["revision"] == 2
        gone, _ = rigsync.apply_push(
            pending, MID, {"acks": [{"id": "c2", "revision": 2, "ok": True}]}, 4
        )
        assert "c2" not in gone["configs"]

    def test_a_config_the_machine_never_ran_is_dropped_at_once(self):
        record = first()
        record = rigsync.owner_put(
            record, MID, "c9", name="new", text=B, base=0, based_on=None, now=2
        )
        assert "c9" not in rigsync.owner_delete(record, MID, "c9", base=1, now=3)["configs"]


def test_the_store_applies_the_rules_per_person(tmp_path: Path):
    rigs = RigStore(tmp_path / "rigs.sqlite3")
    record, results = rigs.push("ada", MID, {"label": "Booth", "writes": [write("c1", A, 0)]})
    assert results["c1"]["ok"] and rigs.get("ben", MID) is None
    rigs.put("ada", MID, "c1", {"name": "booth", "text": B, "base": 1})
    listed = rigs.list("ada")
    assert listed[0]["label"] == "Booth" and "text" not in listed[0]["configs"]["c1"]
    assert listed[0]["configs"]["c1"]["state"] == "pending"
    with pytest.raises(Exception, match="changed since"):
        rigs.put("ada", MID, "c1", {"name": "booth", "text": C, "base": 1})
    rigs.forget("ada", MID)
    assert rigs.list("ada") == []


@pytest.fixture()
def rig_server(server, store):  # noqa: F811 - the machines tests' fixtures
    app, accounts = server
    app.rigs = RigStore(store.root / "rigs.sqlite3")
    return app


def put(app, path, headers, body):  # type: ignore[no-untyped-def]
    return call(
        app,
        "PUT",
        path,
        headers={**headers, "Content-Type": "application/json"},
        body=json.dumps(body).encode(),
    )


class TestRoutes:
    def test_a_machine_syncs_its_record_and_the_owner_edits_it(self, rig_server):
        app = rig_server
        ada, ben = signed_in(app, "ada"), signed_in(app, "ben")
        assert call(app, "GET", "/api/session", headers=ada).json()["capabilities"]["rigConfigs"]
        token = post(app, "/api/machines", ada, {"name": "Booth 2"}).json()["token"]
        machine = {**bearer(token)}
        assert call(app, "GET", f"/api/rigs/machine/{MID}", headers=machine).json() == {
            "record": None
        }
        synced = post(
            app,
            f"/api/rigs/machine/{MID}/sync",
            machine,
            {"label": "Booth", "writes": [write("c1", A, 0)]},
        ).json()
        assert synced["results"]["c1"]["ok"] and synced["record"]["token"]
        listed = call(app, "GET", "/api/rigs", headers=ada).json()["machines"]
        assert [m["id"] for m in listed] == [MID]
        assert call(app, "GET", "/api/rigs", headers=ben).json()["machines"] == []
        assert call(app, "GET", f"/api/rigs/{MID}", headers=ben).status == 404
        edited = put(
            app,
            f"/api/rigs/{MID}/configs/c1",
            ada,
            {"name": "booth", "text": B, "base": 1, "basedOn": rigsync.sha256_text(A)},
        )
        assert edited.status == 200 and edited.json()["config"]["state"] == "pending"
        stale = put(
            app, f"/api/rigs/{MID}/configs/c1", ada, {"name": "booth", "text": C, "base": 1}
        )
        assert stale.status == 409
        seen = call(app, "GET", f"/api/rigs/machine/{MID}", headers=machine).json()["record"]
        assert seen["configs"]["c1"]["text"] == B
        # A machine token never reaches the owner's routes.
        assert call(app, "GET", "/api/rigs", headers=machine).status == 403
        assert (
            put(
                app, f"/api/rigs/{MID}/configs/c1", machine, {"name": "booth", "text": C, "base": 2}
            ).status
            == 403
        )
        # Without the CSRF header the browser's edit is refused.
        no_csrf = {k: v for k, v in ada.items() if k != "X-Alhazen-CSRF"}
        assert put(
            app, f"/api/rigs/{MID}/configs/c1", no_csrf, {"name": "booth", "text": C, "base": 2}
        ).status in (401, 403)
        deleted = call(app, "DELETE", f"/api/rigs/{MID}/configs/c1", headers=ada, query="base=x")
        assert deleted.status == 400

    def test_a_host_without_a_rig_store_says_so(self, server):
        app, _ = server
        ada = signed_in(app, "ada")
        assert call(app, "GET", "/api/rigs", headers=ada).status == 404
        assert (
            call(app, "GET", "/api/session", headers=ada).json()["capabilities"]["rigConfigs"]
            is False
        )


def test_a_local_platform_keeps_rig_configs(tmp_path: Path, store):  # noqa: F811
    from alhazen.experiment_platform.local import LocalPlatform

    platform = LocalPlatform(store)
    try:
        app: PlatformApp = platform._server.get_app()  # type: ignore[attr-defined]
        assert app.rigs is not None and (store.root / "rigs.sqlite3").is_file()
    finally:
        platform._server.server_close()
    assert web_dir  # the support module is imported for the fixtures' sake
