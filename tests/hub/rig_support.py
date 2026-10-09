"""Test support for the rig hub tests: a stand-in central hub over real
loopback HTTP, implementing the parts of docs/hub/api-contract.md the rig
uses (the real service is tested by its own suite; the rig/server seam is
verified in the integration run), and synthetic releases built with the real
``alhazen.hub.packages``. Synthetic users and data only."""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from alhazen.hub import packages

MANIFEST = "alhazen-package.json"


def release_zip(
    tmp: Path,
    name: str = "demo-task",
    version: str = "1.0.0",
    run_py: str = "print('hello')\n",
    platforms: list[str] | None = None,
) -> tuple[bytes, dict]:
    """A synthetic release: run.py and pyproject.toml."""
    source = tmp / f"src-{name}-{version}-{abs(hash(run_py))}"
    source.mkdir(parents=True)
    (source / "run.py").write_text(run_py)
    (source / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')
    metadata = {
        "name": name,
        "version": version,
        "title": "Demo task",
        "description": "",
        "entrypoint": "run.py",
        "python_min": "3.10",
        "alhazen_min": "2.13.0",
        "platforms": platforms or ["linux", "darwin", "win32"],
        "hardware": {"display": True, "eye_tracker": False, "reward": False},
        "license": "MIT",
        "citations": [],
    }
    out = tmp / f"{name}-{version}-{abs(hash(run_py))}.zip"
    info = packages.build_bundle(source, out, metadata, ["pyproject.toml", "run.py"])
    return out.read_bytes(), dict(info.manifest)


ID = r"[A-Za-z0-9_-]+"


class FakeHub(ThreadingHTTPServer):
    """A central hub stand-in. ``users`` maps username -> password."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HubHandler)
        self.users = {"alice": "alice-password-1", "bob": "bob-password-22"}
        self.tokens: dict[str, str] = {}
        self.experiments: dict[str, dict] = {}
        self.versions: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.sessions: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail_puts = 0
        self.redirect = False
        self.receipt_status = "committed"
        self._lock = threading.Lock()
        self._counter = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}{self._counter}"

    def add_release(
        self, zip_bytes: bytes, manifest: dict, owner: str = "alice"
    ) -> tuple[str, str, str]:
        exp = self.next_id("e")
        ver = self.next_id("v")
        sha = hashlib.sha256(zip_bytes).hexdigest()
        self.experiments[exp] = {"id": exp, "owner": owner, "title": manifest["title"]}
        self.versions[ver] = {
            "id": ver,
            "experiment_id": exp,
            "version": manifest["version"],
            "sha256": sha,
            "size": len(zip_bytes),
            "manifest": manifest,
        }
        self.blobs[ver] = zip_bytes
        return exp, ver, sha

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def _user(name: str) -> dict:
    return {"id": f"u-{name}", "username": name, "display_name": name.title()}


class _HubHandler(BaseHTTPRequestHandler):
    server: FakeHub

    def log_message(self, *args: Any) -> None:
        pass

    def _send(
        self,
        status: int,
        payload: Any = None,
        *,
        raw: bytes | None = None,
        kind: str = "application/json",
        extra: dict | None = None,
    ) -> None:
        data = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str = "") -> None:
        self._send(status, {"error": {"code": code, "message": message or code}})

    def _who(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        return self.server.tokens.get(auth[7:])

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.removeprefix("/api/hub/v1")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = self._body()
        hub = self.server
        hub.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "auth": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
                "headers": dict(self.headers),
            }
        )
        if hub.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        who = self._who()
        if method == "GET" and path == "/config":
            return self._send(
                200, {"role": "server", "api_version": 1, "registration_mode": "invite"}
            )
        if method == "POST" and path == "/auth/token":
            data = json.loads(body)
            if hub.users.get(data.get("username")) != data.get("password"):
                return self._error(401, "invalid_credentials")
            token = f"tok-{data['username']}-{hub.next_id('')}"
            hub.tokens[token] = data["username"]
            return self._send(
                200,
                {
                    "user": _user(data["username"]),
                    "access_token": token,
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            )
        if method == "POST" and path == "/auth/register":
            return self._send(201, {"user": _user(json.loads(body)["username"])})
        if who is None:
            return self._error(401, "unauthenticated")
        if method == "GET" and path == "/auth/me":
            return self._send(200, {"user": _user(who), "csrf_token": "x"})
        if method == "POST" and path == "/auth/logout":
            token = self.headers["Authorization"][7:]
            hub.tokens.pop(token, None)
            return self._send(200, {"ok": True})
        if method == "GET" and path == "/catalog":
            return self._send(200, {"items": [], "next_offset": None})
        m = re.fullmatch(rf"/experiments/({ID})", path)
        if m and method == "GET":
            exp = hub.experiments.get(m.group(1))
            if exp is None:
                return self._error(404, "not_found")
            versions = [v for v in hub.versions.values() if v["experiment_id"] == exp["id"]]
            return self._send(200, {"experiment": exp, "versions": versions})
        m = re.fullmatch(rf"/experiments/({ID})/versions", path)
        if m and method == "POST":
            if self.headers.get("Content-Type") != "application/zip":
                return self._error(415, "unsupported")
            ver = hub.next_id("v")
            sha = hashlib.sha256(body).hexdigest()
            manifest = json.loads(zipfile.ZipFile(io.BytesIO(body)).read(MANIFEST))
            hub.versions[ver] = {
                "id": ver,
                "experiment_id": m.group(1),
                "version": manifest["version"],
                "sha256": sha,
                "size": len(body),
                "manifest": manifest,
            }
            hub.blobs[ver] = body
            return self._send(201, {"version": hub.versions[ver]})
        m = re.fullmatch(rf"/experiments/({ID})/versions/({ID})/download", path)
        if m and method == "GET":
            blob = hub.blobs.get(m.group(2))
            if blob is None:
                return self._error(404, "not_found")
            return self._send(
                200,
                raw=blob,
                kind="application/zip",
                extra={"Content-Disposition": 'attachment; filename="../../evil name.zip"'},
            )
        m = re.fullmatch(rf"/data/sessions/({ID})/export", path)
        if m and method == "GET":
            return self._send(
                200,
                raw=b"trial,rt\n1,0.2\n",
                kind="text/html",
                extra={"Content-Disposition": "attachment; filename*=UTF-8''trials.csv"},
            )
        if method == "POST" and path == "/sessions/init":
            data = json.loads(body)
            for s in hub.sessions.values():
                if s["owner"] == who and s["client_session_id"] == data["client_session_id"]:
                    if s["files"] != data["files"]:
                        return self._error(409, "conflict")
                    return self._send(200, {"session": self._progress(s)})
            sid = hub.next_id("s")
            hub.sessions[sid] = {
                "id": sid,
                "owner": who,
                "status": "staging",
                "client_session_id": data["client_session_id"],
                "files": data["files"],
                "metadata": data["metadata"],
                "experiment_id": data["experiment_id"],
                "version_id": data["version_id"],
                "data": {f["path"]: bytearray() for f in data["files"]},
            }
            return self._send(201, {"session": self._progress(hub.sessions[sid])})
        m = re.fullmatch(rf"/sessions/({ID})/(upload|files|complete)", path)
        if m:
            s = hub.sessions.get(m.group(1))
            if s is None or s["owner"] != who:
                return self._error(404, "not_found")
            if m.group(2) == "upload" and method == "GET":
                return self._send(200, self._progress(s))
            if m.group(2) == "files" and method == "PUT":
                if hub.fail_puts > 0:
                    hub.fail_puts -= 1
                    return self._error(503, "busy")
                buf = s["data"][query["path"]]
                offset = int(query["offset"])
                if hashlib.sha256(body).hexdigest() != self.headers.get("X-Chunk-SHA256"):
                    return self._error(400, "bad_chunk")
                if offset == len(buf):
                    buf.extend(body)
                elif (
                    offset + len(body) <= len(buf)
                    and bytes(buf[offset : offset + len(body)]) == body
                ):
                    pass
                else:
                    return self._error(409, "offset_conflict")
                return self._send(200, {"path": query["path"], "received": len(buf)})
            if m.group(2) == "complete" and method == "POST":
                for f in s["files"]:
                    if hashlib.sha256(bytes(s["data"][f["path"]])).hexdigest() != f["sha256"]:
                        return self._error(409, "incomplete")
                s["status"] = hub.receipt_status
                return self._send(
                    200, {"id": s["id"], "status": s["status"], "manifest_sha256": "x"}
                )
        return self._error(404, "not_found")

    def _progress(self, s: dict) -> dict:
        return {
            "id": s["id"],
            "status": s["status"],
            "files": [{**f, "received": len(s["data"][f["path"]])} for f in s["files"]],
        }

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")
