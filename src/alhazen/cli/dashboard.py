"""Loopback-only HTTP entry point for the experiment workspace."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
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
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import ValidationError

from alhazen.cli.console_break import interrupt_on_console_break
from alhazen.cli.people import Conflict
from alhazen.cli.workspace import (
    MEDIA_TYPES,
    Launch,
    Workspace,
    calibration_picture,
    now,
    parse_parameters,
    path_inside,
)
from alhazen.cli.workspace_data import DataView
from alhazen.cli.workspace_manage import Management
from alhazen.errors import AlhazenError

ASSETS = Path(__file__).with_name("assets")
# The page's own files, by URL: (file under ASSETS, content type). Served to
# anyone who can reach the loopback port, without the token: none of them
# holds anything but the page itself. The font is the workspace's own
# (Nunito, SIL OFL, assets/fonts/OFL.txt), since the page may load nothing
# from outside (the CSP below) and a rig may have no internet.
PAGE_ASSETS = {
    "/": ("workspace.html", "text/html; charset=utf-8"),
    "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
    "/workspace_parameters.js": ("workspace_parameters.js", "text/javascript; charset=utf-8"),
    "/workspace_calibration.js": ("workspace_calibration.js", "text/javascript; charset=utf-8"),
    # Measure rig's checklist and progress (workspace_measure.js, a module
    # any shell mounts into containers it owns).
    "/workspace_measure.js": ("workspace_measure.js", "text/javascript; charset=utf-8"),
    "/workspace_measure.css": ("workspace_measure.css", "text/css; charset=utf-8"),
    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/fonts/Nunito-latin.woff2": ("fonts/Nunito-latin.woff2", "font/woff2"),
    # The Data view (workspace_data.py serves its reads).
    "/workspace_data.js": ("workspace_data.js", "text/javascript; charset=utf-8"),
    "/workspace_data.css": ("workspace_data.css", "text/css; charset=utf-8"),
    # The management pages: Experiments (home), General and History
    # (workspace_manage.py serves their reads and writes).
    "/workspace_manage.js": ("workspace_manage.js", "text/javascript; charset=utf-8"),
    "/workspace_manage.css": ("workspace_manage.css", "text/css; charset=utf-8"),
}
# Written beside the lock by the server that holds it: its process id, when it
# took the workspace and, once bound, the address of its page. The lock alone
# says only that *someone* has the workspace; this says who, so the refusal
# can tell the person which window to go back to or which process to stop.
HOLDER_RECORD = "server.json"
# Every response's policy: the page and its assets, nothing from elsewhere.
PAGE_CSP = (
    "default-src 'self'; img-src 'self'; "
    "media-src 'self'; style-src 'self'; script-src 'self'; font-src 'self'; "
    # The page embeds the live session monitor, which the run serves
    # on another loopback port; nothing else may be framed.
    "connect-src 'self'; frame-src http://127.0.0.1:*; "
    "frame-ancestors 'none'; base-uri 'none'"
)
# A figure from a data folder (/data/file). Shown in an <img>, where no
# policy matters; opened on its own, an SVG is a document that could carry
# script, and `sandbox` gives it an opaque origin with scripts off, so it can
# never act as the workspace.
DATA_FILE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
# A run's saved live monitor page (/data-page/<ticket>, see
# workspace_data.DataView.page_ticket). Its script and style are inline, so
# they are allowed — and nothing else: no requests (connect-src), no frames,
# no forms, no framing of it, images only from itself. The page renders the
# snapshot embedded in it and needs nothing more.
SAVED_PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def record_holder(directory: Path, *, url: str | None = None) -> None:
    """Record this process as the workspace's holder, with its page's address
    once the server has bound a port (the lock is taken before that)."""
    record: dict[str, Any] = {"pid": os.getpid(), "started": now()}
    if url is not None:
        record["url"] = url
    (directory / HOLDER_RECORD).write_text(json.dumps(record, indent=2), encoding="utf-8")


def _holder_note(directory: Path) -> str:
    """Describe the server holding `directory` from its record, for the
    refusal message. A missing record (a holder from before the record
    existed) leaves the message with just the directory; an unreadable one
    is reported rather than passed over, so a corrupt file is noticed."""
    path = directory / HOLDER_RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        return f" (its record {path} could not be read: {exc})"
    if not isinstance(record, dict):
        return f" (its record {path} is not an object)"
    note = f" — process {record.get('pid')}, started {record.get('started')}"
    if record.get("url"):
        note += f", at {record['url']}. Open that address"
    else:
        note += ". Wait for it to finish starting"
    return note


@contextmanager
def workspace_lock(directory: Path) -> Iterator[None]:
    """Hold an OS lock, released even on a crash, before recovering history.

    While held, `server.json` beside it names this process; a second server
    refused the lock reads it to say what to open or stop."""
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
            raise ValueError(
                f"A dashboard already has this workspace open: {directory}"
                f"{_holder_note(directory)}, stop that process, or run with --state-dir "
                "to use another workspace."
            ) from exc
        record_holder(directory)
        try:
            yield
        finally:
            # The record goes before the lock does: a stale one would name a
            # dead process to the next server, which must simply take over.
            (directory / HOLDER_RECORD).unlink(missing_ok=True)
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
        # The Data view's reads of saved sessions (workspace_data.py).
        self.data = DataView(workspace)
        # The management pages' reads and writes (workspace_manage.py).
        self.manage = Management(workspace, self.data)
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

    def _headers(self, status: int, kind: str, size: int, csp: str = PAGE_CSP) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, allow_nan=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status: int, reason: object) -> None:
        """Answer a request that failed with its status and reason — unless
        the client has already gone.

        The refusal goes back on the socket the request came in on. A client
        that closed it (a tab closed mid-request; on Windows the connection is
        then aborted, WinError 10053) makes this write raise in its turn, and
        an exception out of an except clause escapes the handler: socketserver
        then prints its traceback into the dashboard's console, which is what
        the owner saw. Nobody is left to read the reason, so there is nothing
        more to do.
        """
        # No one is left to read the reason (see above), so a write that
        # meets the closed socket ends here.
        with contextlib.suppress(ConnectionError):
            self._json({"error": str(reason)}, status)

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
        # /data/ is the Data view's figures, fetched with the token in the
        # URL like /media/. /data-page/<ticket> is not here on purpose: its
        # ticket is the secret (DataView.page_ticket).
        if path.startswith(("/api/", "/media/", "/data/", "/calibration-picture")):
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
                self._json(
                    workspace.schema(
                        query.get("project", [""])[0], query.get("task", [None])[0] or None
                    )
                )
            elif path == "/api/config":
                self._json(
                    workspace.config(query.get("project", [""])[0], query.get("path", [""])[0])
                )
            elif path == "/api/rig":
                # The Rig menu's summary: the rig as it would run, merged when
                # it extends a shared one (Workspace.rig). `rig` is the menu's
                # value — a project-relative path, or alhazen/<name>.
                self._json(workspace.rig(query.get("project", [""])[0], query.get("rig", [""])[0]))
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
            elif path == "/calibration-picture":
                # One of a project's calibration pictures, for the Rig
                # section's target preview: by name, only one its alhazen
                # listed at registration (workspace.calibration_picture).
                project = workspace.project(query.get("project", [""])[0])
                target = calibration_picture(project, query.get("name", [""])[0])
                self._file(target, "image/png")
            elif path.startswith("/api/data/"):
                self._json(self.server.data.get(path.removeprefix("/api/data/"), query))
            elif path == "/data/file":
                target, kind = self.server.data.file(
                    *(query.get(name, [""])[0] for name in ("project", "root", "run", "name"))
                )
                self._file(target, kind, csp=DATA_FILE_CSP)
            elif path.startswith("/api/manage/"):
                self._json(self.server.manage.get(path.removeprefix("/api/manage/"), query))
            elif path == "/data/download":
                # Any file of a session folder, as a download: never shown
                # in the page's origin (attachment, sandboxing CSP).
                target = self.server.manage.session_file(
                    *(query.get(name, [""])[0] for name in ("project", "root", "run", "name"))
                )
                self._file(
                    target, "application/octet-stream", csp=DATA_FILE_CSP, download=target.name
                )
            elif path.startswith("/data-page/"):
                target = self.server.data.page(path.removeprefix("/data-page/"))
                self._file(target, "text/html; charset=utf-8", csp=SAVED_PAGE_CSP)
            elif path in PAGE_ASSETS:
                name, kind = PAGE_ASSETS[path]
                self._file(ASSETS / name, kind)
            else:
                self._json({"error": "Not found"}, 404)
        except (ConnectionError, TimeoutError):
            # The client went away mid-response — a closed tab, a cancelled
            # fetch, a media stream nobody read for longer than `timeout`.
            # The whole ConnectionError family: BrokenPipeError and
            # ConnectionResetError, and ConnectionAbortedError, which is how
            # Windows reports a client that aborted (WinError 10053). That one
            # used to fall through to the OSError branch below, which wrote a
            # 400 to the dead socket and raised again.
            # There is nobody left to answer, and nothing to record.
            pass
        except PermissionError as exc:
            self._refuse(403, exc)
        except FileNotFoundError as exc:
            self._refuse(404, exc)
        except (ValueError, OSError, AlhazenError) as exc:
            self._refuse(400, exc)

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
            elif path.startswith("/api/manage/"):
                self._json(self.server.manage.post(path.removeprefix("/api/manage/"), body))
            else:
                self._json({"error": "Not found"}, 404)
        except ConnectionError:
            # The client went away mid-response: a closed tab, a cancelled
            # fetch. The whole family, as in do_GET — Windows's
            # ConnectionAbortedError (WinError 10053) included, which used to
            # reach the OSError branch below and its write to the dead socket.
            # There is nobody left to answer, and nothing to record —
            # whatever the request did (a launch, a stop) is in the workspace
            # state the next poll shows.
            pass
        except RequestTooLarge as exc:
            self._refuse(413, exc)
        except TimeoutError:
            # The declared body never fully arrived (see `timeout`); the
            # connection is still good, so the client is told why.
            self._refuse(408, f"The request body did not arrive within {self.timeout} s")
        except PermissionError as exc:
            self._refuse(403, exc)
        except FileNotFoundError as exc:
            self._refuse(404, exc)
        except Conflict as exc:
            # A write made against a record or file that changed meanwhile:
            # nothing was saved, and the client is told to reload.
            self._refuse(409, exc)
        except (ValueError, OSError, AlhazenError, ValidationError) as exc:
            self._refuse(400, exc)

    def _file(
        self,
        path: Path,
        kind: str,
        ranges: bool = False,
        csp: str = PAGE_CSP,
        download: str | None = None,
    ) -> None:
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
                    self._headers(416, kind, 0, csp)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            remaining = end - start + 1
            self._headers(status, kind, remaining, csp)
            if download is not None:
                # The name as RFC 6266 spells a UTF-8 one; never the path.
                self.send_header(
                    "Content-Disposition", f"attachment; filename*=UTF-8''{quote(download)}"
                )
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
    # Now that the port is known, the holder record can carry the address a
    # second `alhazen dashboard` should open instead of starting its own.
    record_holder(workspace.directory, url=server.url)
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
