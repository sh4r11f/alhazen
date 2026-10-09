"""Private session uploads (review gate B1): reservation, verified chunks,
replay and conflict, seal and commit ordering with injected failures,
reconciliation, quotas, expiry and isolation between users. Synthetic data."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select, update
from tests.hub.server_support import (
    API,
    Hub,
    init_body,
    make_bundle,
    make_settings,
    session_files,
    sha,
)

from alhazen.hub import admin, uploads
from alhazen.hub.schema import data_sessions


def ready(hub, tmp_path, *, publish=True):
    hub.register("ada")
    hub.register("bob")
    ada, bob = hub.browser("ada"), hub.bearer("bob")
    eid = ada.create_experiment()["id"]
    vid = ada.upload_version(eid, make_bundle(tmp_path)).json()["version"]["id"]
    if publish:
        assert ada.publish(eid, vid).status_code == 200
    return ada, bob, eid, vid


def status_of(hub, session_id):
    with hub.app.state.hub.db.transaction() as conn:
        return conn.execute(select(data_sessions).where(data_sessions.c.id == session_id)).one()


class TestHappyPath:
    def test_collector_uploads_and_receives_a_receipt(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        files = session_files()
        receipt = bob.upload_session(eid, vid, files)
        assert receipt["status"] == "committed" and receipt["file_count"] == 3
        assert receipt["total_bytes"] == sum(len(d) for d in files.values())
        assert "not an independent backup" in receipt["durability"]
        assert bob.post(f"/sessions/{receipt['id']}/complete").json() == receipt  # idempotent
        final = hub.settings.artifact_root / "sessions" / eid / receipt["id"]
        assert (final / "manifest.json").is_file()
        assert (final / "files" / "sub-01_ses-01_run-01_trials.csv").read_bytes() == files[
            "sub-01_ses-01_run-01_trials.csv"
        ]
        assert not (hub.settings.artifact_root / "staging" / receipt["id"]).exists()

    def test_init_replay_and_conflict(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        body = init_body(eid, vid, session_files())
        first = bob.post("/sessions/init", body)
        assert first.status_code == 201
        again = bob.post("/sessions/init", body)
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
        changed = init_body(eid, vid, {**session_files(), "extra.txt": b"x"})
        clash = bob.post("/sessions/init", changed)
        assert clash.status_code == 409 and clash.json()["error"]["code"] == "session_conflict"

    def test_progress_reports_received_bytes(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        files = {"a.bin": b"0123456789"}
        session = bob.upload_session(eid, vid, files, complete=False, chunk=4)
        progress = bob.get(f"/sessions/{session['id']}/upload").json()
        assert progress["status"] == "staging"
        assert progress["files"][0]["received"] == 10 and progress["files"][0]["verified"]


class TestChunks:
    def start(self, hub, tmp_path, files):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.post("/sessions/init", init_body(eid, vid, files)).json()
        return bob, session["id"]

    def test_replay_conflict_and_offset_rules(self, hub, tmp_path):
        bob, sid = self.start(hub, tmp_path, {"a.bin": b"abcdefgh"})
        assert bob.put_chunk(sid, "a.bin", 0, b"abcd").json()["received"] == 4
        replay = bob.put_chunk(sid, "a.bin", 0, b"abcd")
        assert replay.status_code == 200 and replay.json()["replay"] is True
        other = bob.put_chunk(sid, "a.bin", 0, b"zzzz")
        assert other.status_code == 409 and other.json()["error"]["code"] == "chunk_conflict"
        ahead = bob.put_chunk(sid, "a.bin", 6, b"gh")
        assert ahead.status_code == 409 and ahead.json()["error"]["received"] == 4
        inside = bob.put_chunk(sid, "a.bin", 2, b"cd")
        assert inside.status_code == 409 and inside.json()["error"]["code"] == "offset_mismatch"
        past = bob.put_chunk(sid, "a.bin", 4, b"efghi")
        assert past.status_code == 400 and past.json()["error"]["code"] == "chunk_out_of_range"
        assert bob.put_chunk(sid, "a.bin", 4, b"efgh").json()["verified"] is True

    def test_chunk_digest_and_path_checks(self, hub, tmp_path):
        bob, sid = self.start(hub, tmp_path, {"a.bin": b"abcd"})
        assert bob.put_chunk(sid, "a.bin", 0, b"abcd", digest=sha(b"other")).status_code == 400
        assert bob.put_chunk(sid, "a.bin", 0, b"abcd", digest="nothex").status_code == 400
        assert bob.put_chunk(sid, "b.bin", 0, b"abcd").status_code == 404
        assert bob.put_chunk(sid, "../a.bin", 0, b"abcd").status_code == 404
        no_offset = bob.put(
            f"/sessions/{sid}/files",
            params={"path": "a.bin"},
            content=b"abcd",
            headers={"X-Chunk-SHA256": sha(b"abcd")},
        )
        assert no_offset.status_code == 400

    def test_a_tampered_file_is_discarded_at_its_last_byte(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        body = init_body(eid, vid, {"a.bin": b"abcd"})
        body["files"][0]["sha256"] = sha(b"wxyz")  # the client declared other content
        sid = bob.post("/sessions/init", body).json()["id"]
        bad = bob.put_chunk(sid, "a.bin", 0, b"abcd")
        assert bad.status_code == 409 and bad.json()["error"]["code"] == "file_hash_mismatch"
        assert bob.get(f"/sessions/{sid}/upload").json()["files"][0]["received"] == 0
        assert bob.put_chunk(sid, "a.bin", 0, b"wxyz").json()["verified"] is True

    def test_empty_file_and_oversized_chunk(self, tmp_path, clock):
        small = make_settings(tmp_path, max_chunk_bytes=4)
        admin.init_database(small)
        service = Hub(small, clock)
        _ada, bob, eid, vid = ready(service, tmp_path)
        sid = bob.post("/sessions/init", init_body(eid, vid, {"e": b"", "a": b"abcdef"})).json()[
            "id"
        ]
        assert bob.put_chunk(sid, "e", 0, b"").json()["verified"] is True
        assert bob.put_chunk(sid, "a", 0, b"abcdef").status_code == 413
        assert bob.put_chunk(sid, "a", 0, b"abc").status_code == 200

    def test_concurrent_identical_and_different_chunks(self, tmp_path, clock):
        roomy = make_settings(tmp_path, max_concurrent_transfers=8)
        admin.init_database(roomy)
        hub = Hub(roomy, clock)
        bob, sid = self.start(hub, tmp_path, {"a.bin": b"x" * 64})
        results = []

        def send(data):
            results.append(bob.put_chunk(sid, "a.bin", 0, data).status_code)

        threads = [threading.Thread(target=send, args=(b"x" * 32,)) for _ in range(3)]
        threads += [threading.Thread(target=send, args=(b"y" * 32,)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        progress = bob.get(f"/sessions/{sid}/upload").json()["files"][0]
        assert progress["received"] == 32
        # Exactly one content won the offset; the other kind all conflicted.
        assert sorted(results).count(409) == 3 and sorted(results).count(200) == 3
        stored = (hub.settings.artifact_root / "staging" / sid / "files" / "a.bin").read_bytes()
        assert stored in (b"x" * 32, b"y" * 32)


class TestSealing:
    def test_incomplete_sessions_cannot_complete_or_be_seen(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abc", "b": b"def"}, complete=False)
        bob.put_chunk(session["id"], "a", 0, b"abc")
        response = bob.post(f"/sessions/{session['id']}/complete")
        assert response.status_code == 200  # upload_session already sent every chunk
        sid = bob.post("/sessions/init", init_body(eid, vid, {"c": b"abc"}, "run-2")).json()["id"]
        incomplete = bob.post(f"/sessions/{sid}/complete")
        assert incomplete.status_code == 409
        assert incomplete.json()["error"]["missing"] == ["c"]
        listed = [s["id"] for s in bob.get("/data/sessions").json()["items"]]
        assert sid not in listed and bob.get(f"/data/sessions/{sid}").status_code == 404

    def test_corruption_on_disk_before_seal_is_caught(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd", "b": b"efgh"}, complete=False)
        staged = hub.settings.artifact_root / "staging" / session["id"] / "files" / "b"
        staged.write_bytes(b"EFGH")  # same length, different bytes
        response = bob.post(f"/sessions/{session['id']}/complete")
        assert response.status_code == 409 and response.json()["error"]["paths"] == ["b"]
        progress = bob.get(f"/sessions/{session['id']}/upload").json()
        assert progress["status"] == "staging"
        assert {f["path"]: f["received"] for f in progress["files"]} == {"a": 4, "b": 0}
        bob.put_chunk(session["id"], "b", 0, b"efgh")
        assert bob.post(f"/sessions/{session['id']}/complete").json()["status"] == "committed"

    def test_crash_after_install_before_commit_recovers_on_retry(self, hub, tmp_path, monkeypatch):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, session_files(), complete=False)
        store = hub.app.state.hub.store
        real = store.install_session

        def install_then_die(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("simulated crash after the rename")

        monkeypatch.setattr(store, "install_session", install_then_die)
        crashed = bob.post(f"/sessions/{session['id']}/complete")
        assert crashed.status_code == 500
        assert status_of(hub, session["id"]).status == "sealing"
        assert bob.put_chunk(session["id"], "session.json", 0, b"x").status_code == 409
        monkeypatch.setattr(store, "install_session", real)
        receipt = bob.post(f"/sessions/{session['id']}/complete").json()
        assert receipt["status"] == "committed"

    def test_database_failure_at_commit_leaves_nothing_acknowledged(
        self, hub, tmp_path, monkeypatch
    ):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, session_files(), complete=False)
        real = uploads.audit

        def fail_on_commit(conn, now, actor, action, target, detail):
            if action == "session.commit":
                raise RuntimeError("simulated database failure inside the commit transaction")
            return real(conn, now, actor, action, target, detail)

        monkeypatch.setattr(uploads, "audit", fail_on_commit)
        assert bob.post(f"/sessions/{session['id']}/complete").status_code == 500
        row = status_of(hub, session["id"])
        assert row.status == "sealing" and row.completed_at is None
        assert bob.get("/data/sessions").json()["items"] == []
        # The durable final exists without a pointer; reconciliation commits it.
        with hub.app.state.hub.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session["id"])
                .values(seal_lease_until=hub.clock.now - 1)
            )
        monkeypatch.setattr(uploads, "audit", real)
        hub.maintenance.run_once(force_reconcile=True)
        assert hub.maintenance.report["seals_resumed"] == 1
        assert status_of(hub, session["id"]).status == "committed"

    def test_an_existing_final_is_verified_never_overwritten(self, hub, tmp_path, monkeypatch):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        store = hub.app.state.hub.store
        final = store.final_dir(eid, session["id"])
        (final / "files").mkdir(parents=True)
        (final / "files" / "a").write_bytes(b"ZZZZ")
        (final / "manifest.json").write_text("{}", encoding="utf-8")
        response = bob.post(f"/sessions/{session['id']}/complete")
        assert (
            response.status_code == 500 and response.json()["error"]["code"] == "artifact_conflict"
        )
        assert (final / "files" / "a").read_bytes() == b"ZZZZ"
        assert status_of(hub, session["id"]).status == "sealing"

    def test_sealing_blocks_more_chunks_and_a_second_sealer(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        with hub.app.state.hub.db.transaction() as conn:
            conn.execute(
                update(data_sessions)
                .where(data_sessions.c.id == session["id"])
                .values(status="sealing", seal_lease_until=hub.clock.now + 60_000)
            )
        busy = bob.post(f"/sessions/{session['id']}/complete")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "sealing_in_progress"
        assert "retry-after" in busy.headers
        assert bob.put_chunk(session["id"], "a", 0, b"abcd").status_code == 409
        hub.clock.advance(61)
        assert bob.post(f"/sessions/{session['id']}/complete").json()["status"] == "committed"

    def test_committed_session_refuses_chunks_and_abort(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        receipt = bob.upload_session(eid, vid, {"a": b"abcd"})
        assert bob.put_chunk(receipt["id"], "a", 0, b"abcd").status_code == 409
        assert bob.post(f"/sessions/{receipt['id']}/abort").status_code == 409

    def test_reconciliation_reports_missing_and_orphaned_artifacts(self, hub, tmp_path):
        import shutil

        _ada, bob, eid, vid = ready(hub, tmp_path)
        receipt = bob.upload_session(eid, vid, {"a": b"abcd"})
        hub.maintenance.run_once(force_reconcile=True)
        assert hub.client.get(f"{API}/readyz").status_code == 200
        store = hub.app.state.hub.store
        shutil.rmtree(store.final_dir(eid, receipt["id"]))
        (store.final_dir(eid, "f" * 32)).mkdir(parents=True)
        hub.maintenance.run_once(force_reconcile=True)
        report = hub.maintenance.report
        assert report["missing_artifacts"] == 1 and report["orphan_artifacts"] == 1
        ready_response = hub.client.get(f"{API}/readyz")
        assert ready_response.status_code == 503
        assert str(hub.settings.artifact_root) not in ready_response.text
        assert store.final_dir(eid, "f" * 32).exists()  # reported, never deleted


class TestAdmissionAndQuota:
    def test_quota_counts_reservations_and_packages(self, tmp_path, clock):
        tight = make_settings(tmp_path, user_quota_bytes=5000)
        admin.init_database(tight)
        service = Hub(tight, clock)
        _ada, bob, eid, vid = ready(service, tmp_path)
        big = {"a": b"x" * 4000}
        assert bob.post("/sessions/init", init_body(eid, vid, big, "r1")).status_code == 201
        over = bob.post("/sessions/init", init_body(eid, vid, {"b": b"y" * 1500}, "r2"))
        assert over.status_code == 413 and over.json()["error"]["code"] == "quota_exceeded"

    def test_active_upload_limit_and_abort_releases(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        ids = [
            bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, f"r{n}")).json()["id"]
            for n in range(3)
        ]
        fourth = bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, "r4"))
        assert fourth.status_code == 429 and fourth.json()["error"]["code"] == "upload_limit"
        aborted = bob.post(f"/sessions/{ids[0]}/abort").json()
        assert aborted["status"] == "aborted"
        assert not (hub.settings.artifact_root / "staging" / ids[0]).exists()
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"}, "r4")).status_code == 201
        assert bob.put_chunk(ids[0], "a", 0, b"a").status_code == 410

    def test_session_caps(self, tmp_path, clock):
        tight = make_settings(tmp_path, max_session_files=2, max_session_bytes=10)
        admin.init_database(tight)
        service = Hub(tight, clock)
        _ada, bob, eid, vid = ready(service, tmp_path)
        files = {"a": b"1", "b": b"2", "c": b"3"}
        assert bob.post("/sessions/init", init_body(eid, vid, files)).status_code == 413
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"x" * 11})).status_code == 413

    def test_stale_uploads_expire_and_release(self, hub, tmp_path):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        session = bob.upload_session(eid, vid, {"a": b"abcd"}, complete=False)
        hub.clock.advance(7 * 86400 + 1)
        bob = hub.bearer("bob")  # the first token has long expired too
        assert bob.put_chunk(session["id"], "a", 0, b"abcd").status_code == 410
        hub.maintenance.run_once()
        assert status_of(hub, session["id"]).status == "expired"
        assert not (hub.settings.artifact_root / "staging" / session["id"]).exists()
        assert bob.post(f"/sessions/{session['id']}/complete").status_code == 410

    @pytest.mark.parametrize(
        "change",
        [
            {"consent": False},
            {"metadata": {"subject_code": "S01", "participant_name": "x"}},
            {"client_session_id": "has space"},
            {"files": [{"path": "/abs", "size": 1, "sha256": "0" * 64}]},
            {"files": [{"path": "a/../b", "size": 1, "sha256": "0" * 64}]},
            {
                "files": [
                    {"path": "A", "size": 1, "sha256": "0" * 64},
                    {"path": "a", "size": 1, "sha256": "0" * 64},
                ]
            },
            {
                "files": [
                    {"path": "a", "size": 1, "sha256": "0" * 64},
                    {"path": "a/b", "size": 1, "sha256": "0" * 64},
                ]
            },
            {"files": [{"path": "a", "size": -1, "sha256": "0" * 64}]},
            {"files": [{"path": "a", "size": 1, "sha256": "XYZ"}]},
            {"files": []},
        ],
    )
    def test_init_validation(self, hub, tmp_path, change):
        _ada, bob, eid, vid = ready(hub, tmp_path)
        body = {**init_body(eid, vid, {"a": b"a"}), **change}
        assert bob.post("/sessions/init", body).status_code == 400

    def test_upload_needs_a_version_the_collector_may_use(self, hub, tmp_path):
        ada, bob, eid, vid = ready(hub, tmp_path, publish=False)
        assert bob.post("/sessions/init", init_body(eid, vid, {"a": b"a"})).status_code == 404
        assert ada.post("/sessions/init", init_body(eid, vid, {"a": b"a"})).status_code == 201


class TestIsolation:
    def test_nobody_else_reaches_an_upload_or_its_data(self, hub, tmp_path):
        ada, bob, eid, vid = ready(hub, tmp_path)
        hub.register("eve")
        eve = hub.bearer("eve")
        pending = bob.upload_session(eid, vid, {"p": b"pp"}, client_id="pending", complete=False)
        done = bob.upload_session(eid, vid, session_files(), client_id="done")
        hub.maintenance.drain_index()
        for intruder in (ada, eve):  # ada wrote the public code; eve is a stranger
            assert intruder.get(f"/sessions/{pending['id']}/upload").status_code == 404
            assert intruder.put_chunk(pending["id"], "p", 0, b"pp").status_code == 404
            assert intruder.post(f"/sessions/{pending['id']}/complete").status_code == 404
            assert intruder.post(f"/sessions/{pending['id']}/abort").status_code == 404
            assert intruder.get("/data/sessions").json()["items"] == []
            for path in ("", "/trials", "/export?format=csv", "/files?path=session.json"):
                assert intruder.get(f"/data/sessions/{done['id']}{path}").status_code == 404
            assert intruder.post(f"/data/sessions/{done['id']}/reindex").status_code == 404
            same_client_id = intruder.post(
                "/sessions/init", init_body(eid, vid, {"z": b"z"}, "done")
            )
            assert same_client_id.status_code == 201  # client ids are per collector
