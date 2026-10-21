"""The self-hosted host: the platform for a lab, on its own server.

``alhazen platform serve --config platform.toml`` serves the same application as the
local host to several people, with accounts (``alhazen platform users ...``), sessions in
an HttpOnly cookie and a CSRF token on every change. Rigs reach it with machine tokens each
person makes on the Machines page (machines.py; bearer tokens, so no cookie and no CSRF). It
runs the platform only: no experiment code runs on the server, and rigs still run sessions
themselves.

Configuration (TOML, ``[server]`` table)::

    [server]
    store = "/srv/alhazen-platform"          # made by `alhazen platform server-init`
    host = "127.0.0.1"                       # where waitress listens
    port = 8770
    public_url = "https://lab.example.org/platform"   # what browsers open
    tls_terminated_by_proxy = true           # required for any non-loopback host
    public_catalogue = false                 # true: browse without signing in
    session_days = 14
    max_upload_mb = 64
    threads = 8
    trusted_proxy = "127.0.0.1"              # the reverse proxy's address, if any

With ``trusted_proxy`` set, waitress takes the client address from the rightmost entry of
``X-Forwarded-For`` (the address the proxy itself saw: one trusted hop), and the scheme from
``X-Forwarded-Proto``, but only on connections that come from that address; it removes
those headers from every other connection. The proxy must append the client address to
``X-Forwarded-For`` (nginx: ``proxy_add_x_forwarded_for``). Sign-in throttling then counts
failures per client, not per proxy.

Startup refuses unsafe combinations instead of warning: a non-loopback ``host`` without
``tls_terminated_by_proxy = true`` (passwords would cross the network in clear), an
``http://`` public URL anywhere but loopback, or a store that is not a platform store.
"""

from __future__ import annotations

import ipaddress
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alhazen.experiment_platform.accounts import Accounts
from alhazen.experiment_platform.errors import AuthError, Forbidden, InvalidRequest
from alhazen.experiment_platform.machines import FILE as MACHINES_FILE
from alhazen.experiment_platform.machines import Machines
from alhazen.experiment_platform.rigstore import FILE as RIGS_FILE
from alhazen.experiment_platform.rigstore import RigStore
from alhazen.experiment_platform.service import PlatformService, Principal
from alhazen.experiment_platform.store import FileStore
from alhazen.experiment_platform.webapp import PlatformApp, Request, capabilities

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on the 3.10 CI job
    import tomli as tomllib

COOKIE = "alhazen_platform_session"
ACCOUNTS_FILE = "accounts.sqlite3"
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")


@dataclass(frozen=True)
class ServerConfig:
    store: Path
    host: str = "127.0.0.1"
    port: int = 8770
    public_url: str = ""
    tls_terminated_by_proxy: bool = False
    public_catalogue: bool = False
    session_days: float = 14
    max_upload_mb: int = 64
    threads: int = 8
    trusted_proxy: str | None = None

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def prefix(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/")

    @property
    def secure(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(path: Path) -> ServerConfig:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InvalidRequest(f"{path.name} could not be read: {error}") from error
    raw = data.get("server")
    if not isinstance(raw, dict):
        raise InvalidRequest(f"{path.name} has no [server] table")
    known = set(ServerConfig.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise InvalidRequest(f"{path.name}: unknown setting(s) {', '.join(unknown)}")
    if "store" not in raw:
        raise InvalidRequest(f"{path.name}: [server] needs store")
    store = Path(raw["store"])
    if not store.is_absolute():
        store = (path.parent / store).resolve()
    config = ServerConfig(**{**raw, "store": store})
    return validate(config)


def validate(config: ServerConfig) -> ServerConfig:
    """``config`` if it is safe to serve, else InvalidRequest saying what to change."""
    if not isinstance(config.port, int) or not 0 < config.port < 65536:
        raise InvalidRequest("port must be between 1 and 65535")
    if not config.public_url:
        config = ServerConfig(
            **{**config.__dict__, "public_url": f"http://{config.host}:{config.port}"}
        )
    parts = urlsplit(config.public_url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        raise InvalidRequest("public_url must be an http(s) URL with no query or fragment")
    if not _loopback(config.host) and not config.tls_terminated_by_proxy:
        raise InvalidRequest(
            f"host {config.host!r} is reachable from the network. The platform does not speak TLS "
            "itself: put it behind a reverse proxy that does and set "
            "tls_terminated_by_proxy = true, or listen on 127.0.0.1"
        )
    if parts.scheme == "http" and not _loopback(parts.hostname or ""):
        raise InvalidRequest(
            "public_url must be https:// unless the platform is used on this machine only"
        )
    if (
        not 1 <= config.max_upload_mb <= 1024
        or not 1 <= config.threads <= 64
        or not 0 < config.session_days <= 365
    ):
        raise InvalidRequest(
            "max_upload_mb (1-1024), threads (1-64) or session_days (up to 365) is out of range"
        )
    return config


class ServerGate:
    kind = "server"

    def __init__(self, config: ServerConfig, accounts: Accounts) -> None:
        self.config = config
        self.accounts = accounts
        self._hosts = {
            urlsplit(config.public_url).netloc.lower(),
            f"{config.host}:{config.port}".lower(),
        }
        if _loopback(config.host):
            self._hosts |= {f"127.0.0.1:{config.port}", f"localhost:{config.port}"}

    def screen(self, request: Request) -> None:
        host = (request.header("Host") or "").lower()
        if host not in self._hosts:
            raise Forbidden("This server does not answer to that host name")
        origin = request.header("Origin")
        if (
            origin is not None
            and request.method in UNSAFE
            and origin.lower() != self.config.origin.lower()
        ):
            raise Forbidden("Cross-site requests are refused")

    def _session(self, request: Request) -> tuple[Principal, str] | None:
        return self.accounts.session(request.cookie(COOKIE))

    def admit(self, request: Request) -> Principal | None:
        found = self._session(request)
        if found is None:
            return None
        principal, csrf = found
        if request.method in UNSAFE and request.header("X-Alhazen-CSRF") != csrf:
            raise Forbidden("The request is missing its CSRF token; reload the page")
        return principal

    def session(self, request: Request, principal: Principal | None) -> dict[str, Any]:
        found = self._session(request)
        viewer = {"uid": principal.uid, "name": principal.name} if principal else None
        caps = capabilities(sign_in=True)
        if principal is None:  # a visitor (public catalogue) only reads
            caps.update(
                fork=False,
                publish=False,
                uploadRevision=False,
                downloadRevision=False,
                shareWithPeople=False,
            )
        return {
            "kind": "server",
            "viewer": viewer,
            "csrf": found[1] if found else None,
            "capabilities": caps,
            "publicCatalogue": self.config.public_catalogue,
        }

    def _cookie(self, value: str, max_age: int) -> tuple[str, str]:
        attrs = [
            f"{COOKIE}={value}",
            f"Path={self.config.prefix or '/'}",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max_age}",
        ]
        if self.config.secure:
            attrs.append("Secure")
        return ("Set-Cookie", "; ".join(attrs))

    def _client(self, request: Request) -> str:
        # REMOTE_ADDR as waitress leaves it: the forwarded client when the connection came
        # from the trusted proxy, the real peer otherwise. Raw forwarding headers are never
        # read here, so a client cannot choose its own throttling bucket.
        return request.client

    def login(
        self, request: Request, body: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        self.screen(request)
        if request.header("X-Alhazen-Platform") != "1":
            raise Forbidden("Sign in from the platform's own page")
        token, csrf, principal = self.accounts.login(
            str(body.get("username", "")), str(body.get("password", "")), self._client(request)
        )
        info = {
            "kind": "server",
            "viewer": {"uid": principal.uid, "name": principal.name},
            "csrf": csrf,
            "capabilities": capabilities(sign_in=True),
            "publicCatalogue": self.config.public_catalogue,
        }
        return info, [self._cookie(token, self.accounts.session_seconds)]

    def principal_for(self, uid: str) -> Principal | None:
        return self.accounts.principal(uid)

    def logout(self, request: Request) -> list[tuple[str, str]]:
        if self._session(request) is None:
            raise AuthError("Not signed in")
        self.accounts.logout(request.cookie(COOKIE))
        return [self._cookie("", 0)]


def build_app(config: ServerConfig) -> PlatformApp:
    store = FileStore(config.store)
    accounts = Accounts(config.store / ACCOUNTS_FILE, session_days=config.session_days)
    service = PlatformService(store, public_catalogue=config.public_catalogue, people=accounts)
    return PlatformApp(
        service,
        ServerGate(config, accounts),
        max_upload=config.max_upload_mb * 1024 * 1024,
        machines=Machines(config.store / MACHINES_FILE),
        rigs=RigStore(config.store / RIGS_FILE),
    )


def serve(
    config: ServerConfig,
) -> None:  # pragma: no cover - exercised by the integration test's subprocess
    try:
        import waitress
    except ImportError as error:
        raise InvalidRequest(
            'The platform server needs the "platform" extra: pip install "alhazen-vision[platform]"'
        ) from error
    app = build_app(config)
    options: dict[str, Any] = {
        "host": config.host,
        "port": config.port,
        "threads": config.threads,
        "url_prefix": config.prefix,
        "ident": "alhazen-platform",
        "max_request_body_size": config.max_upload_mb * 1024 * 1024 + 1024 * 1024,
        "clear_untrusted_proxy_headers": True,
    }
    if config.trusted_proxy:
        options.update(
            trusted_proxy=config.trusted_proxy,
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
        )
    if config.secure:
        options["url_scheme"] = "https"
    print(
        f"Alhazen Experiment Platform server for {config.public_url} "
        f"(listening on {config.host}:{config.port})"
    )
    waitress.serve(app, **options)
