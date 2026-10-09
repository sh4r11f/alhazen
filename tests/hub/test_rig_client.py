"""The rig's hub client and stored sign-in (alhazen.hub.client, credentials):
canonical hub addresses, redirect refusal, error mapping, bounded verified
downloads, owner-only credential files and the epoch fence."""

from __future__ import annotations

import json
import os
import stat
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest
from tests.hub.rig_support import FakeHub

from alhazen.hub.client import HubClient, HubError, api_path, canonical_base, probe_hub
from alhazen.hub.credentials import Connection, Credential, RigState, sign_in


@pytest.fixture
def hub():
    server = FakeHub()
    yield server
    server.close()


class TestCanonicalBase:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("https://Hub.Example.org", "https://hub.example.org"),
            ("https://hub.example.org/", "https://hub.example.org"),
            ("https://hub.example.org:8443/lab/", "https://hub.example.org:8443/lab"),
            ("https://hub.example.org/api/hub/v1", "https://hub.example.org"),
            ("  https://hub.example.org/x  ", "https://hub.example.org/x"),
        ],
    )
    def test_one_spelling_per_hub(self, given, expected):
        assert canonical_base(given) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "hub.example.org",
            "ftp://hub.example.org",
            "https://user:pw@hub.example.org",
            "https://user@hub.example.org",
            "https://hub.example.org/?next=x",
            "https://hub.example.org/#frag",
            "https://hub.example.org/a/../b",
            "https://hub.example.org/a%2f..",
            "https://hub.example.org//x",
            "https://hub.example.org\\x",
            "https://hub.example.org/a b",
            "http://hub.example.org",
            "https://hub.example.org:99999",
        ],
    )
    def test_refused(self, bad):
        with pytest.raises(ValueError):
            canonical_base(bad)

    def test_http_only_for_loopback_and_only_when_allowed(self):
        with pytest.raises(ValueError, match="local development"):
            canonical_base("http://127.0.0.1:8750")
        assert canonical_base("http://127.0.0.1:8750", allow_http_loopback=True) == (
            "http://127.0.0.1:8750"
        )
        assert canonical_base("http://[::1]:8750", allow_http_loopback=True) == "http://[::1]:8750"
        with pytest.raises(ValueError, match="https"):
            canonical_base("http://10.0.0.5", allow_http_loopback=True)


def test_api_path_escapes_every_segment():
    assert api_path("experiments", "a/b", "versions") == "/experiments/a%2Fb/versions"
    for bad in ("", ".", ".."):
        with pytest.raises(ValueError):
            api_path("experiments", bad)


def test_client_only_accepts_built_paths():
    client = HubClient("https://hub.example.org")
    with pytest.raises(ValueError):
        client.url("https://elsewhere.example/x")
    with pytest.raises(ValueError):
        client.url("/x?y=1")


class _Recorder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(inner):  # noqa: N805
                self.seen.append(dict(inner.headers))
                inner.send_response(200)
                inner.send_header("Content-Length", "2")
                inner.end_headers()
                inner.wfile.write(b"{}")

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


def test_redirects_are_refused_and_the_bearer_goes_nowhere_else(hub):
    target = _Recorder()
    try:
        hub.redirect = True
        client = HubClient(hub.base, "tok-secret")
        with pytest.raises(HubError) as caught:
            client.json("GET", "/catalog")
        assert caught.value.code == "hub_redirect"
        with pytest.raises(HubError) as caught:
            sign_in(HubClient(hub.base), "alice", "alice-password-1")
        assert caught.value.code == "hub_redirect"
        assert target.seen == []
    finally:
        target.shutdown()
        target.server_close()


def test_errors_keep_the_hubs_code_and_status(hub):
    client = HubClient(hub.base, "not-a-token")
    with pytest.raises(HubError) as caught:
        client.json("GET", "/auth/me")
    assert (caught.value.status, caught.value.code) == (401, "unauthenticated")
    assert not caught.value.retryable


def test_unreachable_is_a_typed_retryable_error():
    with pytest.raises(HubError) as caught:
        HubClient("http://127.0.0.1:9", timeout=2).json("GET", "/config", authenticated=False)
    assert caught.value.code == "hub_unreachable" and caught.value.retryable


def test_no_bearer_no_request(hub):
    with pytest.raises(HubError) as caught:
        HubClient(hub.base).json("GET", "/catalog")
    assert caught.value.status == 401
    assert hub.requests == []


def test_probe_checks_role_and_version(hub):
    assert probe_hub(hub.base)["role"] == "server"
    assert hub.requests[-1]["auth"] is None


class TestDownload:
    def test_hash_and_size_checked_and_partial_removed(self, hub, tmp_path):
        exp, ver, sha = hub.add_release(b"x" * 100, {"title": "t", "version": "1.0.0"})
        client = HubClient(
            hub.base, sign_in(HubClient(hub.base), "alice", "alice-password-1").token
        )
        path = api_path("experiments", exp, "versions", ver, "download")
        size, digest = client.download(
            path, tmp_path / "ok.zip", max_bytes=100, expected_sha256=sha
        )
        assert (size, digest) == (100, sha)
        with pytest.raises(HubError) as caught:
            client.download(path, tmp_path / "bad.zip", max_bytes=100, expected_sha256="0" * 64)
        assert caught.value.code == "hash_mismatch"
        assert not (tmp_path / "bad.zip").exists()
        with pytest.raises(HubError) as caught:
            client.download(path, tmp_path / "big.zip", max_bytes=50)
        assert caught.value.code == "too_large"
        assert not (tmp_path / "big.zip").exists()

    def test_never_replaces_a_file(self, hub, tmp_path):
        exp, ver, _ = hub.add_release(b"x", {"title": "t", "version": "1.0.0"})
        client = HubClient(
            hub.base, sign_in(HubClient(hub.base), "alice", "alice-password-1").token
        )
        (tmp_path / "there.zip").write_bytes(b"keep")
        with pytest.raises(FileExistsError):
            client.download(
                api_path("experiments", exp, "versions", ver, "download"),
                tmp_path / "there.zip",
                max_bytes=10,
            )
        assert (tmp_path / "there.zip").read_bytes() == b"keep"


class TestRigState:
    def credential(self, base="https://hub.example.org", user="u1", expires=None):
        return Credential(
            base, "tok-1", {"id": user, "username": "a", "display_name": "A"}, expires
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_files_are_owner_only(self, tmp_path):
        state = RigState(tmp_path / "hub")
        state.save_connection(Connection("https://hub.example.org"))
        state.save_credential(self.credential())
        assert stat.S_IMODE(os.stat(tmp_path / "hub").st_mode) == 0o700
        for name in ("connection.json", "credential.json"):
            assert stat.S_IMODE(os.stat(tmp_path / "hub" / name).st_mode) == 0o600

    def test_public_never_holds_the_token(self, tmp_path):
        state = RigState(tmp_path / "hub")
        state.save_connection(Connection("https://hub.example.org"))
        state.save_credential(self.credential())
        public = state.public()
        assert public["state"] == "signed_in"
        assert "tok-1" not in json.dumps(public)

    def test_new_hub_forgets_the_sign_in_and_bumps_the_epoch(self, tmp_path):
        state = RigState(tmp_path / "hub")
        state.save_connection(Connection("https://hub.example.org"))
        state.save_credential(self.credential())
        epoch = state.epoch
        state.save_connection(Connection("https://other.example.org"))
        assert state.credential() is None
        assert state.epoch > epoch
        assert not (tmp_path / "hub" / "credential.json").exists()

    def test_same_hub_keeps_the_sign_in(self, tmp_path):
        state = RigState(tmp_path / "hub")
        state.save_connection(Connection("https://hub.example.org"))
        state.save_credential(self.credential())
        state.save_connection(Connection("https://hub.example.org"))
        assert state.credential() is not None

    def test_expired_or_foreign_credentials_are_dropped(self, tmp_path):
        state = RigState(tmp_path / "hub")
        state.save_connection(Connection("https://hub.example.org"))
        state.save_credential(self.credential(expires="2000-01-01T00:00:00Z"))
        assert state.credential() is None
        state.save_credential(self.credential(base="https://other.example.org"))
        assert state.credential() is None

    def test_rig_id_is_stable(self, tmp_path):
        state = RigState(tmp_path / "hub")
        assert state.rig_id() == RigState(tmp_path / "hub").rig_id()
