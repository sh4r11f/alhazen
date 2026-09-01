"""Isolated local HTTP runtime for the live dashboard.

The child process owns every socket and every byte of browser rendering. The
experiment process only performs non-blocking queue operations between trials.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import queue
import secrets
import socketserver
import threading
import time
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from alhazen.dashboard.panels import panel_payload
from alhazen.dashboard.spec import DashboardSpec
from alhazen.errors import SessionError

log = logging.getLogger(__name__)

_ALLOWED_COMMANDS = {
    "resume",
    "calibrate",
    "manual_reward",
    "quit",
    "promote_stage",
    "demote_stage",
    "hold_stage",
}


@dataclass(frozen=True)
class DashboardCommand:
    request_id: str
    name: str


class DashboardController:
    """Parent-side lifecycle and IPC facade."""

    def __init__(self, *, port: int = 0, auto_open: bool = True) -> None:
        ctx = mp.get_context("spawn")
        self._updates: Any = ctx.Queue(maxsize=1)
        self._commands: Any = ctx.Queue(maxsize=64)
        self._ready: Any = ctx.Queue(maxsize=1)
        self._stop: Any = ctx.Event()
        self._port = port
        self._auto_open = auto_open
        self._token = secrets.token_urlsafe(32)
        self._process: Any | None = None
        self.url: str | None = None

    def start(self, timeout_s: float = 20.0) -> str:
        """Spawn the server and wait for it to bind.

        The timeout is generous on purpose: it exists to catch a server that
        will never start, not to race a cold interpreter. A spawned child
        re-imports pydantic before it can bind, which on a loaded machine is
        seconds, and a session that refused to start because an import was
        slow would be a worse failure than the one this guards against.
        """
        if self._process is not None:
            assert self.url is not None
            return self.url
        process = mp.get_context("spawn").Process(
            target=_serve,
            args=(
                self._updates,
                self._commands,
                self._ready,
                self._stop,
                self._token,
                self._port,
            ),
            name="alhazen-dashboard",
            daemon=True,
        )
        self._process = process
        process.start()
        try:
            result = self._ready.get(timeout=timeout_s)
        except queue.Empty as e:
            # Say which failure this was. A child that is still running and
            # has not bound is a different problem from one that died on
            # import, and the two need different things looked at.
            fate = (
                "it is still running"
                if process.is_alive()
                else f"it exited with code {process.exitcode}"
            )
            self.stop()
            raise SessionError(
                f"dashboard server did not start within {timeout_s:g} seconds ({fate})"
            ) from e
        if "error" in result:
            self.stop()
            raise SessionError(f"dashboard server failed to start: {result['error']}")
        self.url = f"http://127.0.0.1:{result['port']}/?token={self._token}"
        if self._auto_open and not webbrowser.open(self.url, new=1):
            log.warning("could not open dashboard browser; open %s", self.url)
        return self.url

    def publish(self, state: dict[str, Any]) -> None:
        """Replace any unread snapshot; monitoring must never block a session."""
        if self._process is not None and not self._process.is_alive():
            log.error("dashboard process exited; the experiment will continue without it")
            return
        # One serialisation, not two plus a pickle: the queue carries the
        # JSON the child is going to send anyway, with the revision beside it
        # so the child can answer long-polls without parsing it.
        snapshot = (
            int(state.get("revision", 0)),
            str(state.get("status", "")),
            json.dumps(state, default=str),
        )
        try:
            self._updates.put_nowait(snapshot)
            return
        except queue.Full:
            pass
        with suppress(queue.Empty):
            self._updates.get_nowait()
        try:
            self._updates.put_nowait(snapshot)
        except queue.Full:
            log.warning("dashboard update queue remained full; dropping revision")

    def poll_commands(self) -> list[DashboardCommand]:
        commands: list[DashboardCommand] = []
        while True:
            try:
                item = self._commands.get_nowait()
            except queue.Empty:
                return commands
            commands.append(DashboardCommand(request_id=item["request_id"], name=item["name"]))

    def save(self, figures_dir: Path, state: dict[str, Any]) -> None:
        # Written as UTF-8 explicitly, never in whatever the platform prefers:
        # the page declares that charset and carries µ, Δ and — in its own
        # labels, and `write_text` on Windows defaults to cp1252, which cannot
        # encode them. A saved figure that only exists on Linux is not a
        # record.
        figures_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True, default=str)
        state_path = figures_dir / "dashboard_state.json"
        state_path.write_text(payload + "\n", encoding="utf-8")
        page = page_html(payload.replace("</", "<\\/"))
        (figures_dir / "dashboard.html").write_text(page, encoding="utf-8")

    def alive(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    def stop(self, timeout_s: float = 2.0) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        self._stop.set()
        process.join(timeout_s)
        if process.is_alive():
            process.terminate()
            process.join(timeout_s)


class _State:
    """The child's copy of the latest snapshot, as (revision, JSON text).

    Held as text rather than as a dict: the parent already serialised it, and
    the child's only job with it is to write it to a socket.
    """

    def __init__(self) -> None:
        self.revision = 0
        # Kept beside the payload rather than parsed back out of it: this is
        # the server's authorization boundary (controls are refused unless the
        # session is paused), and it should not depend on re-reading JSON.
        self.status = "starting"
        self.payload = json.dumps({"revision": 0, "status": "starting"})
        self.condition = threading.Condition()

    def set(self, revision: int, status: str, payload: str) -> None:
        with self.condition:
            self.revision = revision
            self.status = status
            self.payload = payload
            self.condition.notify_all()

    def wait_after(self, revision: int, timeout: float = 15.0) -> str:
        with self.condition:
            if self.revision <= revision:
                self.condition.wait(timeout)
            return self.payload


def _serve(updates: Any, commands: Any, ready: Any, stop: Any, token: str, port: int) -> None:
    state = _State()
    seen: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            if path == "/":
                self._send(
                    HTTPStatus.OK,
                    page_html("null"),
                    "text/html; charset=utf-8",
                )
                return
            if path == "/api/state":
                if not self._authorized(query):
                    return
                revision = 0
                for pair in query.split("&"):
                    if pair.startswith("revision="):
                        with suppress(ValueError):
                            revision = int(pair.partition("=")[2])
                payload = state.wait_after(revision)
                self._send(HTTPStatus.OK, payload, "application/json")
                return
            self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/command" or self.headers.get("X-Alhazen-Token") != token:
                self._send(HTTPStatus.FORBIDDEN, "forbidden", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                name, request_id = body["name"], body["request_id"]
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, "invalid command", "text/plain")
                return
            if name not in _ALLOWED_COMMANDS:
                self._send(HTTPStatus.BAD_REQUEST, "unknown command", "text/plain")
                return
            if state.status != "paused":
                self._send(
                    HTTPStatus.CONFLICT,
                    "controls are available only while paused",
                    "text/plain",
                )
                return
            if request_id not in seen:
                try:
                    commands.put_nowait({"name": name, "request_id": request_id})
                except queue.Full:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, "command queue full", "text/plain")
                    return
                seen.add(request_id)
                if len(seen) > 1024:
                    seen.clear()
                    seen.add(request_id)
            self._send(HTTPStatus.ACCEPTED, "accepted", "text/plain")

        def _authorized(self, query: str) -> bool:
            query_token = next(
                (p.partition("=")[2] for p in query.split("&") if p.startswith("token=")), ""
            )
            if query_token == token or self.headers.get("X-Alhazen-Token") == token:
                return True
            self._send(HTTPStatus.FORBIDDEN, "forbidden", "text/plain")
            return False

        def _send(self, status: HTTPStatus, body: str, content_type: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    class Server(ThreadingHTTPServer):
        def server_bind(self) -> None:
            """Bind without asking a name server who 127.0.0.1 is.

            ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` to fill
            in a ``server_name`` this server never reads. That is a reverse
            DNS lookup, and on a machine with a slow or absent resolver — a
            macOS rig with no network, most of all — it blocks for tens of
            seconds while the experimenter waits for a dashboard that has
            already been told which port to use.
            """
            socketserver.TCPServer.server_bind(self)
            self.server_name = "127.0.0.1"
            self.server_port = self.server_address[1]

    try:
        server = Server(("127.0.0.1", port), Handler)
    except Exception as e:
        ready.put({"error": str(e)})
        return
    server.timeout = 0.2
    ready.put({"port": server.server_address[1]})

    def pump() -> None:
        while not stop.is_set():
            try:
                revision, status, payload = updates.get(timeout=0.2)
            except queue.Empty:
                continue
            state.set(revision, status, payload)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    while not stop.is_set():
        server.handle_request()
    server.server_close()


def dashboard_state(
    *,
    revision: int,
    status: str,
    identity: dict[str, Any],
    trials: list[dict[str, Any]],
    events: list[dict[str, Any]],
    spec: DashboardSpec,
    condition_fields: Sequence[str] = (),
    training: dict[str, Any] | None = None,
    message: str | None = None,
    max_rows: int | None = None,
    extra_panels: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Construct the stable wire shape consumed by the bundled frontend.

    Each panel travels with the data it draws, computed by
    :func:`alhazen.dashboard.panels.panel_payload` over the *whole* session —
    never over the truncated echo below, or a long session's cumulative curve
    would start wherever the window happened to begin.

    ``condition_fields`` are the factors the paradigm varies, in the order the
    task names them. They colour the spatial panels and earn accuracy and
    landing panels of their own, so an experiment's own conditions appear on
    its dashboard without being declared twice.

    ``max_rows`` caps how many of the most RECENT trials and events travel as
    that echo. Every update serialises what it sends, so sending the whole
    history after every trial makes a session's publishing cost grow with the
    square of its length. The totals travel alongside as ``n_trials``/
    ``n_events``, and the state written at teardown is produced with no cap at
    all, so the saved copy is the complete record.

    ``extra_panels`` are panels whose data does not come from the trial
    records at all — a live analysis's receptive-field map, computed by the
    session process between trials (task/live.py). Each entry arrives as a
    finished ``{"title", "section", "data"}`` payload in the same wire shapes
    panels.py produces, so the page draws them exactly like every other
    panel. Validated here, loudly: a malformed entry would otherwise render
    as a permanently and inexplicably blank card.
    """
    for panel in extra_panels:
        missing = [key for key in ("title", "data") if key not in panel]
        if missing:
            raise SessionError(
                f"a live-analysis dashboard panel is missing {missing}; each entry of "
                f"panels() must carry title and data (got keys {sorted(panel)})"
            )
    return {
        "revision": revision,
        "status": status,
        "identity": identity,
        "trials": trials if max_rows is None else trials[-max_rows:],
        "events": events if max_rows is None else events[-max_rows:],
        "n_trials": len(trials),
        "n_events": len(events),
        "panels": [
            *(
                {
                    **panel.model_dump(mode="json"),
                    "section": panel.resolved_section,
                    "data": panel_payload(panel, trials, events),
                }
                for panel in spec.resolved_panels(condition_fields)
            ),
            # Live-analysis panels last: the trial-record panels are the ones
            # every session has, and a reader scanning top-to-bottom meets
            # the familiar ones first. Defaulted section: the sidebar groups
            # by it, and an unfiled panel would vanish from every group.
            *({"section": "Live analysis", **panel} for panel in extra_panels),
        ],
        "training": training,
        "message": message,
        "updated_at": time.time(),
    }


_ASSETS = Path(__file__).parent / "assets"


@lru_cache(maxsize=1)
def _page_template() -> str:
    """The page, assembled once from its three source files.

    The stylesheet and the renderer live beside this module as real ``.css``
    and ``.js`` files rather than inside a Python string: a 900-line renderer
    in a string literal cannot be linted, highlighted or usefully diffed. They
    are inlined here — and only here — because the page has to be
    self-contained. The server answers no second request for an asset, and the
    copy saved into ``figures/`` must open from a filesystem with no server at
    all.
    """
    wanted = ("index.html", "dashboard.css", "dashboard.js")
    missing = [name for name in wanted if not (_ASSETS / name).is_file()]
    if missing:
        # An installed package that shipped without its assets would otherwise
        # serve a blank page and look like a browser problem.
        raise SessionError(
            f"dashboard assets are missing from the installed package: {', '.join(missing)} "
            f"(expected under {_ASSETS})"
        )
    return (
        (_ASSETS / "index.html")
        .read_text(encoding="utf-8")
        .replace("__STYLE__", (_ASSETS / "dashboard.css").read_text(encoding="utf-8"))
        .replace("__SCRIPT__", (_ASSETS / "dashboard.js").read_text(encoding="utf-8"))
    )


def page_html(static_state: str) -> str:
    """The dashboard page with its snapshot embedded.

    ``static_state`` is ``"null"`` for the live page, which polls the server,
    and a JSON document for the standalone copy written to ``figures/``.
    """
    return _page_template().replace("__STATIC_STATE__", static_state)
