"""The platform's HTTP interface: one WSGI application for both standalone hosts.

The application serves the compiled web app and the JSON API of design.md Appendix A.
It knows nothing about who may do what (``PlatformService`` does) or how identity is
proven (a ``Gate`` does): the local host's gate checks a per-launch token, the server's
checks a session cookie and a CSRF token. A request carrying ``Authorization: Bearer`` is a
rig's dashboard with a machine token (machines.py): it may use MACHINE_ROUTES only, on its
owner's own copies. Every response carries a strict
Content-Security-Policy and no-sniff headers; API responses are never cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import traceback
from collections.abc import Callable, Iterable
from http import HTTPStatus
from importlib import resources
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, quote

from alhazen.experiment_platform.errors import (
    AuthError,
    BundleError,
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
    PlatformError,
    StoreError,
)
from alhazen.experiment_platform.machines import Machines
from alhazen.experiment_platform.rigstore import RigStore
from alhazen.experiment_platform.service import PlatformService, Principal

log = logging.getLogger("alhazen.experiment_platform")

MAX_JSON_BYTES = 1024 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
_STATUS = {
    InvalidRequest: 400,
    BundleError: 400,
    AuthError: 401,
    Forbidden: 403,
    NotFound: 404,
    Conflict: 409,
}

StartResponse = Callable[..., Any]
# Explicit, so the same file is served with the same type on every OS (mimetypes reads
# the system's tables, which differ).
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
}


class Gate(Protocol):
    """How a host proves who is asking. ``admit`` raises (AuthError, Forbidden) when a
    request may not proceed at all and returns the principal (None: anonymous)."""

    kind: str

    def screen(self, request: Request) -> None:
        """Refuse a request that must not be served at all (a foreign Host header)."""

    def admit(self, request: Request) -> Principal | None: ...

    def session(self, request: Request, principal: Principal | None) -> dict[str, Any]: ...

    def login(
        self, request: Request, body: dict[str, Any]
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]: ...

    def logout(self, request: Request) -> list[tuple[str, str]]: ...

    def principal_for(self, uid: str) -> Principal | None:
        """The person a machine token belongs to, or None when they may no longer act (a
        disabled account, a user this host does not know)."""


# What a machine token may ask (machines.py): its owner's library, revisions of their own
# copies, reports from the machine, and its own rig configs (rigsync.py).
MACHINE_ROUTES = {
    "forks",
    "bundle_archive",
    "machine_event",
    "rigs_machine_get",
    "rigs_machine_sync",
}
# A machine's sync carries its rig schema and the templates it can start a config from.
MAX_RIG_SYNC_BYTES = 8 * 1024 * 1024


class Request:
    """The parts of a WSGI request the platform reads, with the body bounded."""

    def __init__(self, environ: dict[str, Any], max_body: int) -> None:
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "") or "/"
        self.query = {
            k: v[-1]
            for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()
        }
        self._max_body = max_body
        self._body: bytes | None = None

    def header(self, name: str) -> str | None:
        key = "HTTP_" + name.upper().replace("-", "_")
        if name.lower() == "content-type":
            key = "CONTENT_TYPE"
        if name.lower() == "content-length":
            key = "CONTENT_LENGTH"
        value = self.environ.get(key)
        return value if value not in (None, "") else None

    @property
    def client(self) -> str:
        return str(self.environ.get("REMOTE_ADDR", ""))

    def cookie(self, name: str) -> str | None:
        raw = self.header("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def body(self, limit: int | None = None) -> bytes:
        if self._body is not None:
            return self._body
        limit = self._max_body if limit is None else limit
        try:
            length = int(self.header("Content-Length") or "0")
        except ValueError as error:
            raise InvalidRequest("Content-Length is not a number") from error
        if length > limit:
            raise _TooLarge(f"The request is larger than {limit // (1024 * 1024) or 1} MB")
        self._body = self.environ["wsgi.input"].read(length) if length else b""
        if len(self._body) != length:
            raise InvalidRequest("The request body ended early")
        return self._body

    def json(self, limit: int = MAX_JSON_BYTES) -> dict[str, Any]:
        if not (self.header("Content-Type") or "").startswith("application/json"):
            raise InvalidRequest("Send JSON (Content-Type: application/json)")
        try:
            value = json.loads(self.body(limit) or b"{}")
        except ValueError as error:
            raise InvalidRequest("The request body is not valid JSON") from error
        if not isinstance(value, dict):
            raise InvalidRequest("The request body must be a JSON object")
        return value


class _TooLarge(PlatformError):
    pass


def _web_dir() -> Path:
    return Path(str(resources.files("alhazen.experiment_platform") / "web"))


_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (m, re.compile("^" + p + "$"), name)
    for m, p, name in [
        ("GET", r"/api/session", "session"),
        ("POST", r"/api/login", "login"),
        ("POST", r"/api/logout", "logout"),
        ("GET", r"/api/assets", "assets"),
        ("GET", r"/api/catalogue", "catalogue"),
        ("GET", r"/api/catalogue/(?P<pub>[^/]+)/(?P<exp>[^/]+)/docs", "docs"),
        ("GET", r"/api/bundles/(?P<handle>[^/]+)/meta", "bundle_meta"),
        ("GET", r"/api/bundles/(?P<handle>[^/]+)/chunks/(?P<index>[0-9]{1,6})", "bundle_chunk"),
        ("GET", r"/api/bundles/(?P<handle>[^/]+)/archive", "bundle_archive"),
        ("GET", r"/api/forks", "forks"),
        ("POST", r"/api/forks", "fork"),
        ("GET", r"/api/forks/(?P<fork>[^/]+)/docs", "fork_docs"),
        ("POST", r"/api/forks/(?P<fork>[^/]+)/revisions", "revision"),
        ("POST", r"/api/forks/(?P<fork>[^/]+)/publish", "publish"),
        ("POST", r"/api/forks/(?P<fork>[^/]+)/unpublish", "unpublish"),
        ("DELETE", r"/api/forks/(?P<fork>[^/]+)", "remove"),
        ("GET", r"/api/art/(?P<name>[^/]+)", "art"),
        ("GET", r"/api/machines", "machines"),
        ("POST", r"/api/machines", "machine_add"),
        ("POST", r"/api/machines/events", "machine_event"),
        ("DELETE", r"/api/machines/(?P<machine>[^/]+)", "machine_revoke"),
        ("GET", r"/api/rigs", "rigs"),
        ("GET", r"/api/rigs/machine/(?P<mid>[^/]+)", "rigs_machine_get"),
        ("POST", r"/api/rigs/machine/(?P<mid>[^/]+)/sync", "rigs_machine_sync"),
        ("GET", r"/api/rigs/(?P<mid>[^/]+)", "rigs_get"),
        ("DELETE", r"/api/rigs/(?P<mid>[^/]+)", "rigs_forget"),
        ("PUT", r"/api/rigs/(?P<mid>[^/]+)/default", "rigs_default"),
        ("PUT", r"/api/rigs/(?P<mid>[^/]+)/configs/(?P<cid>[^/]+)", "rigs_put"),
        ("DELETE", r"/api/rigs/(?P<mid>[^/]+)/configs/(?P<cid>[^/]+)", "rigs_delete"),
        ("POST", r"/api/rigs/(?P<mid>[^/]+)/configs/(?P<cid>[^/]+)/resolve", "rigs_resolve"),
    ]
]


class PlatformApp:
    """The WSGI application: ``PlatformApp(service, gate)``."""

    def __init__(
        self,
        service: PlatformService,
        gate: Gate,
        *,
        max_upload: int = 64 * 1024 * 1024,
        web_dir: Path | None = None,
        machines: Machines | None = None,
        rigs: RigStore | None = None,
    ) -> None:
        self.service = service
        self.gate = gate
        self.machines = machines
        self.rigs = rigs
        self.max_upload = max_upload
        self.web_dir = web_dir or _web_dir()
        assets = self.web_dir / "assets.json"
        self._assets = json.loads(assets.read_text(encoding="utf-8")) if assets.is_file() else {}

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        request = Request(environ, self.max_upload)
        try:
            self.gate.screen(request)
            if request.path.startswith("/api/"):
                status, headers, body = self._api(request)
            else:
                status, headers, body = self._static(request)
        except PlatformError as error:
            status, headers, body = self._error(error)
        except Exception:  # noqa: BLE001 - the last line of defence: never leak a traceback
            log.error(
                "Unhandled error on %s %s\n%s", request.method, request.path, traceback.format_exc()
            )
            status, headers, body = self._json(
                500,
                {"error": {"kind": "ServerError", "message": "Something went wrong on the server"}},
            )
        headers = headers + [
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Content-Length", str(len(body))),
        ]
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body] if request.method != "HEAD" else [b""]

    @staticmethod
    def _json(
        status: int, value: Any, extra: list[tuple[str, str]] | None = None
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return (
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store"),
                *(extra or []),
            ],
            body,
        )

    def _machine_caps(self, info: dict[str, Any], *, signed_in: bool) -> dict[str, Any]:
        """The session answer with ``machineTokens`` (and ``rigConfigs``): true when this host
        keeps machine tokens (and rig configs) and someone is signed in to use them."""
        caps = info.get("capabilities")
        if isinstance(caps, dict):
            caps["machineTokens"] = self.machines is not None and signed_in
            caps["rigConfigs"] = self.rigs is not None and signed_in
        return info

    def _machine(self, token: str) -> tuple[Principal, tuple[str, str]]:
        if self.machines is None:
            raise AuthError("This platform keeps no machine tokens")
        found = self.machines.authenticate(token)
        if found is None:
            raise AuthError(
                "This machine token is not valid here (it was revoked, or made on another "
                "platform). Make a new one on the Machines page"
            )
        uid, machine_id, machine_name = found
        principal = self.gate.principal_for(uid)
        if principal is None:
            raise AuthError("The account this machine token belongs to cannot sign in any more")
        return principal, (machine_id, machine_name)

    def _machines_route(
        self,
        name: str,
        request: Request,
        principal: Principal | None,
        machine: tuple[str, str] | None,
        args: dict[str, str],
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        if self.machines is None:
            raise NotFound("This platform keeps no machine tokens")
        if name == "machine_event":
            if machine is None or principal is None:
                raise Forbidden("Only a machine reports its events, with its machine token")
            kept = self.machines.record(principal.uid, machine[0], machine[1], request.json())
            return self._json(200, {"ok": True, "event": kept})
        if principal is None:
            raise AuthError("Sign in to manage your machines")
        if name == "machines":
            return self._json(
                200,
                {
                    "machines": self.machines.active(principal.uid),
                    "events": self.machines.events(principal.uid),
                },
            )
        if name == "machine_add":
            body = request.json()
            record, token = self.machines.create(principal.uid, str(body.get("name", "")))
            return self._json(200, {"machine": record, "token": token})
        self.machines.revoke(principal.uid, args["machine"])
        return self._json(200, {})

    def _rigs_route(
        self,
        name: str,
        request: Request,
        principal: Principal | None,
        machine: tuple[str, str] | None,
        args: dict[str, str],
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """A machine's rig configs (rigstore.py, docs/design/machine-rig-configs.md §4): the
        machine reads and syncs its own record; the person reads and edits theirs."""
        if self.rigs is None:
            raise NotFound("This platform keeps no rig configs")
        if principal is None:
            raise AuthError("Sign in to see your machines' rig configs")
        uid, rigs = principal.uid, self.rigs
        if name == "rigs_machine_get":
            return self._json(200, {"record": rigs.get(uid, args["mid"])})
        if name == "rigs_machine_sync":
            record, results = rigs.push(
                uid,
                args["mid"],
                request.json(MAX_RIG_SYNC_BYTES),
                token=machine[0] if machine else None,
            )
            return self._json(200, {"record": record, "results": results})
        if machine is not None:  # pragma: no cover - MACHINE_ROUTES refuses these first
            raise Forbidden("A machine token syncs its own rig configs only")
        if name == "rigs":
            return self._json(200, {"machines": rigs.list(uid)})
        if name == "rigs_get":
            return self._json(200, {"record": rigs.require(uid, args["mid"])})
        if name == "rigs_forget":
            rigs.forget(uid, args["mid"])
            return self._json(200, {})
        if name == "rigs_default":
            body = request.json()
            return self._json(
                200,
                {
                    "record": rigs.set_default(
                        uid, args["mid"], body.get("config"), body.get("base")
                    )
                },
            )
        if name == "rigs_put":
            record = rigs.put(uid, args["mid"], args["cid"], request.json())
            return self._json(200, {"record": record, "config": record["configs"][args["cid"]]})
        if name == "rigs_delete":
            base = request.query.get("base", "")
            if not base.isdigit():
                raise InvalidRequest("Say which revision you are deleting (?base=<revision>)")
            return self._json(
                200, {"record": rigs.delete(uid, args["mid"], args["cid"], int(base))}
            )
        if name == "rigs_resolve":
            body = request.json()
            return self._json(
                200, {"record": rigs.resolve(uid, args["mid"], args["cid"], body.get("keep"))}
            )
        raise NotFound("No such API route")  # pragma: no cover

    def _error(self, error: PlatformError) -> tuple[int, list[tuple[str, str]], bytes]:
        status = (
            413
            if isinstance(error, _TooLarge)
            else next((s for k, s in _STATUS.items() if isinstance(error, k)), 500)
        )
        if status == 500:
            log.error("Platform error: %s", error)
        message = (
            str(error)
            if status != 500 or isinstance(error, StoreError)
            else "Something went wrong on the server"
        )
        return self._json(status, {"error": {"kind": type(error).__name__, "message": message}})

    # ------------------------------------------------------------------ static
    def _static(self, request: Request) -> tuple[int, list[tuple[str, str]], bytes]:
        if request.method not in ("GET", "HEAD"):
            raise NotFound("Not found")
        path = request.path
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/assets/")
        if name != "index.html" and (
            not path.startswith("/assets/")
            or not re.match(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$", name)
            or ".." in name
        ):
            raise NotFound("Not found")
        file = self.web_dir / name
        if not file.is_file():
            raise NotFound("Not found")
        data = file.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
        if request.header("If-None-Match") == etag:
            return 304, [("ETag", etag), ("Cache-Control", "no-cache")], b""
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        kind = _TYPES.get(suffix, "application/octet-stream")
        return 200, [("Content-Type", kind), ("ETag", etag), ("Cache-Control", "no-cache")], data

    # ------------------------------------------------------------------ API
    def _api(self, request: Request) -> tuple[int, list[tuple[str, str]], bytes]:
        found = [(p.match(request.path), method, n) for method, p, n in _ROUTES]
        matches = [(m, n) for m, method, n in found if m and method == request.method]
        if not matches:
            if any(m for m, _, _ in found):
                return self._json(
                    405, {"error": {"kind": "MethodNotAllowed", "message": "Method not allowed"}}
                )
            raise NotFound("No such API route")
        m, name = matches[0]
        args = m.groupdict()
        if name == "login":
            body = request.json()
            info, headers = self.gate.login(request, body)
            return self._json(200, self._machine_caps(info, signed_in=True), headers)
        machine: tuple[str, str] | None = None
        principal: Principal | None
        bearer = request.header("Authorization") or ""
        if bearer.startswith("Bearer "):
            principal, machine = self._machine(bearer[7:].strip())
            if name not in MACHINE_ROUTES:
                raise Forbidden("A machine token reads its library and reports; it cannot do this")
            if name == "bundle_archive" and not args["handle"].startswith("F~"):
                raise Forbidden("A machine token downloads revisions of your own copies only")
        else:
            principal = self.gate.admit(request)
        svc = self.service
        if name == "session":
            info = self.gate.session(request, principal)
            return self._json(200, self._machine_caps(info, signed_in=principal is not None))
        if name in ("machines", "machine_add", "machine_revoke", "machine_event"):
            return self._machines_route(name, request, principal, machine, args)
        if name.startswith("rigs"):
            return self._rigs_route(name, request, principal, machine, args)
        if name == "logout":
            return self._json(200, {}, self.gate.logout(request))
        if name == "assets":
            return self._json(200, self._assets)
        if name == "catalogue":
            return self._json(200, {"published": svc.catalogue(principal)})
        if name == "docs":
            return self._json(
                200, svc.docs(principal, args["pub"], args["exp"], request.query.get("version"))
            )
        if name == "bundle_meta":
            return self._json(200, svc.bundle_meta(principal, args["handle"]))
        if name == "bundle_chunk":
            text = svc.bundle_chunk(principal, args["handle"], int(args["index"]))
            return (
                200,
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "private, max-age=86400"),
                ],
                text.encode("utf-8"),
            )
        if name == "bundle_archive":
            filename, data = svc.bundle_archive(principal, args["handle"])
            return (
                200,
                [
                    ("Content-Type", "application/zip"),
                    ("Cache-Control", "no-store"),
                    ("Content-Disposition", f'attachment; filename="{quote(filename)}"'),
                ],
                data,
            )
        if name == "art":
            data = svc.art(principal, args["name"])
            kind = mimetypes.guess_type(args["name"])[0] or "application/octet-stream"
            headers = [
                ("Content-Type", kind),
                ("Cache-Control", "private, max-age=31536000, immutable"),
            ]
            if kind == "image/svg+xml":
                headers.append(("Content-Disposition", "inline"))
            return 200, headers, data
        if name == "forks":
            return self._json(200, {"forks": svc.forks(principal)})
        if name == "fork":
            body = request.json()
            fork_id, fork = svc.fork(
                principal, str(body.get("publisher", "")), str(body.get("exp", ""))
            )
            return self._json(200, {"id": fork_id, "fork": fork})
        if name == "fork_docs":
            return self._json(200, svc.fork_docs(principal, args["fork"]))
        if name == "revision":
            kind = request.header("Content-Type") or ""
            if not kind.startswith(
                ("application/zip", "application/octet-stream", "application/x-zip-compressed")
            ):
                raise InvalidRequest("Upload the copy as a .zip archive")
            revision, fork = svc.upload_revision(
                principal, args["fork"], request.body(), request.query.get("message", "")
            )
            return self._json(200, {"revision": revision, "fork": fork})
        if name == "publish":
            return self._json(
                200, {"listing": svc.publish(principal, args["fork"], request.json())}
            )
        if name == "unpublish":
            svc.unpublish(principal, args["fork"])
            return self._json(200, {})
        if name == "remove":
            return self._json(200, {"removed": svc.remove_fork(principal, args["fork"])})
        raise NotFound("No such API route")  # pragma: no cover - every route is handled above


def capabilities(*, sign_in: bool) -> dict[str, bool]:
    return {
        "fork": True,
        "publish": True,
        "uploadRevision": True,
        "downloadRevision": True,
        "changeWithMana": False,
        "runOnMachines": False,
        "newWithMana": False,
        "machines": False,
        "machineTokens": False,
        "rigConfigs": False,
        "signIn": sign_in,
    }
