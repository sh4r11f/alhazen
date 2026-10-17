"""The local host: the platform for one person on their own machine.

``alhazen platform local`` serves the platform on the loopback interface only, the way
``alhazen dashboard`` serves the workspace: a random token is minted at each start and
handed to the browser in the URL fragment (never sent over the network, never logged);
every API request must carry it in ``X-Alhazen-Token``; the ``Host`` header must be the
loopback address and port the server bound (so a web page cannot reach it by DNS
rebinding), and a cross-site ``Origin`` is refused. There are no accounts: the person
who started it is the only user, and nothing leaves the machine.

It needs only the standard library: a rig or laptop with plain alhazen installed can
browse, import, fork and change experiments offline.
"""

from __future__ import annotations

import hmac
import secrets
import socketserver
import subprocess
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from alhazen.experiment_platform.errors import AuthError, Forbidden
from alhazen.experiment_platform.service import PlatformService, Principal
from alhazen.experiment_platform.store import FileStore
from alhazen.experiment_platform.webapp import PlatformApp, Request, capabilities

LOCAL_UID = "local"


def default_name() -> str:
    """The name local publications carry: git's user.name when set, else "Local user"."""
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=5
        )
        name = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name[:80] or "Local user"


class LocalGate:
    kind = "local"

    def __init__(self, port: int, token: str, name: str) -> None:
        self.port = port
        self.token = token
        self.principal = Principal(LOCAL_UID, name)

    def _hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def screen(self, request: Request) -> None:
        if request.header("Host") not in self._hosts():
            raise Forbidden("This platform answers only on its loopback address")
        origin = request.header("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self._hosts()}:
            raise Forbidden("Cross-site requests are refused")

    def admit(self, request: Request) -> Principal | None:
        given = request.header("X-Alhazen-Token")
        if (
            given is None
            and request.method == "GET"
            and (request.path.startswith("/api/art/") or request.path.endswith("/archive"))
        ):
            # An <img> or a download link cannot send a header; these two read-only
            # routes accept the token as a query parameter instead (as the dashboard's
            # media routes do).
            given = request.query.get("token")
        if not given or not hmac.compare_digest(given.encode(), self.token.encode()):
            raise AuthError("Open the platform from the link `alhazen platform local` printed")
        return self.principal

    def session(self, request: Request, principal: Principal | None) -> dict[str, Any]:
        return {
            "kind": "local",
            "viewer": {"uid": self.principal.uid, "name": self.principal.name},
            "csrf": None,
            "capabilities": capabilities(sign_in=False),
        }

    def login(
        self, request: Request, body: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        raise Forbidden("The local platform has no accounts; there is nothing to sign in to")

    def logout(self, request: Request) -> list[tuple[str, str]]:
        return []


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


class _ThreadingServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


class LocalPlatform:
    """A running local platform: ``url`` (with the token) and ``shutdown()``."""

    def __init__(self, store: FileStore, *, port: int = 0, name: str | None = None) -> None:
        self.token = secrets.token_urlsafe(32)
        self.service = PlatformService(store, public_catalogue=False)
        self._gate = LocalGate(0, self.token, name or default_name())
        app = PlatformApp(self.service, self._gate)
        self._server = make_server(
            "127.0.0.1", port, app, server_class=_ThreadingServer, handler_class=_QuietHandler
        )
        self._gate.port = self._server.server_port
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/#token={self.token}"

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.25)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.serve_forever, name="alhazen-platform-local", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def run_local(
    store_path: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    name: str | None = None,
    init: bool = False,
    announce: Callable[[str], None] = print,
) -> int:
    store = FileStore(store_path, create=init or not Path(store_path).exists())
    platform = LocalPlatform(store, port=port, name=name)
    announce(f"Alhazen Experiment Platform (local) at {platform.url}")
    announce(f"Store: {Path(store_path)}. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(platform.url)
    try:
        platform.serve_forever()
    except KeyboardInterrupt:
        announce("Stopped.")
    finally:
        platform._server.server_close()
    return 0
