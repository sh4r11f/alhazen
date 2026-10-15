"""Data review fixes (data-review.md): empty files (BL1), retrying closed
uploads (BL2), fenced seals (MA1), long seals and commit failures (MA2), the
unfinished-upload list and abort (MA5), and the server minors: expiry
against sealing, missing-artifact truth, leftovers, index fencing, snapshot
exports and bounded trial pages. Synthetic data; the interleavings are
forced with hooks, so they are deterministic on SQLite and PostgreSQL."""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select, update
from tests.hub.server_support import (
    Hub,
    init_body,
    make_bundle,
    make_settings,
    session_files,
    sha,
)

from alhazen.hub import admin, trials, uploads
from alhazen.hub.auth import Principal
from alhazen.hub.errors import HubError
from alhazen.hub.schema import data_sessions, trial_rows


def ready(hub, tmp_path):
    hub.register("ada")
    hub.register("bob")
    ada, bob = hub.browser("ada"), hub.bearer("bob")
    eid = ada.create_experiment()["id"]
    vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
    assert ada.publish(eid, vid).status_code == 200
    return ada, bob, eid, vid


def as_principal(caller):
    return Principal(user=caller.user, session_id="test", kind="bearer", csrf_token=None)


def row_of(hub, session_id):
    with hub.app.state.hub.db.transaction() as conn:
        return conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()


def service(hub):
    return hub.app.state.hub


class TestEmptyFiles:
    def test_a_session_with_empty_files_completes_without_chunks(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        files = {"events.csv": b"", "logs/empty.log": b"", "data.bin": b"xyz"}
        session = bob.post("/sessions/init", init_body(eid, vid, files)).json()
        assert {f["path"]: f["verified"] for f in session["files"]} == {
            "data.bin": False,
            "events.csv": True,
            "logs/empty.log": True,
        }
        assert bob.put_chunk(session["id"], "data.bin", 0, b"xyz").status_code == 200
        receipt = bob.post(f"/sessions/{session['id']}/complete").json()
        assert receipt["status"] == "committed" and receipt["file_count"] == 3
        final = service(hub).store.final_dir(eid, session["id"])
        assert (final / "files" / "events.csv").read_bytes() == b""
        replay = bob.put_chunk(session["id"], "events.csv", 0, b"")
        assert replay.status_code == 409  # committed; nothing more is accepted

    def test_an_empty_file_must_declare_the_empty_digest(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        body = init_body(eid, vid, {"e": b""})
        body["files"][0]["sha256"] = sha(b"not empty")
        assert bob.post("/sessions/init", body).status_code == 400

    def test_an_empty_put_is_a_harmless_replay(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.post("/sessions/init", init_body(eid, vid, {"e": b""})).json()
        answer = bob.put_chunk(session["id"], "e", 0, b"").json()
        assert answer["replay"] is True and answer["verified"] is True


class TestClosedAttempts:
    def test_abort_then_retry_the_same_client_id(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        files = session_files()
        first = bob.upload_session(eid, vid, files, complete=False)
        assert bob.post(f"/sessions/{first['id']}/abort").json()["status"] == "aborted"
        retry = bob.post("/sessions/init", init_body(eid, vid, files))
        assert retry.status_code == 201
        attempt = retry.json()
        assert attempt["id"] != first["id"] and attempt["previous_attempt_id"] == first["id"]
        assert attempt["client_session_id"] == "run-1"
        receipt = bob.upload_session(eid, vid, files)
        assert receipt["id"] == attempt["id"] and receipt["status"] == "committed"
        assert receipt["client_session_id"] == "run-1"
        old = row_of(hub, first["id"])  # history kept, only its key retired
        assert old.status == "aborted" and old.retired_client_id == "run-1"
        replay = bob.post("/sessions/init", init_body(eid, vid, files))
        assert replay.status_code == 200 and replay.json()["id"] == attempt["id"]
        assert bob.post(f"/sessions/{attempt['id']}/complete").json() == receipt
        changed = bob.post("/sessions/init", init_body(eid, vid, {**files, "x": b"x"}))
        assert changed.status_code == 409 and changed.json()["error"]["status"] == "committed"

    def test_expired_then_retry(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        first = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        hub.clock.advance(7 * 86400 + 1)
        bob = hub.bearer("bob")
        assert bob.post(f"/sessions/{first['id']}/complete").status_code == 410
        retry = bob.post("/sessions/init", init_body(eid, vid, {"a": b"abcd"}))
        assert retry.status_code == 201 and retry.json()["previous_attempt_id"] == first["id"]
        assert row_of(hub, first["id"]).status == "expired"
        assert bob.upload_session(eid, vid, {"a": b"abcd"})["status"] == "committed"

    def test_a_closed_attempt_may_retry_with_new_content(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        first = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        open_conflict = bob.post("/sessions/init", init_body(eid, vid, {"a": b"wxyz"}))
        assert open_conflict.status_code == 409  # abort first
        bob.post(f"/sessions/{first['id']}/abort")
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"wxyz"})).status_code == 201

    def test_retries_still_need_room_and_slots(self, tmp_path, clock):
        tight = make_settings(tmp_path, max_staging_sessions=1)
        admin.init_database(tight)
        service_hub = Hub(tight, clock)
        _ada, bob, eid, vid = ready(service_hub, tmp_path)
        first = bob.upload_session(eid, vid, {"a": b"a"}, client_id="one", complete=False)
        bob.post(f"/sessions/{first['id']}/abort")
        bob.post("/sessions/init", init_body(eid, vid, {"b": b"b"}, "two"))
        again = bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, "one"))
        assert again.status_code == 429
        assert row_of(service_hub, first["id"]).client_session_id == "one"  # not retired on refusal


class TestFencedSeals:
    def setup_upload(self, hub, tmp_path, files=None):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        files = files or {"trials.csv": b"a,b\n1,2\n", "data/eye.bin": b"\x00" * 4096}
        session = bob.upload_session(eid, vid, files, complete=False)
        return bob, eid, session["id"], as_principal(bob)

    def test_takeover_while_the_first_sealer_installs(self, hub, tmp_path, monkeypatch):
        """Review r2: A's lease lapses while it is about to rename; B takes over
        and finds the tree installed under it. Both answer with the committed
        receipt; nothing is reset; no staging is left."""
        bob, eid, sid, who = self.setup_upload(hub, tmp_path)
        hub_ = service(hub)
        store = hub_.store
        a_waiting, go = threading.Event(), threading.Event()
        real_install = store.install_session
        b_started = threading.Event()

        def install(*args, **kwargs):
            if threading.current_thread().name == "A":
                a_waiting.set()
                go.wait(10)
            return real_install(*args, **kwargs)

        real_sha = uploads.sha256_file

        def hashing(path, on_progress=None):
            if threading.current_thread().name == "B" and not b_started.is_set():
                b_started.set()
                go.set()  # A renames staging -> final while B hashes
                deadline = time.monotonic() + 10
                while not store.final_dir(eid, sid).exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            return real_sha(path, on_progress)

        monkeypatch.setattr(store, "install_session", install)
        monkeypatch.setattr(uploads, "sha256_file", hashing)
        out = {}

        def run(name):
            try:
                out[name] = uploads.complete(hub_, who, sid)["status"]
            except HubError as exc:
                out[name] = f"{exc.status} {exc.code}"

        a = threading.Thread(target=run, args=("A",), name="A")
        a.start()
        assert a_waiting.wait(10)
        hub.clock.advance(hub.settings.limits.seal_lease_seconds + 1)
        b = threading.Thread(target=run, args=("B",), name="B")
        b.start()
        b.join(20)
        a.join(20)
        # B holds the claim and commits; A lost it, so it either saw the commit
        # or answers the retryable refusal. Nothing is reset or 500.
        assert out["B"] == "committed", out
        assert out["A"] in ("committed", "409 sealing_in_progress"), out
        assert uploads.complete(hub_, who, sid)["status"] == "committed"
        row = row_of(hub, sid)
        assert row.status == "committed" and row.seal_token is None
        assert not store.staging_dir(sid).exists()

    def test_takeover_after_the_rename_never_resets_bytes(self, hub, tmp_path, monkeypatch):
        """Review r2b: the rename lands before B looks at the first staged
        file. B verifies the final tree instead of calling the files bad."""
        bob, eid, sid, who = self.setup_upload(hub, tmp_path)
        hub_ = service(hub)
        store = hub_.store
        a_waiting, go, a_installed = threading.Event(), threading.Event(), threading.Event()
        real_install = store.install_session
        real_staging_file = store.staging_file
        b_looked = threading.Event()

        def install(*args, **kwargs):
            if threading.current_thread().name == "A":
                a_waiting.set()
                go.wait(10)
            result = real_install(*args, **kwargs)
            if threading.current_thread().name == "A":
                a_installed.set()
            return result

        def staging_file(session_id, rel):
            if threading.current_thread().name == "B" and not b_looked.is_set():
                b_looked.set()
                go.set()
                a_installed.wait(10)
            return real_staging_file(session_id, rel)

        monkeypatch.setattr(store, "install_session", install)
        monkeypatch.setattr(store, "staging_file", staging_file)
        out = {}

        def run(name):
            try:
                out[name] = uploads.complete(hub_, who, sid)["status"]
            except HubError as exc:
                out[name] = f"{exc.status} {exc.code}"

        a = threading.Thread(target=run, args=("A",), name="A")
        a.start()
        assert a_waiting.wait(10)
        hub.clock.advance(hub.settings.limits.seal_lease_seconds + 1)
        b = threading.Thread(target=run, args=("B",), name="B")
        b.start()
        b.join(20)
        a.join(20)
        assert out["B"] == "committed", out
        assert out["A"] in ("committed", "409 sealing_in_progress"), out
        assert row_of(hub, sid).status == "committed"
        progress = bob.get(f"/sessions/{sid}/upload").json()
        assert all(f["received"] == f["size"] for f in progress["files"])

    def test_a_stale_sealer_cannot_release_or_commit_a_live_claim(self, hub, tmp_path):
        bob, eid, sid, who = self.setup_upload(hub, tmp_path)
        hub_ = service(hub)
        state, stale = uploads.begin_complete(hub_, who, sid)
        assert state == "seal"
        hub.clock.advance(hub.settings.limits.seal_lease_seconds + 1)
        state, live = uploads.begin_complete(hub_, who, sid)
        assert state == "seal" and live != stale
        uploads._release(hub_, sid, stale)
        assert row_of(hub, sid).seal_lease_until is not None  # the live claim's lease kept
        with pytest.raises(HubError) as refused:
            uploads.seal(hub_, sid, stale, actor="stale")
        assert refused.value.code == "sealing_in_progress"
        assert row_of(hub, sid).status == "sealing"
        assert uploads.seal(hub_, sid, live, actor="live")["status"] == "committed"

    def test_the_lease_is_renewed_while_hashing(self, hub, tmp_path, monkeypatch):
        files = {f"f{n}": bytes([n]) * 100 for n in range(4)}
        bob, eid, sid, who = self.setup_upload(hub, tmp_path, files)
        hub_ = service(hub)
        lease = hub.settings.limits.seal_lease_seconds
        real_sha = uploads.sha256_file

        def slow(path, on_progress=None):
            hub.clock.advance(lease * 0.6)  # each file takes most of a lease
            return real_sha(path, on_progress)

        monkeypatch.setattr(uploads, "sha256_file", slow)
        state, token = uploads.begin_complete(hub_, who, sid)
        hub.maintenance.run_once(force_reconcile=True)  # nothing to take over yet
        assert uploads.seal(hub_, sid, token, actor="test")["status"] == "committed"
        assert hub.maintenance.report["seals_resumed"] == 0

    def test_commit_failure_releases_the_claim_for_an_immediate_retry(
        self, hub, tmp_path, monkeypatch
    ):
        bob, eid, sid, who = self.setup_upload(hub, tmp_path)
        real = uploads.audit

        def failing(conn, now, actor, action, target, detail):
            if action == "session.commit":
                raise HubError(503, "database_unavailable", "simulated")
            return real(conn, now, actor, action, target, detail)

        monkeypatch.setattr(uploads, "audit", failing)
        assert bob.post(f"/sessions/{sid}/complete").status_code == 503
        row = row_of(hub, sid)
        assert row.status == "sealing" and row.seal_lease_until is None
        monkeypatch.setattr(uploads, "audit", real)
        assert bob.post(f"/sessions/{sid}/complete").json()["status"] == "committed"


class TestLongSeals:
    def test_complete_answers_202_while_sealing_then_the_receipt(self, hub, tmp_path, monkeypatch):
        from alhazen.hub import app as hub_app

        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        release = threading.Event()
        real_install = service(hub).store.install_session

        def slow_install(*args, **kwargs):
            release.wait(10)
            return real_install(*args, **kwargs)

        monkeypatch.setattr(service(hub).store, "install_session", slow_install)
        monkeypatch.setattr(hub_app, "COMPLETE_WAIT_SECONDS", 0.2)
        first = bob.post(f"/sessions/{session['id']}/complete")
        assert first.status_code == 202 and first.json()["status"] == "sealing"
        assert first.headers["retry-after"] == "5"
        second = bob.post(f"/sessions/{session['id']}/complete")
        assert second.status_code == 202
        listed = bob.get("/sessions").json()["items"][0]
        assert listed["status"] == "sealing" and listed["sealing_active"] is True
        assert listed["can_abort"] is False
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            done = bob.post(f"/sessions/{session['id']}/complete")
            if done.status_code == 200:
                break
            time.sleep(0.05)
        receipt = done.json()
        assert receipt["status"] == "committed" and receipt["id"] == session["id"]
        assert receipt["manifest_sha256"] == session["manifest_sha256"]
        assert bob.get("/sessions").json()["items"] == []


class TestUnfinishedList:
    def test_list_shows_only_my_open_uploads(self, hub, tmp_path):
        ada, bob, eid, vid = ready(hub, tmp_path)
        bob.upload_session(eid, vid, {"a": b"a"}, client_id="done")
        pending = bob.upload_session(eid, vid, {"b": b"bb"}, client_id="pending", complete=False)
        listing = bob.get("/sessions").json()
        assert [s["id"] for s in listing["items"]] == [pending["id"]]
        item = listing["items"][0]
        assert item["received_bytes"] == 2 and item["files_verified"] == 1
        assert item["can_abort"] is True and item["expires_at"] and item["expired"] is False
        assert ada.get("/sessions").json()["items"] == []
        assert ada.post(f"/sessions/{pending['id']}/abort").status_code == 404
        assert bob.post(f"/sessions/{pending['id']}/abort").json()["status"] == "aborted"
        assert bob.get("/sessions").json()["items"] == []

    def test_three_abandoned_uploads_can_be_cleared_by_their_owner(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        for n in range(3):
            bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, f"r{n}"))
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, "r3")).status_code == 429
        for item in bob.get("/sessions", params={"limit": 2}).json()["items"]:
            bob.post(f"/sessions/{item['id']}/abort")
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, "r3")).status_code == 201


class TestExpiryAgainstSealing:
    def test_expiry_never_closes_a_session_being_sealed(self, hub, tmp_path):
        """Review r10: the guard is in the UPDATE, so a row moved to sealing
        between the stale-row SELECT and the close stays sealing."""
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        hub_ = service(hub)
        with hub_.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session["id"])
                .values(
                    status="sealing", seal_token="t" * 32, seal_lease_until=hub.clock.now + 10**7
                )
            )
            closed = uploads._close(
                conn,
                session["id"],
                "expired",
                hub.clock.now,
                actor="test",
                horizon=hub.clock.now + 1,
            )
        assert closed is False and row_of(hub, session["id"]).status == "sealing"
        assert hub_.store.staging_dir(session["id"]).exists()

    def test_concurrent_init_expiry_and_stale_complete(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        stale = bob.upload_session(eid, vid, {"a": b"abcd"}, client_id="old", complete=False)
        hub.clock.advance(7 * 86400 + 1)
        bob = hub.bearer("bob")
        hub_ = service(hub)
        who = as_principal(bob)
        out = {}

        def do_init():
            try:
                out["init"] = uploads.init_session(
                    hub_, who, init_body(eid, vid, {"x": b"1"}, "new")
                )[1]
            except HubError as exc:
                out["init"] = exc.code

        def do_complete():
            try:
                out["complete"] = uploads.complete(hub_, who, stale["id"])["status"]
            except HubError as exc:
                out["complete"] = exc.code

        threads = [threading.Thread(target=do_init), threading.Thread(target=do_complete)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert out["complete"] == "upload_expired"  # a stale upload is never sealed
        assert row_of(hub, stale["id"]).status in ("staging", "expired")


class TestReconciliationTruth:
    def test_a_missing_committed_artifact_is_marked_and_cleared_on_restore(self, hub, tmp_path):
        import shutil

        _ada, bob, eid, vid = ready(hub, tmp_path)
        receipt = bob.upload_session(eid, vid, {"a": b"abcd"})
        store = service(hub).store
        final = store.final_dir(eid, receipt["id"])
        backup = tmp_path / "backup-copy"
        shutil.copytree(final, backup)
        shutil.rmtree(final)
        hub.maintenance.run_once(force_reconcile=True)
        report = hub.maintenance.report
        assert report["missing_artifact_ids"] == [receipt["id"]]
        marked = bob.post(f"/sessions/{receipt['id']}/complete").json()
        assert marked["problem"]["code"] == "artifact_missing" and "missing" in marked["durability"]
        detail = bob.get(f"/data/sessions/{receipt['id']}").json()
        assert detail["session"]["problem"]["code"] == "artifact_missing"
        public = hub.client.get("/api/hub/v1/readyz").json()["reconciliation"]
        assert "missing_artifact_ids" not in public and public["missing_artifacts"] == 1
        shutil.copytree(backup, final)
        hub.maintenance.run_once(force_reconcile=True)
        assert bob.post(f"/sessions/{receipt['id']}/complete").json()["problem"] is None

    def test_leftovers_are_removed_only_when_provably_safe(self, hub, tmp_path):
        import os

        _ada, bob, eid, vid = ready(hub, tmp_path)
        receipt = bob.upload_session(eid, vid, {"a": b"abcd"})
        store = service(hub).store
        leftover = store.staging_dir(receipt["id"]) / "files"
        leftover.mkdir(parents=True)
        (leftover / "a").write_bytes(b"abcd")  # bytes re-sent after a lost claim
        unknown = store.root / "staging" / ("e" * 32)
        unknown.mkdir()
        old_part = store.root / "tmp" / "stale.part"
        old_part.write_bytes(b"x")
        os.utime(old_part, (time.time() - 2 * 86400,) * 2)
        fresh_part = store.root / "tmp" / "fresh.part"
        fresh_part.write_bytes(b"x")
        orphan_release = store.root / "releases" / ("f" * 32) / ("1" * 32 + ".zip")
        orphan_release.parent.mkdir(parents=True)
        orphan_release.write_bytes(b"zip")
        hub.maintenance.run_once(force_reconcile=True)
        report = hub.maintenance.report
        assert not store.staging_dir(receipt["id"]).exists()
        assert unknown.exists() and report["unknown_staging"] == 1
        assert not old_part.exists() and fresh_part.exists()
        assert orphan_release.exists() and report["orphan_releases"] == 1


class TestIndexFencing:
    def test_a_rebuild_requested_during_indexing_supersedes_the_running_job(self, hub, tmp_path):
        ada, _bob, eid, vid = ready(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub_ = service(hub)
        claimed = trials.claim_next(hub_, receipt["id"])
        assert claimed is not None
        assert trials.request_reindex(hub_, None, receipt["id"]) == "pending"
        assert trials.index_session(hub_, *claimed) == "superseded"
        assert row_of(hub, receipt["id"]).index_status == "pending"
        hub.maintenance.drain_index()
        assert row_of(hub, receipt["id"]).index_status == "indexed"

    def test_a_transient_database_error_defers_instead_of_failing(self, hub, tmp_path, monkeypatch):
        ada, _bob, eid, vid = ready(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub_ = service(hub)

        def unavailable(*args, **kwargs):
            raise HubError(503, "database_unavailable", "simulated")

        monkeypatch.setattr(trials, "_rows", unavailable)
        claimed = trials.claim_next(hub_, receipt["id"])
        assert trials.index_session(hub_, *claimed) == "pending"
        monkeypatch.undo()
        hub.maintenance.drain_index()
        assert row_of(hub, receipt["id"]).index_status == "indexed"

    def test_an_export_reads_one_snapshot(self, hub, tmp_path):
        ada, _bob, eid, vid = ready(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub.maintenance.drain_index()
        hub_ = service(hub)
        stream = trials.open_export(hub_, receipt["id"], "csv")
        written = threading.Event()

        def rebuild_fails():
            with hub_.db.transaction() as conn:
                conn.execute(trial_rows.delete().where(trial_rows.c.session_id == receipt["id"]))
                conn.execute(
                    update(data_sessions)
                    .where(data_sessions.c.id == receipt["id"])
                    .values(index_status="failed", index_columns=None, index_rows=0)
                )
            written.set()

        writer = threading.Thread(target=rebuild_fails)
        writer.start()
        time.sleep(0.2)
        body = b"".join(stream)  # SQLite: the writer waits for the snapshot to end
        writer.join(40)
        assert written.is_set()
        assert body.count(b"\r\n") == 4  # header + 3 rows from the snapshot
        late = ada.get(f"/data/sessions/{receipt['id']}/export")
        assert late.status_code == 409

    def test_trial_pages_are_bounded_in_bytes(self, hub, tmp_path, monkeypatch):
        ada, _bob, eid, vid = ready(hub, tmp_path)
        receipt = ada.upload_session(eid, vid, session_files())
        hub.maintenance.drain_index()
        monkeypatch.setattr(trials, "PAGE_BYTES", 1)
        page = ada.get(f"/data/sessions/{receipt['id']}/trials", params={"limit": 100}).json()
        assert len(page["items"]) == 1 and page["next_offset"] == 1
        nxt = ada.get(
            f"/data/sessions/{receipt['id']}/trials", params={"offset": page["next_offset"]}
        ).json()
        assert nxt["items"][0]["ordinal"] == 1


class TestExportStreamLifetime:
    """Integration seam: the export stream the response wraps must release its
    database snapshot on close(), whether it was consumed, partly consumed or
    never iterated, without relying on garbage collection."""

    @pytest.fixture
    def indexed(self, hub, tmp_path):
        ada, _bob, eid, vid = ready(hub, tmp_path)
        rows = "".join(f"{n},CORRECT,0.3,x\n" for n in range(1200))
        files = {"trials.csv": ("trial_index,outcome,rt,label\n" + rows).encode()}
        receipt = ada.upload_session(eid, vid, files, chunk=1 << 20)
        hub.maintenance.drain_index()
        return service(hub), receipt["id"], ada

    @pytest.mark.parametrize("fmt", ["csv", "json"])
    @pytest.mark.parametrize("consumed", [0, 1, 2, "all"])
    def test_close_releases_the_snapshot_without_gc(self, indexed, fmt, consumed):
        import gc

        hub_, sid, _ada = indexed
        pool = hub_.db.engine.pool
        gc.disable()
        try:
            stream = trials.open_export(hub_, sid, fmt)
            assert pool.checkedout() == 1  # the primed snapshot holds one connection
            if consumed == "all":
                parts = list(stream)
                assert parts
            else:
                for _ in range(consumed):
                    next(stream)
            stream.close()
            stream.close()  # idempotent
            assert pool.checkedout() == 0
            assert list(stream) == []
        finally:
            gc.enable()

    def test_many_abandoned_exports_do_not_exhaust_connections(self, indexed):
        import gc

        hub_, sid, _ada = indexed
        pool = hub_.db.engine.pool
        gc.disable()
        try:
            for _ in range(3 * (pool.size() + 5)):
                stream = trials.open_export(hub_, sid, "csv")
                next(stream)
                stream.close()
            assert pool.checkedout() == 0
        finally:
            gc.enable()

    def test_a_disconnected_http_export_releases_its_connection(self, indexed, hub):
        hub_, sid, ada = indexed
        pool = hub_.db.engine.pool
        with ada.client.stream(
            "GET", f"/api/hub/v1/data/sessions/{sid}/export", headers=ada.headers()
        ) as response:
            assert response.status_code == 200
            next(response.iter_bytes())  # read a little, then go away
        deadline = time.monotonic() + 5
        while pool.checkedout() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pool.checkedout() == 0


def test_long_seal_over_real_http(hub, tmp_path, monkeypatch):
    """Review MA2 over a real socket: uvicorn serving the app, an httpx client
    with the rig's 20 s timeout. complete answers 202 well inside it while a
    slow seal runs, and the receipt arrives once the seal ends."""
    import socket

    import httpx
    import uvicorn

    from alhazen.hub import app as hub_app

    ada, bob, eid, vid = ready(hub, tmp_path)
    session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
    release = threading.Event()
    store = service(hub).store
    real_install = store.install_session

    def slow_install(*args, **kwargs):
        release.wait(15)
        return real_install(*args, **kwargs)

    monkeypatch.setattr(store, "install_session", slow_install)
    monkeypatch.setattr(hub_app, "COMPLETE_WAIT_SECONDS", 0.3)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(hub.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/api/hub/v1/sessions/{session['id']}/complete"
        with httpx.Client(timeout=20.0) as client:
            started = time.monotonic()
            first = client.post(url, headers=bob.headers())
            assert first.status_code == 202 and time.monotonic() - started < 5
            assert first.json()["status"] == "sealing" and first.headers["retry-after"] == "5"
            release.set()
            for _ in range(100):
                done = client.post(url, headers=bob.headers())
                if done.status_code == 200:
                    break
                time.sleep(0.05)
        assert done.status_code == 200 and done.json()["status"] == "committed"
    finally:
        release.set()
        server.should_exit = True
        thread.join(10)
