"""The dashboard's Experiment Hub adapter: `/api/hub/v1/*` on the loopback page.

Composes the existing workspace (registration, DataView discovery, the active
run) with the rig-side hub modules (``alhazen.hub.client``, ``credentials``,
``installation``, ``source``, ``sync``). It is the only place that joins the
two, so ``alhazen.hub`` never imports ``alhazen.cli`` (the layering contract
puts ``alhazen.hub`` directly below ``alhazen.cli``).

Imported only when a hub route is first used (dashboard.DashboardServer.hub),
so a dashboard that never opens the hub page loads none of it.

Routes (dashboard.Handler has already checked Host, Origin and the token):

* central routes, same paths as the hub's API: ``/config`` and ``/guide``
  answered here; ``/auth/*`` mapped to the bearer flow; an exact allowlist
  proxied to the ONE configured hub with the stored bearer;
* ``/local/*``: connection, installed releases, source packaging and session
  uploads (shapes in docs/hub/rig.md).
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass
from http.client import HTTPResponse
from pathlib import Path
from typing import IO, Any

from alhazen.cli import workspace as workspace_module
from alhazen.cli.workspace import Workspace
from alhazen.cli.workspace_data import DataView, _run_folder, data_roots
from alhazen.cli.workspace_manage import _console_run_folder
from alhazen.hub.client import (
    DEFAULT_TIMEOUT_S,
    HubClient,
    HubError,
    api_path,
    canonical_base,
    probe_hub,
)
from alhazen.hub.credentials import Connection, Credential, RigState, public_user, sign_in
from alhazen.hub.installation import (
    TRUST_STATEMENT,
    InstallError,
    InstallStore,
    check_identifier,
    check_sha256,
)
from alhazen.hub.source import pack_and_upload
from alhazen.hub.source import preview as source_preview
from alhazen.hub.sync import (
    ACTIVE,
    PRIVACY_WARNING,
    Outbox,
    Previews,
    SyncError,
    Uploader,
    client_session_id,
    file_sha256,
    job_id_for,
    privacy_fields,
    public_job,
    session_files,
    session_metadata,
)

API_PREFIX = "/api/hub/v1"
IDENT = r"[A-Za-z0-9_-]{1,128}"
MAX_QUERY_VALUE = 1024
MAX_ZIP_BYTES = 256 * 1024 * 1024
# What a proxied download may say it is; anything else goes as a plain byte
# stream. It is always an attachment (dashboard.Handler adds a sandbox CSP).
STREAM_TYPES = ("text/csv", "application/json", "application/zip", "text/plain")
CONFIG_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 10.0


class HubRouteError(Exception):
    """A refusal with its status and code (``{error:{code,message}}``)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class ProxyStream:
    """A download to relay: ``connect()`` yields the hub's response (HubError
    before any byte is sent), ``filename`` is the attachment's name."""

    connect: Callable[[], AbstractContextManager[HTTPResponse]]
    filename: str


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    query: tuple[str, ...] = ()
    kind: str = "json"  # json | zip | stream
    auth: bool = True
    filename: str = "download"


def _route(method: str, path: str, query: tuple[str, ...] = (), **kw: Any) -> Route:
    regex = re.compile("^" + path.replace("{id}", f"({IDENT})") + "$")
    return Route(method, regex, query, **kw)


PAGE = ("limit", "offset")
# The exact central routes the rig page may reach through the adapter.
PROXY_ROUTES = (
    _route("POST", "/auth/register", auth=False),
    _route("GET", "/catalog", ("query", *PAGE)),
    _route("GET", "/experiments", PAGE),
    _route("POST", "/experiments"),
    _route("GET", "/experiments/{id}"),
    _route("PATCH", "/experiments/{id}"),
    _route("POST", "/experiments/{id}/versions", kind="zip"),
    _route(
        "GET", "/experiments/{id}/versions/{id}/download", kind="stream", filename="release.zip"
    ),
    _route("GET", "/experiments/{id}/versions/{id}/documentation"),
    _route("POST", "/experiments/{id}/publish"),
    _route("POST", "/experiments/{id}/unpublish"),
    _route("GET", "/library", PAGE),
    _route("POST", "/library"),
    _route("GET", "/data/sessions", ("experiment_id", "subject_code", "mode", *PAGE)),
    _route("GET", "/data/sessions/{id}"),
    _route("GET", "/data/sessions/{id}/trials", PAGE),
    _route("GET", "/data/sessions/{id}/export", ("format",), kind="stream", filename="trials"),
    _route("GET", "/data/sessions/{id}/files", ("path",), kind="stream"),
    # Rebuild a session's derived trial index (owner only on the hub, 202);
    # the raw files are never touched by it.
    _route("POST", "/data/sessions/{id}/reindex"),
)
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _filename(response: HTTPResponse, fallback: str) -> str:
    header = response.headers.get("Content-Disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", header) or re.search(
        r'filename="?([^";]+)"?', header
    )
    name = match.group(1) if match else fallback
    from urllib.parse import unquote

    name = SAFE_NAME.sub("_", unquote(name).rsplit("/", 1)[-1]).strip("._")
    return name[:120] or fallback


def stream_type(response: HTTPResponse) -> str:
    kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return kind if kind in STREAM_TYPES else "application/octet-stream"


def _text(body: dict[str, Any], name: str, limit: int = 1024) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise HubRouteError(400, "invalid_request", f"{name} is required text")
    return value


class HubAdapter:
    """The hub routes of one dashboard (one workspace)."""

    def __init__(
        self,
        workspace: Workspace,
        data: DataView,
        *,
        packages: Any = None,
        opener: Any = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        uploader_wait_s: float = 1.0,
        backoff_s: tuple[float, ...] = (1, 2, 4, 8, 16),
    ) -> None:
        self.workspace = workspace
        self.data = data
        self._packages = packages
        self._opener = opener
        self._timeout = timeout
        hub = workspace.directory / "hub"
        self.state = RigState(hub)
        self.installs = InstallStore(hub)
        self.outbox = Outbox(hub / "outbox")
        self.previews = Previews()
        self.scratch = hub / "scratch"
        self._lock = threading.Lock()
        self.outbox.recover()
        self.uploader = Uploader(
            self.outbox,
            self.state,
            self._client_for,
            self.busy,
            wait_s=uploader_wait_s,
            backoff_s=backoff_s,
        )
        credential = self.state.credential()
        if credential is not None:
            self.uploader.resume_for(credential.base, credential.user_id)

    def close(self) -> None:
        self.uploader.close()

    # -- plumbing -------------------------------------------------------------------------

    def busy(self) -> bool:
        """A session is running from this workspace: heavy hub work waits."""
        return self.workspace.active is not None

    def _refuse_if_busy(self) -> None:
        if self.busy():
            raise HubRouteError(
                409,
                "run_active",
                "A session is running on this rig; hub work that reads or writes many files "
                "waits until it ends",
            )

    def _client_for(self, credential: Credential) -> HubClient:
        return HubClient(
            credential.base, credential.token, timeout=self._timeout, opener=self._opener
        )

    def _connection(self) -> Connection:
        connection = self.state.connection()
        if connection is None:
            raise HubRouteError(409, "not_connected", "Connect this rig to a hub first")
        return connection

    def _anonymous(self, timeout: float | None = None) -> HubClient:
        return HubClient(
            self._connection().base, None, timeout=timeout or self._timeout, opener=self._opener
        )

    def _signed_in(self) -> Credential:
        self._connection()
        credential = self.state.credential()
        if credential is None:
            raise HubRouteError(401, "unauthenticated", "Sign in to the hub")
        return credential

    def _authed(self, call: Callable[[HubClient], Any]) -> Any:
        """Run ``call`` with the stored bearer; a 401 forgets it."""
        credential = self._signed_in()
        try:
            return call(self._client_for(credential))
        except HubError as exc:
            if exc.status == 401:
                self.state.clear_credential()
                raise HubRouteError(401, "unauthenticated", "Sign in to the hub again") from exc
            raise

    def packages(self) -> Any:
        if self._packages is None:
            try:
                self._packages = importlib.import_module("alhazen.hub.packages")
            except ImportError as exc:
                raise HubRouteError(
                    503, "not_available", "Experiment packages are not available in this install"
                ) from exc
        return self._packages

    # -- entry point ------------------------------------------------------------------------

    def handle(
        self,
        method: str,
        route: str,
        query: dict[str, list[str]],
        body: dict[str, Any] | None = None,
        upload: tuple[IO[bytes], int] | None = None,
    ) -> tuple[int, Any] | ProxyStream:
        """Answer one request under ``/api/hub/v1``; ``route`` is the rest of
        the path. Raises HubRouteError, HubError, InstallError or SyncError."""
        args = {k: v[0] for k, v in query.items() if k != "token"}
        body = body if body is not None else {}
        local = LOCAL_ROUTES.get((method, route))
        if local is not None:
            return local(self, args, body)
        match = re.fullmatch(r"/local/jobs/([A-Za-z0-9]{1,64})(?:/(resume|cancel))?", route)
        if match:
            action = match.group(2)
            if method == "GET" and action is None:
                return 200, {"job": public_job(self._job(match.group(1)))}
            if method == "POST" and action == "resume":
                return 200, self.resume_job(match.group(1))
            if method == "POST" and action == "cancel":
                return 200, self.cancel_job(match.group(1))
        if route.startswith("/local/") or route.startswith("/sessions"):
            raise HubRouteError(404, "not_found", "No such hub route on this rig")
        return self._proxy(method, route, args, body, upload)

    # -- proxied central routes ----------------------------------------------------------------

    def _proxy(
        self,
        method: str,
        route: str,
        args: dict[str, str],
        body: dict[str, Any],
        upload: tuple[IO[bytes], int] | None,
    ) -> tuple[int, Any] | ProxyStream:
        spec = next(
            (r for r in PROXY_ROUTES if r.method == method and r.pattern.fullmatch(route)), None
        )
        if spec is None:
            raise HubRouteError(404, "not_found", "No such hub route")
        unknown = set(args) - set(spec.query)
        if unknown:
            raise HubRouteError(
                400, "invalid_request", f"Unknown query parameter: {sorted(unknown)[0]}"
            )
        if any(len(v) > MAX_QUERY_VALUE for v in args.values()):
            raise HubRouteError(400, "invalid_request", "A query value is too long")
        # The upstream path is the matched route itself, rebuilt segment by
        # segment (every id already matched IDENT), under the fixed base.
        path = api_path(*route.strip("/").split("/"))
        query = {k: args[k] for k in spec.query if k in args} or None
        if spec.kind == "stream":
            client = self._client_for(self._signed_in())
            return ProxyStream(
                connect=lambda: self._open_stream(client, path, query), filename=spec.filename
            )
        if spec.kind == "zip":
            if upload is None:
                raise HubRouteError(400, "invalid_request", "Send the package as application/zip")
            stream, length = upload
            return self._authed(
                lambda c: c.json_with_status(
                    method, path, data=stream, length=length, content_type="application/zip"
                )
            )
        json_body = body if method in ("POST", "PATCH") else None
        if not spec.auth:
            return self._anonymous().json_with_status(
                method, path, query=query, json_body=json_body, authenticated=False
            )
        # The hub's own status passes through (201 created, 202 accepted).
        return self._authed(
            lambda c: c.json_with_status(method, path, query=query, json_body=json_body)
        )

    @contextmanager
    def _open_stream(
        self, client: HubClient, path: str, query: dict[str, str] | None
    ) -> Iterator[HTTPResponse]:
        """The hub's response for a download; a rejected bearer is forgotten."""
        with ExitStack() as stack:
            try:
                response = stack.enter_context(client.stream("GET", path, query=query))
            except HubError as exc:
                if exc.status == 401:
                    self.state.clear_credential()
                    raise HubRouteError(401, "unauthenticated", "Sign in to the hub again") from exc
                raise
            yield response

    # -- config, auth, guide ----------------------------------------------------------------------

    def config(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        public = self.state.public()
        server, server_error = None, None
        if public["base_url"]:
            try:
                server = self._anonymous(CONFIG_TIMEOUT_S).json(
                    "GET", "/config", authenticated=False
                )
            except HubError as exc:
                server_error = {"code": exc.code, "message": exc.message}
        return 200, {
            "role": "rig",
            "api_version": 1,
            "rig": {
                "base_url": public["base_url"],
                "connected": public["base_url"] is not None,
                "signed_in": public["user"] is not None,
                "user": public["user"],
                "workspace_url": "/",
            },
            "server": server,
            "server_error": server_error,
        }

    def guide(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        try:
            documentation = importlib.import_module("alhazen.hub.documentation")
        except ImportError as exc:
            raise HubRouteError(
                503, "not_available", "The offline guide is not available in this install"
            ) from exc
        return 200, {"guide": documentation.global_guide()}

    def login(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        username = _text(body, "username", 256)
        password = _text(body, "password", 1024)
        self._connection()
        credential = sign_in(self._anonymous(), username, password)
        self.state.save_credential(credential)
        self.uploader.resume_for(credential.base, credential.user_id)
        return 200, {
            "user": credential.user,
            "csrf_token": None,
            "expires_at": credential.expires_at,
        }

    def me(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        answer = self._authed(lambda c: c.json("GET", "/auth/me"))
        user = public_user(answer.get("user") if isinstance(answer, dict) else None)
        return 200, {"user": user, "csrf_token": None}

    def logout(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        credential = self.state.credential()
        if credential is None:
            return 200, {"ok": True, "revoked": False, "paused_jobs": 0}
        paused = self._pause_jobs(credential)
        revoked = True
        try:
            self._client_for(credential).json("POST", "/auth/logout")
        except HubError:
            # The local sign-in is forgotten either way; the page is told the
            # hub may still hold the session (`revoked: false`).
            revoked = False
        self.state.clear_credential()
        return 200, {"ok": True, "revoked": revoked, "paused_jobs": paused}

    def _pause_jobs(self, credential: Credential) -> int:
        count = 0
        for job in self.outbox.visible(credential.base, credential.user_id):
            if job.get("status") in ACTIVE:
                self.uploader.pause(
                    job["id"], "signed_out", "Signed out of the hub; sign in again to resume"
                )
                count += 1
        return count

    # -- connection ------------------------------------------------------------------------

    def status(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        public = self.state.public()
        credential = self.state.credential()
        jobs = self.outbox.visible(credential.base, credential.user_id) if credential else []
        interpreters = {sys.executable: "This dashboard's Python"}
        for project in self.workspace.projects:
            interpreters.setdefault(project["python"], f"Used by {project['name']}")
        return 200, {
            **public,
            "installed": [self._install_public(r) for r in self.installs.records()],
            "jobs": [public_job(j) for j in jobs],
            "run_active": self.busy(),
            "outbox_problems": self.outbox.problems(),
            "interpreters": [{"path": p, "label": label} for p, label in interpreters.items()],
            "trust_statement": TRUST_STATEMENT,
        }

    def connect(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        allow = body.get("allow_http_loopback", False)
        if not isinstance(allow, bool):
            raise HubRouteError(400, "invalid_request", "allow_http_loopback must be true or false")
        try:
            base = canonical_base(_text(body, "url", 2048), allow_http_loopback=allow)
        except ValueError as exc:
            raise HubRouteError(400, "invalid_url", str(exc)) from exc
        probe_hub(base, timeout=CONNECT_TIMEOUT_S, opener=self._opener)
        with self._lock:
            credential = self.state.credential()
            if credential is not None and credential.base != base:
                self._pause_jobs(credential)
            self.state.save_connection(Connection(base, allow))
        return self.status(args, body)

    def disconnect(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        if self.state.credential() is not None:
            self.logout(args, body)
        self.state.clear_connection()
        return self.status(args, body)

    # -- projects and installs ------------------------------------------------------------------

    def _install_public(self, record: dict[str, Any]) -> dict[str, Any]:
        registered = record.get("project_id") and any(
            p["id"] == record["project_id"] for p in self.workspace.projects
        )
        return {
            "sha256": record.get("sha256"),
            "experiment_id": record.get("experiment_id"),
            "version_id": record.get("version_id"),
            "name": record.get("name"),
            "version": record.get("version"),
            "title": record.get("title"),
            "project_id": record.get("project_id") if registered else None,
            "workspace_url": f"/?project={record['project_id']}&view=run" if registered else None,
            "status": (
                "installing"
                if record.get("status") == "installing"
                else ("registered" if registered else "installed")
            ),
            "durable": record.get("durable"),
            "python": record.get("python"),
            "installed_at": record.get("installed_at"),
            "hardware": record.get("hardware"),
            "license": record.get("license"),
            "citations": record.get("citations"),
            "base_url": record.get("base_url"),
            "intact": record.get("intact"),
            "error": record.get("error"),
        }

    def projects(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        items = []
        for project in list(self.workspace.projects):
            described = self.workspace.describe(project["id"])
            install = self.installs.for_path(project["path"])
            items.append(
                {
                    "id": project["id"],
                    "title": described["title"],
                    "slug": described["slug"],
                    "version": described["version"],
                    "path": project["path"],
                    "archived": described["archived"],
                    "install": self._install_public(install) if install else None,
                }
            )
        return 200, {"items": items}

    def install(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self._refuse_if_busy()
        credential = self._signed_in()
        python = body.get("python")
        if not isinstance(python, str) or not python or not Path(python).is_absolute():
            raise HubRouteError(
                400, "invalid_request", "Choose the Python interpreter by its full path"
            )
        if not Path(python).is_file():
            raise HubRouteError(400, "invalid_request", f"No interpreter at {python}")
        sha256 = check_sha256(body.get("sha256"))
        record = self._authed(
            lambda client: self.installs.install(
                client,
                self.packages(),
                experiment_id=check_identifier(body.get("experiment_id"), "experiment_id"),
                version_id=check_identifier(body.get("version_id"), "version_id"),
                sha256=sha256,
                trust_code=body.get("trust_code"),
                trusted_by=credential.user,
            )
        )
        return 201, {"install": self._install_public(self.register(record["sha256"], python))}

    def install_recover(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        """Explicit recovery of an interrupted install (package module's own
        recovery; other content is never removed)."""
        self._refuse_if_busy()
        answer = self.installs.recover(self.packages(), check_sha256(body.get("sha256")))
        install = answer["install"]
        return 200, {
            "recovery": answer["recovery"],
            "install": self._install_public(install) if install else None,
        }

    def register(self, sha256: str, python: str) -> dict[str, Any]:
        """Probe ``python`` and register a TRUSTED install with the workspace.
        Both import code from the release, so the trust gate comes first."""
        record = self.installs.trusted_record(sha256)
        path = record["path"]
        problems = self.installs.verify(record)
        if problems:
            self.installs.update(sha256, intact=False)
            raise InstallError(
                409,
                "install_changed",
                "The installed files changed since they were unpacked: " + "; ".join(problems[:5]),
            )
        try:
            # Looked up on the module at call time, as Workspace.add does.
            probe = workspace_module.probe_interpreter(python, path)
        except ValueError as exc:
            self.installs.update(
                sha256, intact=True, error={"code": "interpreter_unusable", "message": str(exc)}
            )
            raise InstallError(400, "interpreter_unusable", str(exc)) from exc
        if _python_release(probe.get("python_version")) is None:
            raise InstallError(
                400, "interpreter_unusable", "The interpreter did not report its Python version"
            )
        problems = self.packages().compatibility_problems(
            record,
            python_version=_python_release(probe.get("python_version")),
            alhazen_version=str(probe.get("alhazen_version") or ""),
            platform=sys.platform,
        )
        if problems:
            message = "This interpreter cannot run the release: " + "; ".join(problems)
            self.installs.update(
                sha256, intact=True, error={"code": "incompatible", "message": message}
            )
            raise InstallError(400, "incompatible", message)
        key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]
        try:
            if any(p["id"] == key for p in self.workspace.projects):
                described = self.workspace.add(path, python)
            else:
                described = self.workspace.register(path, python)
        except ValueError as exc:
            self.installs.update(sha256, error={"code": "registration_failed", "message": str(exc)})
            raise InstallError(400, "registration_failed", str(exc)) from exc
        return self.installs.update(
            sha256,
            status="registered",
            project_id=described["id"],
            python=python,
            intact=True,
            error=None,
        )

    # -- source packages --------------------------------------------------------------------------

    def _project_root(self, body: dict[str, Any]) -> Path:
        project = self.workspace.project(_text(body, "project_id", 64))
        return Path(project["path"])

    def package_preview(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self._refuse_if_busy()
        return 200, source_preview(self._project_root(body), self.packages())

    def package_upload(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self._refuse_if_busy()
        if body.get("confirmed") is not True:
            raise HubRouteError(400, "confirmation_required", "Confirm the file list to upload it")
        root = self._project_root(body)
        experiment_id = check_identifier(body.get("experiment_id"), "experiment_id")
        packages = self.packages()
        try:
            answer = self._authed(
                lambda client: pack_and_upload(
                    client,
                    root,
                    packages,
                    self.scratch,
                    experiment_id=experiment_id,
                    metadata=body.get("metadata"),
                    files=body.get("files"),
                )
            )
        except packages.PackageError as exc:
            raise HubRouteError(422, "invalid_package", str(exc)) from exc
        return 201, answer

    # -- sessions ---------------------------------------------------------------------------------

    def sessions(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        project_id = args.get("project_id", "")
        roots = self.data.roots(project_id)
        credential = self.state.credential()
        jobs: dict[tuple[Any, Any], dict[str, Any]] = {}
        if credential is not None:
            for job in self.outbox.visible(credential.base, credential.user_id):
                jobs.setdefault((job.get("root_id"), job.get("run_id")), job)
        items, problems = [], list(roots["problems"])
        for root in roots["roots"]:
            listed = self.data.runs(project_id, root["id"])
            problems += listed["problems"]
            base = Path(listed["root"]["path"])
            for row in listed["runs"]:
                folder = base / row["id"]
                found = jobs.get((root["id"], row["id"]))
                items.append(
                    {
                        "root_id": root["id"],
                        "root_kind": root["kind"],
                        "root_name": root["name"],
                        "run_id": row["id"],
                        "subject": row["subject"],
                        "session": row["session"],
                        "run": row["run"],
                        "task": row["task"],
                        "mode": row["mode"],
                        "date": row["date"],
                        "complete": (folder / "manifest.yaml").is_file(),
                        "job": public_job(found) if found else None,
                    }
                )
        return 200, {"items": items, "problems": problems, "run_active": self.busy()}

    def _session(self, body: dict[str, Any]) -> tuple[str, str, str, Path, dict[str, Any]]:
        project_id = _text(body, "project_id", 64)
        root_id = _text(body, "root_id", 64)
        run_id = _text(body, "run_id", 512)
        project = self.workspace.project(project_id)
        root = self.data._root(project_id, root_id)
        return project_id, root_id, run_id, _run_folder(root.path, run_id), project

    def _recorded_release(self, project_id: str, folder: Path) -> dict[str, Any] | None:
        """The hub release the workspace recorded at launch for the run that
        wrote ``folder`` (run.json ``hub_release``), found through the run's
        console as the Run page finds its session; None when no recorded run
        of this project wrote it (an older run, or one from another tool)."""
        existing, _, _ = data_roots(self.workspace.describe(project_id))
        target = str(folder.resolve())
        with self.workspace.lock:
            runs = [
                dict(r)
                for r in self.workspace.runs.values()
                if r.get("project") == project_id and r.get("hub_release")
            ]
        for run in runs:
            console = Path(run["directory"]) / "console.log"
            if _console_run_folder(console, existing, run.get("mode")) == target:
                return dict(run["hub_release"])
        return None

    def _release_for(
        self, project: dict[str, Any], folder: Path, body: dict[str, Any], base: str
    ) -> tuple[str, str, dict[str, Any] | None, str]:
        """``(experiment_id, version_id, install, source)`` for an upload.

        The release recorded with the run at launch wins; else the folder's
        install record; else the operator's choice. A choice that differs
        from a recorded identity, or a release from another hub, is refused."""
        install = self.installs.for_path(project["path"])
        recorded = self._recorded_release(project["id"], folder)
        pinned, source = (recorded, "run_record") if recorded else (install, "install_record")
        experiment_id = body.get("experiment_id") or (pinned or {}).get("experiment_id")
        version_id = body.get("version_id") or (pinned or {}).get("version_id")
        check_identifier(experiment_id, "experiment_id")
        check_identifier(version_id, "version_id")
        if pinned is None:
            return str(experiment_id), str(version_id), install, "operator"
        if pinned.get("base_url") != base:
            raise HubRouteError(
                409,
                "conflict",
                "This session's experiment was installed from another hub; it can only be "
                "uploaded there",
            )
        if pinned.get("experiment_id") != experiment_id or pinned.get("version_id") != version_id:
            what = "recorded with this run" if recorded else "this experiment was installed from"
            raise HubRouteError(
                409,
                "conflict",
                f"The release {what} differs from the one chosen; sessions are uploaded under "
                "the release that ran them",
            )
        return str(experiment_id), str(version_id), install, source

    def upload_preview(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self._refuse_if_busy()
        credential = self._signed_in()
        project_id, root_id, run_id, folder, project = self._session(body)
        experiment_id, version_id, install, release_source = self._release_for(
            project, folder, body, credential.base
        )
        listing = session_files(folder)
        session_key = client_session_id(self.state.rig_id(), folder)
        manifest_sha = file_sha256(folder / "manifest.yaml")
        metadata = session_metadata(folder, run_id)
        binding = {
            "base_url": credential.base,
            "user_id": credential.user_id,
            "project_id": project_id,
            "root_id": root_id,
            "run_id": run_id,
            "experiment_id": experiment_id,
            "version_id": version_id,
            "client_session_id": session_key,
            "manifest_sha256": manifest_sha,
            "listing": [asdict(f) for f in listing],
            "folder": str(folder),
            "metadata": metadata,
            "install_sha256": (install or {}).get("sha256"),
            "release_source": release_source,
        }
        preview_id = self.previews.create(binding)
        return 200, {
            "preview_id": preview_id,
            "manifest_digest": manifest_sha,
            "files": [{"path": f.path, "size": f.size} for f in listing],
            "total_bytes": sum(f.size for f in listing),
            "file_count": len(listing),
            "recipient": {"base_url": credential.base, "user": dict(credential.user)},
            "experiment_id": experiment_id,
            "version_id": version_id,
            "release_source": release_source,
            "metadata": metadata,
            "privacy": {"fields": privacy_fields(folder), "warning": PRIVACY_WARNING},
            "install": self._install_public(install) if install else None,
        }

    def upload(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        if body.get("consent") is not True:
            raise HubRouteError(400, "consent_required", "Confirm the upload to start it")
        binding = self.previews.get(body.get("preview_id"))
        credential = self._signed_in()
        if credential.base != binding["base_url"] or credential.user_id != binding["user_id"]:
            raise HubRouteError(
                409,
                "auth_context_changed",
                "The signed-in hub account changed since the preview; preview again",
            )
        for key in ("project_id", "root_id", "run_id", "experiment_id", "version_id"):
            if body.get(key) != binding[key]:
                raise HubRouteError(409, "preview_stale", f"{key} differs from the preview")
        project_id, root_id, run_id, folder, project = self._session(body)
        current = [asdict(f) for f in session_files(folder)]
        if (
            str(folder) != binding["folder"]
            or current != binding["listing"]
            or file_sha256(folder / "manifest.yaml") != binding["manifest_sha256"]
        ):
            raise HubRouteError(409, "preview_stale", "The session changed since the preview")
        job_id = job_id_for(binding["base_url"], binding["user_id"], binding["client_session_id"])
        with self._lock:
            existing = self.outbox.load(job_id)
            if existing is not None:
                same_release = (existing.get("experiment_id"), existing.get("version_id")) == (
                    binding["experiment_id"],
                    binding["version_id"],
                )
                if not same_release:
                    raise HubRouteError(
                        409,
                        "conflict",
                        "This session is already being uploaded under another release",
                    )
                if existing.get("status") in (*ACTIVE, "completed"):
                    return 200, {"job": public_job(existing)}
            from alhazen.hub.sync import now

            job = {
                "id": job_id,
                "status": "queued",
                **{k: binding[k] for k in ("project_id", "root_id", "run_id")},
                "experiment_id": binding["experiment_id"],
                "version_id": binding["version_id"],
                "base_url": binding["base_url"],
                "user_id": binding["user_id"],
                "client_session_id": binding["client_session_id"],
                "manifest_digest": binding["manifest_sha256"],
                "folder": binding["folder"],
                "listing": binding["listing"],
                "metadata": binding["metadata"],
                "install_sha256": binding["install_sha256"],
                "release_source": binding["release_source"],
                "files": None,
                "files_digest": None,
                # Always re-initialised: the hub answers the same session for the
                # same files and refuses different content under this identity.
                "session_id": None,
                "bytes_done": 0,
                "bytes_total": sum(f["size"] for f in binding["listing"]),
                "files_done": 0,
                "files_total": len(binding["listing"]),
                "error": None,
                "receipt": None,
                "consent": {
                    "at": now(),
                    "preview_id_sha256": hashlib.sha256(
                        str(body["preview_id"]).encode()
                    ).hexdigest(),
                    "warning": PRIVACY_WARNING,
                },
                "created_at": (existing or {}).get("created_at") or now(),
            }
            self.outbox.save(job)
        self.uploader.submit(job_id)
        return 202, {"job": public_job(job)}

    def _job(self, job_id: str) -> dict[str, Any]:
        credential = self.state.credential()
        job = self.outbox.load(job_id)
        if (
            job is None
            or credential is None
            or job.get("base_url") != credential.base
            or job.get("user_id") != credential.user_id
        ):
            raise HubRouteError(404, "not_found", "No such upload job")
        return job

    def jobs(self, args: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        credential = self.state.credential()
        jobs = self.outbox.visible(credential.base, credential.user_id) if credential else []
        return 200, {"items": [public_job(j) for j in jobs]}

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        retryable = (job.get("error") or {}).get("retryable")
        if job.get("status") in ACTIVE or job.get("status") == "completed":
            return {"job": public_job(job)}
        if job.get("status") == "cancelled" or not retryable:
            raise HubRouteError(
                409, "preview_required", "Preview the session again to start a new upload"
            )
        job = self.outbox.update(job_id, status="queued", error=None)
        self.uploader.submit(job_id)
        return {"job": public_job(job)}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        if job.get("status") in ACTIVE:
            self.uploader.cancel(job_id)
            if job.get("status") == "queued":
                job = self.outbox.update(
                    job_id,
                    status="cancelled",
                    error={"code": "cancelled", "message": "Cancelled", "retryable": True},
                )
        elif job.get("status") in ("paused", "failed"):
            job = self.outbox.update(
                job_id,
                status="cancelled",
                error={"code": "cancelled", "message": "Cancelled", "retryable": True},
            )
        return {"job": public_job(job)}


def _python_release(text: Any) -> tuple[int, int] | None:
    """``(3, 11)`` from the probe's ``sys.version`` text; None when unreadable
    (the package module then judges against this interpreter, so an
    unreadable answer is refused below rather than guessed)."""
    match = re.match(r"\s*(\d+)\.(\d+)", str(text or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


LocalHandler = Callable[[HubAdapter, dict[str, str], dict[str, Any]], tuple[int, Any]]
LOCAL_ROUTES: dict[tuple[str, str], LocalHandler] = {
    ("GET", "/config"): HubAdapter.config,
    ("GET", "/guide"): HubAdapter.guide,
    ("POST", "/auth/login"): HubAdapter.login,
    ("POST", "/auth/token"): HubAdapter.login,
    ("GET", "/auth/me"): HubAdapter.me,
    ("POST", "/auth/logout"): HubAdapter.logout,
    ("GET", "/local/status"): HubAdapter.status,
    ("POST", "/local/connect"): HubAdapter.connect,
    ("POST", "/local/disconnect"): HubAdapter.disconnect,
    ("GET", "/local/projects"): HubAdapter.projects,
    ("GET", "/local/installs"): lambda self, a, b: (
        200,
        {"items": [self._install_public(r) for r in self.installs.records()]},
    ),
    ("POST", "/local/install"): HubAdapter.install,
    ("POST", "/local/install-recover"): HubAdapter.install_recover,
    ("POST", "/local/package-preview"): HubAdapter.package_preview,
    ("POST", "/local/package-upload"): HubAdapter.package_upload,
    ("GET", "/local/sessions"): HubAdapter.sessions,
    ("POST", "/local/upload-preview"): HubAdapter.upload_preview,
    ("POST", "/local/upload"): HubAdapter.upload,
    ("GET", "/local/jobs"): HubAdapter.jobs,
}


def error_payload(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """``(status, {error:{code,message}})`` for anything a hub route raises
    that is a refusal rather than a bug."""
    if isinstance(exc, (HubRouteError, HubError, InstallError, SyncError)):
        status, code, message = exc.status, exc.code, exc.message
    elif isinstance(exc, PermissionError):
        status, code, message = 403, "forbidden", str(exc)
    elif isinstance(exc, FileNotFoundError):
        status, code, message = 404, "not_found", str(exc)
    elif isinstance(exc, ValueError):
        status, code, message = 400, "invalid_request", str(exc)
    elif isinstance(exc, OSError):
        status, code = 500, "local_io_error"
        message = f"A local file operation failed: {exc.strerror or type(exc).__name__}"
    else:
        raise exc
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    return status, payload


def cli_install(
    directory: Path, *, experiment_id: str, version_id: str, sha256: str, python: str
) -> dict[str, Any]:
    """``alhazen hub install``: the dashboard's install route, under the
    workspace lock (so never beside a running dashboard), trust confirmed by
    the caller's ``--trust-code``."""
    from alhazen.cli.dashboard import workspace_lock

    with workspace_lock(directory):
        workspace = Workspace(directory)
        try:
            adapter = HubAdapter(workspace, DataView(workspace))
            try:
                answer = adapter.handle(
                    "POST",
                    "/local/install",
                    {},
                    {
                        "experiment_id": experiment_id,
                        "version_id": version_id,
                        "sha256": sha256,
                        "python": python,
                        "trust_code": True,
                    },
                )
            except HubRouteError as exc:
                raise ValueError(exc.message) from exc
            finally:
                adapter.close()
        finally:
            workspace.close()
    if not isinstance(answer, tuple):  # the install route never streams
        raise TypeError("Unexpected answer from the install route")
    return dict(answer[1]["install"])
