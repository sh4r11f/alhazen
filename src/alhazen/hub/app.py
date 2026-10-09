"""The central hub HTTP service: routing, request parsing, authentication
transport, and the static web interface.

Public entry points:

- ``create_app(settings, *, clock=..., start_maintenance=True) -> FastAPI``:
  refuses to build for a database that is not at the current schema (it never
  creates or migrates one; see `alhazen.hub.admin`).
- ``serve(config_path, host="127.0.0.1", port=8750)``: run it with uvicorn,
  ONE worker process (docs/hub/server.md "Process model").

Hides: HTTP details. Every route reads its body through a bounded reader
(JSON only with ``Content-Type: application/json``), resolves the caller,
applies the write checks below, and calls one service function in a worker
thread. Service functions own every authorization decision.

Write checks (review gate M1):
- Browser (cookie) writes need the exact configured Origin AND the session's
  ``X-CSRF-Token``. Login and register need the exact Origin (no cookie yet).
- Bearer (rig/CLI) requests carry no cookie; a request carrying both a
  cookie and a bearer is refused, never silently resolved.
- ``POST /auth/token`` never sets a cookie and refuses a request with one.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alhazen.hub import auth, catalog, data, trials, uploads
from alhazen.hub.auth import BEARER_KIND, COOKIE_KIND, Principal
from alhazen.hub.context import Clock, Hub, system_clock
from alhazen.hub.database import Database
from alhazen.hub.errors import HubError, invalid, not_found, too_large
from alhazen.hub.maintenance import Maintenance
from alhazen.hub.settings import HubSettings, load_settings
from alhazen.hub.storage import ArtifactStore

log = logging.getLogger(__name__)

API = "/api/hub/v1"
API_VERSION = 1
ASSET_DIR = Path(__file__).parent / "assets"
# The frontend's files, by name (no directory walk), with their types.
ASSETS = {
    "hub.css": "text/css; charset=utf-8",
    "hub_core.js": "text/javascript; charset=utf-8",
    "hub.js": "text/javascript; charset=utf-8",
    "hub_docs.js": "text/javascript; charset=utf-8",
    "hub_docs.css": "text/css; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "fonts/Nunito-latin.woff2": "font/woff2",
    "fonts/OFL.txt": "text/plain; charset=utf-8",
}
PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
API_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"
_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_OFFSET = 10_000_000


def cookie_name(settings: HubSettings) -> str:
    # __Host- binds the cookie to this exact host, path / and HTTPS.
    return "__Host-alhazen_hub" if settings.secure_cookies else "alhazen_hub"


# -- middleware ---------------------------------------------------------------


class SecurityHeaders:
    """Headers every response carries; API responses are never cached."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = str(scope.get("path", "")).startswith(API)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"x-frame-options": b"DENY",
                    b"cross-origin-resource-policy": b"same-origin",
                }
                if is_api:
                    extra[b"cache-control"] = b"no-store"
                    extra[b"content-security-policy"] = API_CSP.encode()
                for name, value in extra.items():
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# -- request helpers ----------------------------------------------------------


def _address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise invalid("Content-Length is malformed")
        if int(declared) > limit:
            raise too_large(f"The request body is larger than {limit} bytes")
    buffer = bytearray()
    async for part in request.stream():
        buffer += part
        if len(buffer) > limit:
            raise too_large(f"The request body is larger than {limit} bytes")
    return bytes(buffer)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


async def _json(request: Request, limit: int) -> dict[str, Any]:
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/json":
        raise HubError(415, "unsupported_media_type", "Send the request body as application/json")
    raw = await _body(request, limit)
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise invalid("The request body is not valid JSON", "invalid_json") from None
    if not isinstance(value, dict):
        raise invalid("The request body must be a JSON object", "invalid_json")
    return value


def _id(value: str, what: str = "Not found") -> str:
    if not _ID.match(value):
        raise not_found(what)
    return value


def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    if not raw.isdigit():
        raise invalid(f"{name} must be a non-negative integer")
    value = int(raw)
    if not low <= value <= high:
        raise invalid(f"{name} must be between {low} and {high}")
    return value


def _page(request: Request) -> tuple[int, int]:
    return _int(request, "limit", 50, 1, 100), _int(request, "offset", 0, 0, _MAX_OFFSET)


# -- the application ----------------------------------------------------------


def create_app(
    settings: HubSettings, *, clock: Clock = system_clock, start_maintenance: bool = True
) -> FastAPI:
    database = Database(settings)
    database.check_schema()
    hub = Hub(
        settings=settings, db=database, store=ArtifactStore(settings.artifact_root), clock=clock
    )
    maintenance = Maintenance(hub)
    cookie = cookie_name(settings)
    allowed = set(settings.allowed_origins)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_maintenance:
            maintenance.start()
        try:
            yield
        finally:
            maintenance.stop()
            database.dispose()

    app = FastAPI(
        title="alhazen experiment hub",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.maintenance = maintenance
    app.add_middleware(SecurityHeaders)

    def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Awaitable[Any]:
        return run_in_threadpool(fn, *args, **kwargs)

    # -- errors -------------------------------------------------------------

    @app.exception_handler(HubError)
    async def _hub_error(_request: Request, exc: HubError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        code = codes.get(exc.status_code, "http_error")
        message = "Not found" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(
            {"error": {"code": code, "message": message}}, status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "The request is malformed"}},
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            {"error": {"code": "internal", "message": "Internal server error"}}, status_code=500
        )

    # -- authentication transport -------------------------------------------

    def _clear_cookie() -> str:
        secure = "; Secure" if settings.secure_cookies else ""
        return f"{cookie}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict{secure}"

    def _set_cookie(raw: str) -> str:
        secure = "; Secure" if settings.secure_cookies else ""
        age = settings.auth.browser_max_seconds
        return f"{cookie}={raw}; Max-Age={age}; Path=/; HttpOnly; SameSite=Strict{secure}"

    def _credentials(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization")
        presented = request.cookies.get(cookie)
        if header is not None and presented is not None:
            raise invalid(
                "Send either a bearer token or a session cookie, not both", "ambiguous_credentials"
            )
        if header is not None:
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise HubError(401, "not_authenticated", "Malformed Authorization header")
            return BEARER_KIND, token.strip()
        if presented is not None:
            return COOKIE_KIND, presented
        return None

    def _resolve(request: Request, *, required: bool) -> Principal | None:
        found = _credentials(request)
        if found is None:
            if required:
                raise HubError(401, "not_authenticated", "Sign in first")
            return None
        kind, raw = found
        principal = auth.authenticate(hub, raw, kind)
        if principal is None:
            if required:
                headers = {"Set-Cookie": _clear_cookie()} if kind == COOKIE_KIND else {}
                raise HubError(
                    401,
                    "not_authenticated",
                    "Your session has ended; sign in again",
                    headers=headers,
                )
            return None
        return principal

    def _origin_ok(request: Request, *, required: bool) -> None:
        origin = request.headers.get("origin")
        if origin is None:
            if required:
                raise HubError(403, "origin_required", "This request must come from the hub's page")
            return
        if origin not in allowed:
            raise HubError(403, "origin_not_allowed", "Requests from this origin are not allowed")

    def _write_check(request: Request, principal: Principal) -> None:
        if principal.kind == COOKIE_KIND:
            _origin_ok(request, required=True)
            token = request.headers.get("x-csrf-token", "")
            if not principal.csrf_token or not auth.same_secret(token, principal.csrf_token):
                raise HubError(403, "csrf_failed", "Missing or wrong X-CSRF-Token; reload the page")
        else:
            _origin_ok(request, required=False)

    async def reader(request: Request) -> Principal | None:
        return await call(_resolve, request, required=False)

    async def member(request: Request) -> Principal:
        principal = await call(_resolve, request, required=True)
        assert principal is not None
        return principal

    async def writer(request: Request) -> Principal:
        principal = await member(request)
        _write_check(request, principal)
        return principal

    # -- service and health -------------------------------------------------

    @app.get(f"{API}/config")
    async def get_config() -> dict[str, Any]:
        return {
            "role": "server",
            "api_version": API_VERSION,
            "registration_mode": "invite",
            "limits": settings.limits.public(),
            "auth": {"cookie": True, "csrf_header": "X-CSRF-Token", "bearer": True},
        }

    @app.get(f"{API}/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get(f"{API}/readyz")
    async def readyz() -> JSONResponse:
        db_ok = await call(database.ping)
        store_ok = await call(hub.store.writable)
        report = maintenance.report
        problems = (report or {}).get("missing_artifacts", 0) + (report or {}).get(
            "seals_failed", 0
        )
        ready = (
            db_ok
            and store_ok
            and report is not None
            and not problems
            and not maintenance.last_error
        )
        body = {
            "status": "ready" if ready else "not_ready",
            "database": "ok" if db_ok else "unavailable",
            "artifacts": "ok" if store_ok else "unwritable",
            "reconciliation": report,
            "maintenance_error": maintenance.last_error,
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get(f"{API}/guide")
    async def guide() -> dict[str, Any]:
        documentation = catalog.documentation_module()
        return {"guide": await call(documentation.global_guide)}

    # -- accounts -----------------------------------------------------------

    @app.post(f"{API}/auth/register", status_code=201)
    async def register(request: Request) -> dict[str, Any]:
        _origin_ok(request, required=True)
        body = await _json(request, settings.limits.max_json_bytes)
        unknown = sorted(set(body) - {"username", "display_name", "password", "invite_code"})
        if unknown:
            raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
        user = await call(
            auth.register,
            hub,
            username=body.get("username"),
            display_name=body.get("display_name"),
            password=body.get("password"),
            invite_code=body.get("invite_code"),
            address=_address(request),
        )
        return {"user": user}

    @app.post(f"{API}/auth/login")
    async def login(request: Request) -> JSONResponse:
        _origin_ok(request, required=True)
        if request.headers.get("authorization") is not None:
            raise invalid("Browser sign-in takes no bearer token", "ambiguous_credentials")
        body = await _json(request, settings.limits.max_json_bytes)
        previous = request.cookies.get(cookie)
        user, raw, expires = await call(
            auth.sign_in,
            hub,
            username=body.get("username"),
            password=body.get("password"),
            address=_address(request),
            kind=COOKIE_KIND,
        )
        if previous:
            old = await call(auth.authenticate, hub, previous, COOKIE_KIND)
            if old is not None:
                await call(auth.revoke_session, hub, old.session_id)
        from alhazen.hub.timefmt import iso

        response = JSONResponse(
            {"user": user, "csrf_token": auth.csrf_for(raw), "expires_at": iso(expires)}
        )
        response.headers.append("Set-Cookie", _set_cookie(raw))
        return response

    @app.post(f"{API}/auth/token")
    async def token(request: Request) -> dict[str, Any]:
        if (
            request.cookies.get(cookie) is not None
            or request.headers.get("authorization") is not None
        ):
            raise invalid(
                "Request a token without a cookie or another token", "ambiguous_credentials"
            )
        _origin_ok(request, required=False)
        body = await _json(request, settings.limits.max_json_bytes)
        user, raw, expires = await call(
            auth.sign_in,
            hub,
            username=body.get("username"),
            password=body.get("password"),
            address=_address(request),
            kind=BEARER_KIND,
        )
        from alhazen.hub.timefmt import iso

        return {"user": user, "access_token": raw, "expires_at": iso(expires)}

    @app.get(f"{API}/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        principal = await member(request)
        return {"user": principal.user, "csrf_token": principal.csrf_token, "kind": principal.kind}

    @app.post(f"{API}/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        principal = await writer(request)
        await call(auth.revoke_session, hub, principal.session_id)
        response = JSONResponse({"ok": True})
        if principal.kind == COOKIE_KIND:
            response.headers.append("Set-Cookie", _clear_cookie())
        return response

    # -- catalogue and experiments -------------------------------------------

    @app.get(f"{API}/catalog")
    async def get_catalog(request: Request) -> dict[str, Any]:
        await reader(request)
        limit, offset = _page(request)
        return await call(
            catalog.catalog, hub, request.query_params.get("query", ""), limit, offset
        )

    @app.get(f"{API}/experiments")
    async def list_experiments(request: Request) -> dict[str, Any]:
        principal = await member(request)
        limit, offset = _page(request)
        return await call(catalog.list_own, hub, principal, limit, offset)

    @app.post(f"{API}/experiments", status_code=201)
    async def create_experiment(request: Request) -> dict[str, Any]:
        principal = await writer(request)
        body = await _json(request, settings.limits.max_json_bytes)
        return await call(catalog.create_experiment, hub, principal, body)

    @app.get(API + "/experiments/{experiment_id}")
    async def get_experiment(request: Request, experiment_id: str) -> dict[str, Any]:
        principal = await reader(request)
        return await call(
            catalog.get_experiment, hub, principal, _id(experiment_id, "Experiment not found")
        )

    @app.patch(API + "/experiments/{experiment_id}")
    async def patch_experiment(request: Request, experiment_id: str) -> dict[str, Any]:
        principal = await writer(request)
        body = await _json(request, settings.limits.max_json_bytes)
        return await call(
            catalog.patch_experiment,
            hub,
            principal,
            _id(experiment_id, "Experiment not found"),
            body,
        )

    @app.post(API + "/experiments/{experiment_id}/versions")
    async def upload_version(request: Request, experiment_id: str) -> JSONResponse:
        principal = await writer(request)
        experiment_id = _id(experiment_id, "Experiment not found")
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/zip":
            raise HubError(415, "unsupported_media_type", "Upload the package as application/zip")
        await call(catalog.precheck_version_upload, hub, principal, experiment_id)
        with hub.transfers.slot():
            temp, digest, size = await _receive_package(request)
            version, created = await call(
                catalog.accept_version, hub, principal, experiment_id, temp, digest, size
            )
        return JSONResponse({"version": version}, status_code=201 if created else 200)

    async def _receive_package(request: Request) -> tuple[Path, str, int]:
        import hashlib

        limit = settings.limits.max_package_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise too_large(f"A package may be at most {limit} bytes")
        temp = hub.store.new_temp()
        digest = hashlib.sha256()
        size = 0
        handle = await call(temp.open, "xb")
        try:
            pending = bytearray()
            async for part in request.stream():
                size += len(part)
                if size > limit:
                    raise too_large(f"A package may be at most {limit} bytes")
                digest.update(part)
                pending += part
                if len(pending) >= 1024 * 1024:
                    await call(handle.write, bytes(pending))
                    pending.clear()
            if pending:
                await call(handle.write, bytes(pending))
            await call(handle.close)
        except BaseException:
            handle.close()
            temp.unlink(missing_ok=True)
            raise
        if size == 0:
            temp.unlink(missing_ok=True)
            raise invalid("The package is empty", "invalid_package")
        return temp, digest.hexdigest(), size

    def _file_response(path: Path, filename: str, sha256: str, media: str) -> FileResponse:
        response = FileResponse(path, media_type=media, filename=filename)
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        response.headers["X-Alhazen-SHA256"] = sha256
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get(API + "/experiments/{experiment_id}/versions/{version_id}/download")
    async def download_version(
        request: Request, experiment_id: str, version_id: str
    ) -> FileResponse:
        principal = await reader(request)
        path, name, digest = await call(
            catalog.version_download,
            hub,
            principal,
            _id(experiment_id, "Version not found"),
            _id(version_id, "Version not found"),
        )
        return _file_response(path, name, digest, "application/zip")

    @app.get(API + "/experiments/{experiment_id}/versions/{version_id}/documentation")
    async def version_documentation(
        request: Request, experiment_id: str, version_id: str
    ) -> dict[str, Any]:
        principal = await reader(request)
        return await call(
            catalog.version_documentation,
            hub,
            principal,
            _id(experiment_id, "Version not found"),
            _id(version_id, "Version not found"),
        )

    @app.post(API + "/experiments/{experiment_id}/publish")
    async def publish(request: Request, experiment_id: str) -> dict[str, Any]:
        principal = await writer(request)
        body = await _json(request, settings.limits.max_json_bytes)
        return await call(
            catalog.publish, hub, principal, _id(experiment_id, "Experiment not found"), body
        )

    @app.post(API + "/experiments/{experiment_id}/unpublish")
    async def unpublish(request: Request, experiment_id: str) -> dict[str, Any]:
        principal = await writer(request)
        return await call(
            catalog.unpublish, hub, principal, _id(experiment_id, "Experiment not found")
        )

    # -- library --------------------------------------------------------------

    @app.get(f"{API}/library")
    async def get_library(request: Request) -> dict[str, Any]:
        principal = await member(request)
        limit, offset = _page(request)
        return await call(catalog.library_list, hub, principal, limit, offset)

    @app.post(f"{API}/library")
    async def add_library(request: Request) -> dict[str, Any]:
        principal = await writer(request)
        body = await _json(request, settings.limits.max_json_bytes)
        return await call(catalog.library_add, hub, principal, body)

    # -- session uploads ------------------------------------------------------

    @app.post(f"{API}/sessions/init")
    async def init_session(request: Request) -> JSONResponse:
        principal = await writer(request)
        body = await _json(request, settings.limits.max_manifest_json_bytes)
        view, created = await call(uploads.init_session, hub, principal, body)
        return JSONResponse(view, status_code=201 if created else 200)

    @app.get(API + "/sessions/{session_id}/upload")
    async def upload_progress(request: Request, session_id: str) -> dict[str, Any]:
        principal = await member(request)
        return await call(uploads.progress, hub, principal, _id(session_id, "Upload not found"))

    @app.put(API + "/sessions/{session_id}/files")
    async def put_chunk(request: Request, session_id: str) -> dict[str, Any]:
        principal = await writer(request)
        session_id = _id(session_id, "Upload not found")
        path = request.query_params.get("path")
        if not path:
            raise invalid("path is required")
        offset = _int(request, "offset", -1, 0, settings.limits.max_session_bytes)
        if offset < 0:
            raise invalid("offset is required")
        chunk_sha = request.headers.get("x-chunk-sha256", "")
        with hub.transfers.slot():
            data_bytes = await _body(request, settings.limits.max_chunk_bytes)
            return await call(
                uploads.put_chunk, hub, principal, session_id, path, offset, data_bytes, chunk_sha
            )

    @app.post(API + "/sessions/{session_id}/complete")
    async def complete(request: Request, session_id: str) -> dict[str, Any]:
        principal = await writer(request)
        result = await call(uploads.complete, hub, principal, _id(session_id, "Upload not found"))
        maintenance.wake()
        return result

    @app.post(API + "/sessions/{session_id}/abort")
    async def abort(request: Request, session_id: str) -> dict[str, Any]:
        principal = await writer(request)
        return await call(
            uploads.abort_session, hub, principal, _id(session_id, "Upload not found")
        )

    # -- data -----------------------------------------------------------------

    def _filter(request: Request, name: str, limit: int) -> str | None:
        value = request.query_params.get(name) or None
        if value is not None and len(value) > limit:
            raise invalid(f"{name} is too long")
        return value

    @app.get(f"{API}/data/sessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        principal = await member(request)
        limit, offset = _page(request)
        return await call(
            data.list_sessions,
            hub,
            principal,
            experiment_id=_filter(request, "experiment_id", 32),
            subject_code=_filter(request, "subject_code", 64),
            mode=_filter(request, "mode", 32),
            limit=limit,
            offset=offset,
        )

    @app.get(API + "/data/sessions/{session_id}")
    async def session_detail(request: Request, session_id: str) -> dict[str, Any]:
        principal = await member(request)
        return await call(data.session_detail, hub, principal, _id(session_id, "Session not found"))

    @app.get(API + "/data/sessions/{session_id}/trials")
    async def session_trials(request: Request, session_id: str) -> dict[str, Any]:
        principal = await member(request)
        limit, offset = _page(request)
        row, columns = await call(
            data.index_state, hub, principal, _id(session_id, "Session not found")
        )
        index = {"status": row.index_status, "rows": int(row.index_rows), "error": row.index_error}
        if row.index_status != "indexed":
            return {"items": [], "next_offset": None, "columns": columns, "index": index}
        items, more = await call(trials.page, hub, row.id, limit, offset)
        return {
            "items": items,
            "next_offset": offset + limit if more else None,
            "columns": columns,
            "index": index,
        }

    @app.get(API + "/data/sessions/{session_id}/export")
    async def export(request: Request, session_id: str) -> StreamingResponse:
        principal = await member(request)
        fmt = request.query_params.get("format", "csv")
        if fmt not in ("csv", "json"):
            raise invalid("format must be csv or json")
        row, columns = await call(
            data.index_state, hub, principal, _id(session_id, "Session not found")
        )
        data.require_indexed(row)
        slot = hub.exports.slot()
        slot.__enter__()

        def guarded(stream: Iterator[bytes]) -> Iterator[bytes]:
            try:
                yield from stream
            finally:
                slot.__exit__(None, None, None)

        try:
            if fmt == "csv":
                sources = await call(_source_count, row.id)
                stream = trials.export_csv(hub, row.id, columns, sources > 1)
                media = "text/csv; charset=utf-8"
            else:
                stream = trials.export_json(hub, row.id, columns)
                media = "application/json"
        except BaseException:
            slot.__exit__(None, None, None)
            raise
        name = f"session-{row.id}-trials.{fmt}"
        return StreamingResponse(
            guarded(stream),
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Cache-Control": "private, no-store",
            },
        )

    def _source_count(session_id: str) -> int:
        from sqlalchemy import func, select

        from alhazen.hub.schema import trial_rows

        with database.transaction() as conn:
            return int(
                conn.execute(
                    select(func.count(func.distinct(trial_rows.c.source_path))).where(
                        trial_rows.c.session_id == session_id
                    )
                ).scalar_one()
            )

    @app.get(API + "/data/sessions/{session_id}/files")
    async def session_file(request: Request, session_id: str) -> FileResponse:
        principal = await member(request)
        path, name, digest = await call(
            data.artifact,
            hub,
            principal,
            _id(session_id, "Session not found"),
            request.query_params.get("path", ""),
        )
        return _file_response(path, name, digest, "application/octet-stream")

    @app.post(API + "/data/sessions/{session_id}/reindex", status_code=202)
    async def reindex(request: Request, session_id: str) -> dict[str, Any]:
        principal = await writer(request)
        session_id = _id(session_id, "Session not found")
        await call(data.index_state, hub, principal, session_id)
        await call(trials.request_reindex, hub, principal.user_id, session_id)
        maintenance.wake()
        return await call(data.session_detail, hub, principal, session_id)

    # -- web interface ----------------------------------------------------------

    def _page_response() -> Response:
        page = ASSET_DIR / "index.html"
        if not page.is_file():
            return Response(
                "The hub's web interface is not part of this build yet (development state). "
                f"The API is served under {API}.\n",
                status_code=503,
                media_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(
            page,
            media_type="text/html; charset=utf-8",
            headers={"Content-Security-Policy": PAGE_CSP, "Cache-Control": "no-cache"},
        )

    @app.get("/")
    async def root() -> Response:
        return _page_response()

    @app.get("/hub")
    async def hub_page() -> Response:
        return _page_response()

    @app.get("/hub/")
    async def hub_page_slash() -> Response:
        return _page_response()

    @app.get("/hub/assets/{name:path}")
    async def asset(name: str) -> Response:
        media = ASSETS.get(name)
        path = ASSET_DIR / name
        if media is None or not path.is_file():
            raise not_found("Not found")
        return FileResponse(path, media_type=media, headers={"Cache-Control": "no-cache"})

    return app


def serve(config_path: Path, host: str = "127.0.0.1", port: int = 8750) -> None:
    """Run the hub with uvicorn as ONE process (the supported pilot model)."""
    import uvicorn

    settings = load_settings(Path(config_path))
    app = create_app(settings)
    trusted = settings.forwarded_allow_ips
    uvicorn.run(
        app,
        host=host,
        port=port,
        workers=1,
        proxy_headers=bool(trusted),
        forwarded_allow_ips=trusted or None,
        server_header=False,
        limit_concurrency=256,
        timeout_keep_alive=15,
    )
