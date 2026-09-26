"""Loopback-only HTTP entry point for the experiment workspace."""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from alhazen.cli.console_break import interrupt_on_console_break
from alhazen.cli.workspace import MEDIA_TYPES, Launch, Workspace, parse_parameters, path_inside
from alhazen.errors import AlhazenError

ASSETS = Path(__file__).with_name("assets")


@contextmanager
def workspace_lock(directory: Path) -> Iterator[None]:
    """Hold an OS lock, released even on a crash, before recovering history."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "server.lock").open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.write(b"\0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"A dashboard already has this workspace open: {directory}") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, workspace: Workspace, port: int = 0):
        self.workspace = workspace
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/#token={self.token}"


class RequestTooLarge(ValueError):
    """A declared body over the limit: answered 413, and never read."""


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    # Socket timeout for one connection. A client that declares a longer
    # Content-Length than it sends would otherwise hold rfile.read — and this
    # handler thread — for as long as it stays connected. Generous for a
    # loopback client; one that stalls this long has gone away.
    timeout = 30

    def log_message(self, *args: Any) -> None:
        # Request paths may carry a media token; never put it in console logs.
        pass

    def _headers(self, status: int, kind: str, size: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; "
            "media-src 'self'; style-src 'self'; script-src 'self'; "
            # The page embeds the live session monitor, which the run serves
            # on another loopback port; nothing else may be framed.
            "connect-src 'self'; frame-src http://127.0.0.1:*; "
            "frame-ancestors 'none'; base-uri 'none'",
        )

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, allow_nan=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _request(self) -> tuple[str, dict[str, list[str]]]:
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            # Almost always someone who typed localhost:PORT; tell them the
            # URL that works rather than leaving "Invalid host" to decode.
            raise PermissionError(
                f"Invalid host header: open the dashboard at the printed http://{host} URL, "
                "not through another name such as localhost"
            )
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise PermissionError("Cross-origin requests are not allowed")
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()):
            raise ValueError("Query parameters may only appear once")
        if path.startswith(("/api/", "/media/")):
            token = self.headers.get("X-Alhazen-Token", query.get("token", [""])[0])
            if not hmac.compare_digest(token.encode(), self.server.token.encode()):
                raise PermissionError(
                    "Open the dashboard using the URL printed by alhazen dashboard"
                )
        return path, query

    def do_GET(self) -> None:
        try:
            path, query = self._request()
            workspace = self.server.workspace
            if path == "/api/state":
                self._json(workspace.state())
            elif path == "/api/schema":
                self._json(workspace.schema(query.get("project", [""])[0]))
            elif path == "/api/config":
                self._json(
                    workspace.config(query.get("project", [""])[0], query.get("path", [""])[0])
                )
            elif path.startswith("/api/runs/"):
                self._json(workspace.detail(path.removeprefix("/api/runs/")))
            elif path.startswith("/media/"):
                parts = path.split("/", 3)
                if len(parts) != 4 or parts[2] not in workspace.runs:
                    raise FileNotFoundError("Unknown run")
                root = workspace.directory / "runs" / parts[2] / "media"
                target = path_inside(root, parts[3])
                if target.suffix.lower() not in MEDIA_TYPES:
                    raise ValueError("Only images and movies are served as media")
                self._file(target, MEDIA_TYPES[target.suffix.lower()], ranges=True)
            elif path in {"/", "/workspace.js", "/workspace_parameters.js", "/workspace.css"}:
                name, kind = {
                    "/workspace_parameters.js": (
                        "workspace_parameters.js",
                        "text/javascript; charset=utf-8",
                    ),
                    "/": ("workspace.html", "text/html; charset=utf-8"),
                    "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
                    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
                }[path]
                self._file(ASSETS / name, kind)
            else:
                self._json({"error": "Not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # The client went away mid-response — a closed tab, a cancelled
            # fetch, a media stream nobody read for longer than `timeout`.
            # There is nobody left to answer, and nothing to record.
            pass
        except PermissionError as exc:
            self._json({"error": str(exc)}, 403)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except (ValueError, OSError, AlhazenError) as exc:
            self._json({"error": str(exc)}, 400)

    def _body(self) -> dict[str, Any]:
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            raise ValueError("One Content-Length is required")
        length = int(lengths[0])
        if length < 0 or length > 1024 * 1024:
            raise RequestTooLarge("Request must be at most 1 MiB")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("Content-Type must be application/json")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        return body

    def do_POST(self) -> None:
        try:
            # The body is read before the request is judged. Refusing with its
            # bytes still unread in the socket makes Windows reset the
            # connection on close, and the browser then reports "Failed to
            # fetch" in place of the 403 and its reason (the tests saw it as
            # WinError 10053 on the auth refusals).
            body = self._body()
            path, _ = self._request()
            workspace = self.server.workspace
            if path == "/api/projects":
                if not isinstance(body.get("path"), str) or not isinstance(
                    body.get("python", ""), str
                ):
                    raise ValueError("Project path and interpreter must be strings")
                self._json(workspace.add(body["path"], body.get("python", "")), 201)
            elif path == "/api/projects/remove":
                workspace.remove(body.get("id", ""))
                self._json({"ok": True})
            elif path == "/api/parameters":
                if not isinstance(body.get("text"), str):
                    raise ValueError("Parameter YAML must be text")
                self._json({"values": parse_parameters(body["text"])})
            elif path == "/api/runs":
                self._json(workspace.start(Launch.model_validate(body)), 201)
            elif path == "/api/stop":
                workspace.stop(body.get("id", ""))
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            # The client went away mid-response: a closed tab, a cancelled
            # fetch. There is nobody left to answer, and nothing to record —
            # whatever the request did (a launch, a stop) is in the workspace
            # state the next poll shows.
            pass
        except RequestTooLarge as exc:
            self._json({"error": str(exc)}, 413)
        except TimeoutError:
            # The declared body never fully arrived (see `timeout`); the
            # connection is still good, so the client is told why.
            self._json({"error": f"The request body did not arrive within {self.timeout} s"}, 408)
        except PermissionError as exc:
            self._json({"error": str(exc)}, 403)
        except (ValueError, OSError, AlhazenError, ValidationError) as exc:
            self._json({"error": str(exc)}, 400)

    def _file(self, path: Path, kind: str, ranges: bool = False) -> None:
        # An open descriptor pins the file whose size and range we send.
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start, end, status = 0, size - 1, 200
            header = self.headers.get("Range") if ranges else None
            if header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
                if match and any(match.groups()):
                    left, right = match.groups()
                    start = int(left) if left else max(0, size - int(right))
                    end = min(size - 1, int(right)) if left and right else size - 1
                else:
                    start = size
                if start >= size or end < start:
                    self._headers(416, kind, 0)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            remaining = end - start + 1
            self._headers(status, kind, remaining)
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            stream.seek(start)
            while remaining:
                block = stream.read(min(256 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)


def serve(args: argparse.Namespace) -> int:
    directory = Path(args.state_dir) if args.state_dir else Path.home() / ".alhazen" / "dashboard"
    with workspace_lock(directory.expanduser().resolve()):
        return _serve(args, directory)


def _serve(args: argparse.Namespace, directory: Path) -> int:
    # The server is stopped the way it stops its runs: Ctrl+C, or on Windows a
    # console break — which must become the KeyboardInterrupt handled below,
    # so the active run is stopped and the workspace lock released rather than
    # both being abandoned by a process that simply vanished.
    interrupt_on_console_break()
    workspace = Workspace(directory)
    for path in args.project:
        workspace.add(path)
    server = DashboardServer(workspace, args.port)
    print(f"Alhazen dashboard: {server.url}", flush=True)
    print(
        f"Workspace: {workspace.directory}\nCtrl+C stops the server and any active run.", flush=True
    )
    if not args.no_browser:
        threading.Timer(0.25, webbrowser.open, args=(server.url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        # Ctrl+C — or the console break armed above — is the documented way
        # to stop the server, not a fault: the finally stops the active run
        # and releases the workspace lock, and a traceback here would read as
        # a crash to the person who just asked it to stop.
        pass
    finally:
        server.server_close()
        workspace.close()
    return 0
