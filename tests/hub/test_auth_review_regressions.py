"""Regression tests for the auth/admission review of the hub service
(docs/hub/auth-review-fixes.md). Each class turns one review repro into a
test that failed before its fix. Synthetic users, packages and bytes only.

Backends: SQLite by default; set ALHAZEN_HUB_TEST_POSTGRES_URL (see
server_support) to run the same tests against PostgreSQL, where the
concurrency classes matter most. The body-budget and disconnect classes run
the app under a real uvicorn server on a private loopback port.
"""

from __future__ import annotations

import gc
import json
import logging
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from tests.hub.server_support import (
    API,
    ORIGIN,
    PASSWORD,
    Hub,
    init_body,
    make_bundle,
    make_settings,
    sha,
)

from alhazen.hub import admin, auth
from alhazen.hub import app as app_module
from alhazen.hub.context import Gate
from alhazen.hub.schema import auth_attempts, auth_sessions

# -- helpers -------------------------------------------------------------------


def service(hub: Hub) -> Any:
    return hub.app.state.hub


def login(hub: Hub, username: str, password: str, *, token: bool = True) -> Any:
    client = TestClient(hub.app, base_url=ORIGIN, raise_server_exceptions=False)
    if token:
        return client.post(f"{API}/auth/token", json={"username": username, "password": password})
    return client.post(
        f"{API}/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


def burst(hub: Hub, passwords_and_names: list[tuple[str, str]]) -> list[int]:
    """Send every (username, password) attempt at the same instant."""
    statuses: list[int] = [0] * len(passwords_and_names)
    start = threading.Barrier(len(passwords_and_names))

    def attempt(i: int, username: str, password: str) -> None:
        client = TestClient(hub.app, base_url=ORIGIN, raise_server_exceptions=False)
        start.wait()
        statuses[i] = client.post(
            f"{API}/auth/token", json={"username": username, "password": password}
        ).status_code

    threads = [
        threading.Thread(target=attempt, args=(i, name, pw))
        for i, (name, pw) in enumerate(passwords_and_names)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return statuses


def count_rows(hub: Hub, table: Any, *where: Any) -> int:
    with service(hub).db.transaction() as conn:
        return int(conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def served(hub: Hub) -> Iterator[int]:
    """The hub's app under a real single uvicorn server; yields its port."""
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            hub.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
            proxy_headers=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    hub.server_thread_ident = thread.ident  # type: ignore[attr-defined]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def raw_request(port: int, head: str, body: bytes = b"") -> socket.socket:
    conn = socket.create_connection(("127.0.0.1", port), timeout=30)
    conn.sendall(head.encode() + b"\r\n" + body)
    return conn


def status_line(conn: socket.socket) -> str:
    data = b""
    while b"\r\n" not in data:
        part = conn.recv(4096)
        if not part:
            break
        data += part
    return data.split(b"\r\n", 1)[0].decode()


def put_head(port: int, token: str, session_id: str, path: str, length: int, digest: str) -> str:
    return (
        f"PUT {API}/sessions/{session_id}/files?path={path}&offset=0 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\n"
        f"X-Chunk-SHA256: {digest}\r\nContent-Type: application/octet-stream\r\n"
        f"Content-Length: {length}\r\n"
    )


def wait_for(predicate: Any, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def tight_hub(tmp_path: Path, clock: Any, **limits: Any) -> Hub:
    settings = make_settings(tmp_path, **limits)
    admin.init_database(settings)
    return Hub(settings, clock)


def staging_upload(hub: Hub, owner: Any, size: int, client_id: str) -> tuple[str, bytes]:
    """A staging session of the owner's own (private) version with one file."""
    payload = bytes(range(256)) * (size // 256) + b"x" * (size % 256)
    experiment = owner.create_experiment()
    version = owner.upload_version(experiment["id"], make_bundle(hub.tmp)).json()["version"]
    response = owner.post(
        "/sessions/init", init_body(experiment["id"], version["id"], {"f.bin": payload}, client_id)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], payload


@pytest.fixture
def tmp_hub(hub: Hub, tmp_path: Path) -> Hub:
    hub.tmp = tmp_path  # type: ignore[attr-defined]
    return hub


# -- finding 1: atomic sign-in admission -------------------------------------


class TestAtomicAdmission:
    def test_a_burst_of_guesses_is_held_to_the_account_limit(self, hub):
        hub.register("ada")
        statuses = burst(hub, [("ada", f"wrong guess {i:04d}") for i in range(20)])
        counts = Counter(statuses)
        # Before the fix every guess in the burst was hashed (19-20 x 401).
        assert counts[401] <= hub.settings.auth.failures_per_account, counts
        assert counts[401] + counts[429] == 20, counts
        # The account is now throttled, even for the right password.
        assert login(hub, "ada", PASSWORD).status_code == 429

    def test_the_right_password_inside_a_burst_is_throttled_like_the_rest(self, hub):
        hub.register("ada")
        attempts = [("ada", f"wrong guess {i:04d}") for i in range(15)] + [("ada", PASSWORD)]
        statuses = burst(hub, attempts)
        counts = Counter(statuses)
        # Failures stay within the limit wherever the right password lands; a
        # success that settles early frees its pending slot, which is the
        # policy (the window counts failures, not successes).
        assert counts[401] <= hub.settings.auth.failures_per_account, counts
        assert counts[200] <= 1 and counts[401] + counts[429] + counts[200] == 16, counts

    def test_a_burst_across_accounts_is_held_to_the_address_limit(self, hub):
        statuses = burst(hub, [(f"nobody{i:02d}", "wrong password!") for i in range(30)])
        counts = Counter(statuses)
        assert counts[401] <= hub.settings.auth.failures_per_address, counts

    def test_a_success_settles_its_pending_attempt(self, hub):
        hub.register("ada")
        for _ in range(4):
            assert login(hub, "ada", "wrong password!").status_code == 401
        assert login(hub, "ada", PASSWORD).status_code == 200
        assert login(hub, "ada", "wrong password!").status_code == 401  # 5th failure
        assert login(hub, "ada", "wrong password!").status_code == 429
        assert count_rows(hub, auth_attempts, auth_attempts.c.scope == "account") == 6

    def test_no_hashing_slot_withdraws_the_attempt(self, hub):
        hub.register("ada")
        svc = service(hub)
        svc.hashes = Gate("sign-ins", 1, wait_seconds=0.0)
        with svc.hashes.slot():  # every hashing slot busy
            busy = login(hub, "ada", "wrong password!")
        assert busy.status_code == 429 and busy.json()["error"]["code"] == "server_busy"
        failures = auth_attempts.c.success.is_(False)
        assert count_rows(hub, auth_attempts, auth_attempts.c.scope == "account", failures) == 0
        assert count_rows(hub, auth_attempts, auth_attempts.c.scope == "address", failures) == 0


# -- finding 4: reset / disable racing a sign-in ------------------------------


class TestRecoveryRace:
    @pytest.mark.parametrize("action", ["reset", "disable"])
    def test_an_overlapping_old_password_sign_in_does_not_survive(
        self, hub, settings, monkeypatch, action
    ):
        hub.register("ada")
        paused, release = threading.Event(), threading.Event()
        real_audit = auth.audit
        audited = "user.reset_password" if action == "reset" else "user.disable"

        def gated_audit(conn, now, actor, act, target, detail):
            real_audit(conn, now, actor, act, target, detail)
            if act == audited:
                paused.set()
                release.wait(30)

        monkeypatch.setattr(auth, "audit", gated_audit)

        def operator() -> None:
            if action == "reset":
                admin.reset_password(settings, "ada", "operator chosen password", actor="op")
            else:
                admin.disable_user(settings, "ada", actor="op")

        worker = threading.Thread(target=operator)
        worker.start()
        assert paused.wait(30)
        # The operator's transaction is open and not committed. Let it commit
        # while the sign-in below is in flight (it blocks on the row lock on
        # PostgreSQL, on the database lock on SQLite).
        timer = threading.Timer(1.5, release.set)
        timer.start()
        response = login(hub, "ada", PASSWORD)
        worker.join(30)
        timer.cancel()
        release.set()
        assert response.status_code == 401, response.text
        live = count_rows(hub, auth_sessions, auth_sessions.c.revoked_at.is_(None))
        assert live == 0


# -- finding 2 and 6: bounded bodies, owner slots, quiet disconnects ----------


class TestBodyBudgets:
    def test_a_stalled_chunk_body_gets_408_and_frees_its_slots(self, tmp_path, clock):
        hub = tight_hub(tmp_path, clock, body_idle_seconds=1, body_base_seconds=30)
        hub.tmp = tmp_path  # type: ignore[attr-defined]
        hub.register("ada")
        rig = hub.bearer("ada")
        session_id, payload = staging_upload(hub, rig, 1000, "stall")
        svc = service(hub)
        with served(hub) as port:
            started = time.monotonic()
            conn = raw_request(
                port,
                put_head(port, rig.token, session_id, "f.bin", 1000, sha(payload)),
                payload[:10],
            )
            line = status_line(conn)
            elapsed = time.monotonic() - started
            conn.close()
            assert line.startswith("HTTP/1.1 408"), line
            assert elapsed < 10
            assert wait_for(
                lambda: svc.transfers._slots._value == hub.settings.limits.max_concurrent_transfers
            )
            assert svc.owner_transfers.held(rig.user["id"]) == 0
        progress = rig.get(f"/sessions/{session_id}/upload").json()
        assert progress["received_bytes"] == 0

    def test_a_dripping_body_hits_the_overall_budget(self, tmp_path, clock):
        hub = tight_hub(
            tmp_path,
            clock,
            body_idle_seconds=2,
            body_base_seconds=2,
            body_min_bytes_per_second=1024 * 1024 * 1024,
        )
        hub.tmp = tmp_path  # type: ignore[attr-defined]
        hub.register("ada")
        rig = hub.bearer("ada")
        session_id, payload = staging_upload(hub, rig, 1000, "drip")
        with served(hub) as port:
            conn = raw_request(
                port, put_head(port, rig.token, session_id, "f.bin", 1000, sha(payload))
            )
            conn.settimeout(0.5)
            started = time.monotonic()
            line = ""
            for byte in payload:
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    break
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue
                line = data.split(b"\r\n", 1)[0].decode()
                break
            elapsed = time.monotonic() - started
            conn.close()
        assert line.startswith("HTTP/1.1 408"), line
        assert 1.5 < elapsed < 8

    def test_a_stalled_package_upload_gets_408_and_leaves_no_temp_file(self, tmp_path, clock):
        hub = tight_hub(tmp_path, clock, body_idle_seconds=1, body_base_seconds=30)
        hub.register("ada")
        rig = hub.bearer("ada")
        experiment = rig.create_experiment()
        with served(hub) as port:
            conn = raw_request(
                port,
                f"POST {API}/experiments/{experiment['id']}/versions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nAuthorization: Bearer {rig.token}\r\n"
                "Content-Type: application/zip\r\nContent-Length: 100000\r\n",
                b"PK\x03\x04" + b"\0" * 100,
            )
            line = status_line(conn)
            conn.close()
        assert line.startswith("HTTP/1.1 408"), line
        assert list((hub.settings.artifact_root / "tmp").iterdir()) == []

    def test_one_owner_cannot_hold_every_transfer_slot(self, tmp_path, clock, caplog):
        hub = tight_hub(
            tmp_path,
            clock,
            body_idle_seconds=30,
            body_base_seconds=60,
            owner_transfer_wait_seconds=1,
        )
        hub.tmp = tmp_path  # type: ignore[attr-defined]
        hub.register("slow")
        hub.register("ada")
        slow, ada = hub.bearer("slow"), hub.bearer("ada")
        slow_ids = [staging_upload(hub, slow, 4096, f"s{i}") for i in range(2)]
        ada_id, ada_payload = staging_upload(hub, ada, 64, "a1")
        svc = service(hub)
        caplog.set_level(logging.INFO)
        with served(hub) as port:
            held = [
                raw_request(port, put_head(port, slow.token, sid, "f.bin", 4096, sha(p)), p[:8])
                for sid, p in slow_ids
            ]
            assert wait_for(lambda: svc.owner_transfers.held(slow.user["id"]) == 2)
            third = raw_request(
                port,
                put_head(port, slow.token, slow_ids[0][0], "f.bin", 4096, sha(slow_ids[0][1])),
                slow_ids[0][1],
            )
            line = status_line(third)
            third.close()
            assert line.startswith("HTTP/1.1 429"), line
            # Another account still uploads while the slow one is stalled.
            response = ada.put_chunk(ada_id, "f.bin", 0, ada_payload)
            assert response.status_code == 200, response.text
            for conn in held:  # hang up mid-body
                conn.close()
            assert wait_for(lambda: svc.owner_transfers.held(slow.user["id"]) == 0)
            assert wait_for(
                lambda: svc.transfers._slots._value == hub.settings.limits.max_concurrent_transfers
            )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]
        assert any("client disconnected" in r.getMessage() for r in caplog.records)
        for sid, _payload in slow_ids:
            assert slow.get(f"/sessions/{sid}/upload").json()["received_bytes"] == 0

    def test_a_burst_from_one_owner_waits_for_its_own_slots(self, tmp_hub):
        """A rig's back-to-back retries (or two rigs on one account) briefly
        exceed the owner's share: they wait for it instead of failing."""
        tmp_hub.register("ada")
        rig = tmp_hub.bearer("ada")
        session_id, payload = staging_upload(tmp_hub, rig, 64, "burst")
        statuses: list[int] = []
        start = threading.Barrier(6)

        def send() -> None:
            client = TestClient(tmp_hub.app, base_url=ORIGIN, raise_server_exceptions=False)
            start.wait()
            statuses.append(
                client.put(
                    f"{API}/sessions/{session_id}/files",
                    params={"path": "f.bin", "offset": 0},
                    content=payload,
                    headers={**rig.headers(), "X-Chunk-SHA256": sha(payload)},
                ).status_code
            )

        threads = [threading.Thread(target=send) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert sorted(statuses) == [200] * 6, statuses  # one write, five replays
        assert service(tmp_hub).owner_transfers.held(rig.user["id"]) == 0

    def test_owner_gate_is_per_owner(self, hub):
        gate = service(hub).owner_transfers
        with gate.slot("a"), gate.slot("a"):
            with pytest.raises(app_module.HubError) as refused, gate.slot("a"):
                pass
            assert refused.value.status == 429 and refused.value.code == "owner_transfer_limit"
            with gate.slot("b"):
                assert gate.held("b") == 1
        assert gate.held("a") == 0 and gate.held("b") == 0


# -- finding 3: export permit -------------------------------------------------


def indexed_session(hub: Hub) -> tuple[Any, str]:
    from tests.hub.server_support import session_files

    hub.register("ada")
    rig = hub.bearer("ada")
    experiment = rig.create_experiment()
    version = rig.upload_version(experiment["id"], make_bundle(hub.tmp)).json()["version"]
    receipt = rig.upload_session(experiment["id"], version["id"], session_files())
    hub.maintenance.drain_index()
    assert (
        rig.get(f"/data/sessions/{receipt['id']}").json()["session"]["index"]["status"] == "indexed"
    )
    return rig, receipt["id"]


class TestExportPermit:
    def test_a_disconnect_before_streaming_returns_the_permit(self, tmp_hub):
        import asyncio

        rig, session_id = indexed_session(tmp_hub)
        exports = service(tmp_hub).exports._slots
        full = tmp_hub.settings.limits.max_concurrent_exports
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"{API}/data/sessions/{session_id}/export",
            "raw_path": f"{API}/data/sessions/{session_id}/export".encode(),
            "query_string": b"format=csv",
            "headers": [
                (b"host", b"127.0.0.1:8750"),
                (b"authorization", f"Bearer {rig.token}".encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8750),
            "root_path": "",
        }
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}  # the client is already gone

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        gc.disable()
        try:
            for _ in range(full + 2):
                asyncio.run(tmp_hub.app(scope, receive, send))
                assert exports._value == full
        finally:
            gc.enable()
        assert rig.get(f"/data/sessions/{session_id}/export?format=csv").status_code == 200

    def test_early_disconnects_under_uvicorn_do_not_exhaust_exports(self, tmp_hub):
        rig, session_id = indexed_session(tmp_hub)
        exports = service(tmp_hub).exports._slots
        full = tmp_hub.settings.limits.max_concurrent_exports
        head = (
            f"GET {API}/data/sessions/{session_id}/export?format=csv HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\nAuthorization: Bearer {rig.token}\r\nConnection: close\r\n"
        )
        gc.disable()
        try:
            with served(tmp_hub) as port:
                for _ in range(full + 4):
                    conn = raw_request(port, head)
                    conn.shutdown(socket.SHUT_RDWR)
                    conn.close()
                    time.sleep(0.2)
                assert wait_for(lambda: exports._value == full, 5)
                conn = raw_request(port, head)
                assert status_line(conn).startswith("HTTP/1.1 200")
                conn.close()
        finally:
            gc.enable()


class HeldSnapshotStream:
    """Stands in for the export stream: it holds a real pooled database
    connection across its parts, as a snapshot export does, and records which
    thread closes it and in what order relative to each ``next``."""

    def __init__(self, engine: Any, parts: int, delay: float = 0.0, hold: Any = None) -> None:
        self.conn = engine.connect()  # checked out from the pool until close()
        self.parts, self.delay, self.hold = parts, delay, hold
        self.sent = 0
        self.events: list[str] = []
        self.close_threads: list[int | None] = []

    def __iter__(self) -> HeldSnapshotStream:
        return self

    def __next__(self) -> bytes:
        self.events.append("next-start")
        try:
            if self.hold is not None and self.sent == 1:
                self.events.append("held")
                self.hold.wait(30)
            if self.delay:
                time.sleep(self.delay)
            if self.sent >= self.parts:
                raise StopIteration
            self.sent += 1
            return b"x" * 4096
        finally:
            self.events.append("next-end")

    def close(self) -> None:
        self.events.append("close")
        self.close_threads.append(threading.get_ident())
        self.conn.close()


def serve_stream(monkeypatch: Any, factory: Any) -> None:
    """Make the export route stream ``factory()``, whichever export
    constructor this tree has (export_csv here; open_export after the
    server's snapshot exports)."""
    from alhazen.hub import trials

    if hasattr(trials, "open_export"):
        monkeypatch.setattr(trials, "open_export", lambda *_a, **_k: factory())
    monkeypatch.setattr(trials, "export_csv", lambda *_a, **_k: factory())
    monkeypatch.setattr(trials, "export_json", lambda *_a, **_k: factory())


class TestExportStreamClose:
    """The response's finalizer closes the actual stream (finding 3, with the
    snapshot exports): off the event loop, also before the first send, after
    a part still running in a worker thread, never twice, never by GC."""

    def test_disconnect_before_first_send_closes_the_stream_off_the_loop(
        self, tmp_hub, monkeypatch
    ):
        import asyncio

        rig, session_id = indexed_session(tmp_hub)
        svc = service(tmp_hub)
        pool = svc.db.engine.pool
        streams: list[HeldSnapshotStream] = []

        def factory() -> HeldSnapshotStream:
            streams.append(HeldSnapshotStream(svc.db.engine, parts=50))
            return streams[-1]

        serve_stream(monkeypatch, factory)
        baseline = pool.checkedout()
        scope = export_scope(session_id, rig.token)

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def send(_message: dict[str, Any]) -> None:
            return None

        gc.disable()
        try:
            loop_thread = threading.get_ident()
            asyncio.run(tmp_hub.app(scope, receive, send))
        finally:
            gc.enable()
        assert len(streams) == 1
        stream = streams[0]
        assert stream.events.count("close") == 1
        assert stream.close_threads[0] != loop_thread
        assert pool.checkedout() == baseline
        assert svc.exports._slots._value == tmp_hub.settings.limits.max_concurrent_exports

    def test_early_disconnect_under_uvicorn_returns_the_connection(self, tmp_hub, monkeypatch):
        rig, session_id = indexed_session(tmp_hub)
        svc = service(tmp_hub)
        pool = svc.db.engine.pool
        streams: list[HeldSnapshotStream] = []

        def factory() -> HeldSnapshotStream:
            streams.append(HeldSnapshotStream(svc.db.engine, parts=10_000, delay=0.002))
            return streams[-1]

        serve_stream(monkeypatch, factory)
        gc.disable()
        try:
            with served(tmp_hub) as port:
                baseline = pool.checkedout()
                conn = raw_request(port, export_head(session_id, rig.token))
                assert status_line(conn).startswith("HTTP/1.1 200")
                conn.close()  # after the first bytes, long before the end
                assert wait_for(lambda: streams and "close" in streams[0].events)
                assert wait_for(lambda: pool.checkedout() == baseline)
                server_thread = tmp_hub.server_thread_ident
        finally:
            gc.enable()
        stream = streams[0]
        assert stream.sent < stream.parts
        assert stream.events.count("close") == 1
        assert stream.close_threads[0] != server_thread
        assert svc.exports._slots._value == tmp_hub.settings.limits.max_concurrent_exports

    def test_close_waits_for_a_part_running_in_a_worker_thread(self, tmp_hub, monkeypatch):
        import httpx

        rig, session_id = indexed_session(tmp_hub)
        svc = service(tmp_hub)
        hold = threading.Event()
        streams: list[HeldSnapshotStream] = []

        def factory() -> HeldSnapshotStream:
            streams.append(HeldSnapshotStream(svc.db.engine, parts=100, hold=hold))
            return streams[-1]

        serve_stream(monkeypatch, factory)
        gc.disable()
        try:
            with served(tmp_hub) as port:
                conn = raw_request(port, export_head(session_id, rig.token))
                assert status_line(conn).startswith("HTTP/1.1 200")
                assert wait_for(lambda: streams and "held" in streams[0].events)
                conn.close()  # disconnect while next() is blocked in a worker thread
                # The event loop stays free while that part is stuck.
                started = time.monotonic()
                health = httpx.get(f"http://127.0.0.1:{port}{API}/healthz", timeout=5)
                assert health.status_code == 200 and time.monotonic() - started < 2
                time.sleep(0.3)
                assert "close" not in streams[0].events  # not while next() runs
                hold.set()
                assert wait_for(lambda: "close" in streams[0].events)
                assert wait_for(
                    lambda: (
                        svc.exports._slots._value == tmp_hub.settings.limits.max_concurrent_exports
                    )
                )
        finally:
            hold.set()
            gc.enable()
        events = streams[0].events
        assert events.count("close") == 1
        held_end = events.index("next-end", events.index("held"))
        assert events.index("close") > held_end
        assert events[events.index("close") + 1 :].count("next-start") == 0

    def test_guarded_stream_closes_once_and_then_ends(self):
        closed: list[int] = []

        def parts() -> Iterator[bytes]:
            try:
                yield b"a"
                yield b"b"
            finally:
                closed.append(1)

        guarded = app_module.GuardedStream(parts())
        assert next(guarded) == b"a"
        guarded.close()
        guarded.close()
        assert closed == [1]
        assert list(guarded) == []
        never_started = app_module.GuardedStream(parts())
        never_started.close()
        assert closed == [1]  # an unstarted generator has nothing to release
        no_close = app_module.GuardedStream(iter([b"x"]))
        no_close.close()
        assert list(no_close) == []


def export_scope(session_id: str, token: str) -> dict[str, Any]:
    path = f"{API}/data/sessions/{session_id}/export"
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"format=csv",
        "headers": [(b"host", b"127.0.0.1:8750"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8750),
        "root_path": "",
    }


def export_head(session_id: str, token: str) -> str:
    return (
        f"GET {API}/data/sessions/{session_id}/export?format=csv HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n"
    )


# -- finding 5: numbers and JSON ---------------------------------------------


class TestStrictParsing:
    @pytest.mark.parametrize(
        "query",
        ["limit=%C2%B2", "offset=%C2%B9", "limit=%EF%BC%95", "limit=%D9%A3", "limit=1_0"],
    )
    def test_non_ascii_digits_are_refused_not_500(self, hub, query):
        response = hub.client.get(f"{API}/catalog?{query}")
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"

    def test_chunk_offset_must_be_ascii(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        response = rig.put(
            f"/sessions/{'0' * 32}/files?path=a&offset=%C2%B3",
            content=b"x",
            headers={"X-Chunk-SHA256": sha(b"x")},
        )
        assert response.status_code == 400

    def test_digits_helper(self):
        assert app_module._digits("0042") == 42
        for bad in ("²", "٣", "５", "", "-1", "+1", " 1", "1e3", "9" * 19):
            assert app_module._digits(bad) is None

    def test_deep_json_is_400_even_anonymously(self, hub):
        deep = b'{"username":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
        response = hub.client.post(
            f"{API}/auth/login",
            content=deep,
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_json"

    def test_moderately_deep_json_is_refused(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        body = b'{"title":"t","citations":' + b"[" * 40 + b"]" * 40 + b"}"
        response = rig.post(
            "/experiments", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_json"

    def test_repeated_keys_are_refused(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        response = rig.post(
            "/experiments",
            content=b'{"title":"a","title":"b"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_json"


# -- finding 7: one hidden-character rule -------------------------------------

HIDDEN = {
    "right-to-left override": "\u202e",
    "first strong isolate": "\u2068",
    "zero width space": "\u200b",
    "soft hyphen": "\u00ad",
    "line separator": "\u2028",
    "bell": "\x07",
    "C1 control": "\x85",
    "delete": "\x7f",
    "lone surrogate": "\ud800",
}


def escaped(body: dict[str, Any]) -> dict[str, Any]:
    """Request kwargs carrying ``body`` as ASCII JSON (\\u escapes), so even a
    lone surrogate reaches the server as the JSON a client could send."""
    return {
        "content": json.dumps(body, ensure_ascii=True).encode(),
        "headers": {"Content-Type": "application/json"},
    }


class TestHiddenCharacters:
    @pytest.mark.parametrize("field", ["title", "summary", "description", "license", "citations"])
    @pytest.mark.parametrize("name", sorted(HIDDEN))
    def test_metadata_refuses_hidden_characters(self, hub, field, name):
        hub.register("ada")
        rig = hub.bearer("ada")
        text = f"abc{HIDDEN[name]}dcba"
        value: Any = [text] if field == "citations" else text
        body = {"title": "ok", field: value}
        assert rig.post("/experiments", **escaped(body)).status_code == 400
        experiment = rig.create_experiment()
        patch = rig.client.patch(
            f"{API}/experiments/{experiment['id']}",
            content=escaped({field: value})["content"],
            headers={**rig.headers(), "Content-Type": "application/json"},
        )
        assert patch.status_code == 400

    @pytest.mark.parametrize("name", sorted(HIDDEN))
    def test_display_names_follow_the_same_rule(self, hub, name):
        body = {
            "username": "ada",
            "display_name": f"Ad{HIDDEN[name]}a",  # inside: ends are stripped
            "password": PASSWORD,
            "invite_code": hub.invite(),
        }
        response = hub.client.post(
            f"{API}/auth/register",
            content=json.dumps(body, ensure_ascii=True).encode(),
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_ordinary_text_is_accepted(self, hub):
        hub.register("ada")
        rig = hub.bearer("ada")
        experiment = rig.create_experiment(
            title="Sakkaden-Bias über Kulturen 日本語 🧠",
            description="Line one\n\tindented line two",
            citations=["Müller & Ōno (2024). Vision Res."],
        )
        assert experiment["title"].startswith("Sakkaden")


# -- finding 8: bounded session cleanup ---------------------------------------


class TestSessionPurge:
    def test_ended_sessions_are_deleted_after_retention(self, hub):
        hub.register("ada")
        page = hub.browser("ada")
        assert page.post("/auth/logout").status_code == 200  # revoked now
        hub.bearer("ada")  # expires in 12 h
        svc = service(hub)
        retention = hub.settings.auth.session_retention_seconds
        assert auth.purge_sessions(svc) == 0
        hub.clock.advance(retention + 13 * 3600)
        live = hub.bearer("ada")
        assert auth.purge_sessions(svc) == 2
        assert count_rows(hub, auth_sessions) == 1
        assert live.get("/auth/me").status_code == 200

    def test_purge_is_bounded_per_call(self, hub, monkeypatch):
        hub.register("ada")
        for _ in range(5):
            hub.bearer("ada")
        hub.clock.advance(hub.settings.auth.session_retention_seconds + 13 * 3600)
        monkeypatch.setattr(auth, "PURGE_BATCH", 2)
        svc = service(hub)
        assert auth.purge_sessions(svc, max_batches=1) == 2
        assert auth.purge_sessions(svc) == 3
        assert count_rows(hub, auth_sessions) == 0

    def test_housekeeping_runs_it(self, hub):
        hub.register("ada")
        hub.bearer("ada")
        hub.clock.advance(hub.settings.auth.session_retention_seconds + 13 * 3600)
        hub.maintenance.run_once()
        assert count_rows(hub, auth_sessions) == 0
        assert hub.maintenance.last_error is None
