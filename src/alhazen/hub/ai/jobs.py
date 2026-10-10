"""The AI job worker: claims queued provider jobs, runs the authoring kit,
records results, usage and disclosure.

Hides: the claim/lease/fence protocol (the same shape as trial indexing:
a job is claimed with a random token and a lease; every write after the
claim is conditional on the token, so a cancelled or taken-over job can
never record anything), lease renewal and cancellation checks between
provider calls, building the start-from context from a stored release,
packaging generated source into the draft's private area, and mapping
failures onto stable error codes.

The worker never imports or executes generated code: a bundle is packaged
and checked statically (package rules, documentation schema) only.

Seam with the authoring kit: `AuthorKit` adapts the module
``alhazen.hub.ai.author`` (``build_context``, ``plan``, ``generate_source``,
``StartFrom``, ``PlanInvalid``, ``SourceInvalid``, and a plan constructor:
``Plan.from_dict`` / ``parse_plan`` / ``Plan(**fields)``). Tests pass their
own kit.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import secrets
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, or_, select, update

from alhazen.hub import catalog, packages
from alhazen.hub.ai import keys
from alhazen.hub.ai.providers import (
    Completion,
    HttpBudget,
    ProviderClient,
    ProviderError,
    ProviderInfo,
    make_client,
    provider_infos,
)
from alhazen.hub.context import Hub
from alhazen.hub.errors import HubError
from alhazen.hub.schema import ai_drafts, ai_jobs

log = logging.getLogger(__name__)

POLL_SECONDS = 15.0

# Stable error codes of a failed job, with the HTTP status a synchronous
# call would have answered (the UI branches on the code; docs/hub/ai.md).
ERROR_STATUS = {
    "provider_quota": 402,
    "key_required": 409,
    "key_rejected": 409,
    "key_unreadable": 409,
    "start_unavailable": 404,
    "generation_invalid": 422,
    "provider_error": 502,
    "provider_timeout": 504,
    "lease_lost": 500,
    "internal": 500,
}
_PROVIDER_CODES = {
    "quota": "provider_quota",
    "auth": "key_rejected",
    "timeout": "provider_timeout",
    "invalid": "provider_error",
    "other": "provider_error",
}
# What a draft returns to when its job ends without a result.
_DRAFT_ON_FAILURE = {"plan": "describing", "source": "planned"}
_DRAFT_ON_SUCCESS = {"plan": "planned", "source": "generated"}

ClientFactory = Callable[[ProviderInfo, str, str], ProviderClient]


class JobFenced(Exception):
    """This job's token is no longer current (cancelled or taken over)."""


# -- the authoring kit seam ---------------------------------------------------


def to_plain(value: Any) -> Any:
    """A JSON-ready copy of a kit object (Plan, ValidationReport, ...)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    for name in ("to_dict", "as_dict"):
        method = getattr(value, name, None)
        if callable(method):
            return to_plain(method())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return str(value)


@dataclass
class AuthorKit:
    module: Any

    @classmethod
    def default(cls) -> AuthorKit:
        return cls(importlib.import_module("alhazen.hub.ai.author"))

    def start_from(self, title: str, version: str, files: dict[str, str]) -> Any:
        return self.module.StartFrom(experiment_title=title, version=version, files=files)

    def build_context(self, alhazen_version: str, start: Any | None) -> Any:
        return self.module.build_context(alhazen_version, start)

    def plan(self, client: ProviderClient, prompt: str, ctx: Any) -> Any:
        return self.module.plan(client, prompt, ctx)

    def generate_source(self, client: ProviderClient, plan: Any, ctx: Any) -> Any:
        return self.module.generate_source(client, plan, ctx)

    def plan_from_dict(self, data: dict[str, Any]) -> Any:
        plan_type = getattr(self.module, "Plan", None)
        if plan_type is not None and callable(getattr(plan_type, "from_dict", None)):
            return plan_type.from_dict(data)
        parse = getattr(self.module, "parse_plan", None)
        if callable(parse):
            return parse(data)
        if plan_type is not None:
            return plan_type(**data)
        return data

    def disclosure(self, ctx: Any, kind: str) -> dict[str, Any] | None:
        """The kit's own account of what a request sends, if it keeps one."""
        method = getattr(ctx, "disclosure", None)
        return to_plain(method(kind)) if callable(method) else None

    def relabel(self, files: dict[str, bytes], metadata: dict[str, Any], old_license: str) -> bytes:
        """The package rebuilt with edited metadata (title, description,
        license); a changed license also replaces LICENSE with the kit's text
        for it. Returns the new ZIP bytes; PackageError if refused."""
        files = dict(files)
        license_text = getattr(self.module, "license_text", None)
        if metadata.get("license") != old_license and callable(license_text):
            known = getattr(self.module, "LICENSES", None) or getattr(
                getattr(self.module, "schemas", None), "LICENSES", None
            )
            if known is not None and metadata["license"] not in known:
                raise packages.PackageError(
                    f"license must be one of {', '.join(known)} for a generated package"
                )
            files["LICENSE"] = license_text(metadata["license"], metadata["title"]).encode()
        build = getattr(self.module, "bundle_archive", None)
        if callable(build):
            archive, _info = build(files, metadata)
            return bytes(archive)
        with tempfile.TemporaryDirectory(prefix="ai-relabel-") as folder:
            root = Path(folder) / "source"
            for rel, data in files.items():
                target = root / Path(packages.safe_relative(rel))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            out = Path(folder) / "bundle.zip"
            packages.build_bundle(root, out, metadata, sorted(files))
            return out.read_bytes()

    def is_plan_invalid(self, exc: BaseException) -> bool:
        kind = getattr(self.module, "PlanInvalid", None)
        return kind is not None and isinstance(exc, kind)

    def is_source_invalid(self, exc: BaseException) -> bool:
        kind = getattr(self.module, "SourceInvalid", None)
        return kind is not None and isinstance(exc, kind)


# -- the guarded client ---------------------------------------------------------


class GuardedClient:
    """Wraps the provider client for one job: before every call it renews the
    job's lease (conditional on its token, so a cancelled or taken-over job
    stops before spending), and it totals what was sent and the usage."""

    def __init__(self, hub: Hub, job_id: str, token: str, inner: ProviderClient) -> None:
        self._hub = hub
        self._job_id = job_id
        self._token = token
        self._inner = inner
        self.calls = 0
        self.sent_bytes = 0
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.models: list[str] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> Completion:
        renew(self._hub, self._job_id, self._token)
        cap = self._hub.settings.ai.max_output_tokens
        self.calls += 1
        self.sent_bytes += sum(len(str(m.get("content", "")).encode()) for m in messages)
        result = self._inner.complete(
            messages,
            json_schema=json_schema,
            max_tokens=min(max_tokens, cap),
            temperature=temperature,
        )
        for name in ("input_tokens", "output_tokens"):
            self.usage[name] += int(result.usage.get(name, 0) or 0)
        self.models.append(result.model)
        return result


def renew(hub: Hub, job_id: str, token: str) -> None:
    lease = hub.settings.ai.job_lease_seconds * 1000
    with hub.db.transaction() as conn:
        held = conn.execute(
            update(ai_jobs)
            .where(ai_jobs.c.id == job_id, ai_jobs.c.token == token, ai_jobs.c.status == "running")
            .values(lease_until=hub.clock() + lease)
        ).rowcount
    if not held:
        raise JobFenced(job_id)


# -- claiming ---------------------------------------------------------------------


def claim_next(hub: Hub, job_id: str | None = None) -> tuple[str, str] | None:
    """Claim the oldest queued job, or a running one whose lease lapsed
    (its worker died), as (job_id, token). A lapsed job that already used
    its attempts is failed with ``lease_lost`` instead of rerun."""
    ai = hub.settings.ai
    while True:
        now = hub.clock()
        with hub.db.transaction() as conn:
            query = (
                select(ai_jobs)
                .where(
                    or_(
                        ai_jobs.c.status == "queued",
                        (ai_jobs.c.status == "running") & (ai_jobs.c.lease_until <= now),
                    )
                )
                .order_by(ai_jobs.c.created_at, ai_jobs.c.id)
                .limit(1)
            )
            if job_id is not None:
                query = query.where(ai_jobs.c.id == job_id)
            row = conn.execute(query).first()
            if row is None:
                return None
            guard = [ai_jobs.c.id == row.id, ai_jobs.c.status == row.status]
            if row.status == "running":
                guard.append(ai_jobs.c.token == row.token)
                if row.attempts >= ai.max_attempts:
                    taken = conn.execute(
                        update(ai_jobs)
                        .where(*guard)
                        .values(
                            status="failed",
                            error_code="lease_lost",
                            error_message="The job was interrupted too many times; start it again",
                            finished_at=now,
                            lease_until=None,
                            token=None,
                        )
                    ).rowcount
                    if taken:
                        revert_draft(conn, row, now)
                    continue
            token = secrets.token_hex(16)
            taken = conn.execute(
                update(ai_jobs)
                .where(*guard)
                .values(
                    status="running",
                    token=token,
                    attempts=row.attempts + 1,
                    started_at=now,
                    lease_until=now + ai.job_lease_seconds * 1000,
                )
            ).rowcount
            if taken:
                return str(row.id), token
        if job_id is not None:
            return None


def revert_draft(conn: Connection, job: Any, now: int) -> None:
    """A job ended without a result: its draft goes back one step, if the
    draft still points at this job."""
    pointer = ai_drafts.c.plan_job_id if job.kind == "plan" else ai_drafts.c.source_job_id
    conn.execute(
        update(ai_drafts)
        .where(
            ai_drafts.c.id == job.draft_id,
            pointer == job.id,
            ai_drafts.c.status == ("planning" if job.kind == "plan" else "generating"),
        )
        .values(status=_DRAFT_ON_FAILURE[job.kind], updated_at=now)
    )


# -- running one job ------------------------------------------------------------


@dataclass
class _Outcome:
    status: str  # done | failed
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    plan_json: str | None = None
    bundle_key: str | None = None


@dataclass
class Runner:
    """Everything a job needs besides the database: the kit and the clients."""

    hub: Hub
    kit_factory: Callable[[], AuthorKit] = AuthorKit.default
    client_factory: ClientFactory | None = None
    _kit: AuthorKit | None = field(default=None, init=False)

    @property
    def kit(self) -> AuthorKit:
        if self._kit is None:
            self._kit = self.kit_factory()
        return self._kit

    @kit.setter
    def kit(self, value: AuthorKit) -> None:
        self._kit = value

    def infos(self) -> dict[str, ProviderInfo]:
        return provider_infos(self.hub.settings.ai.providers)

    def make_client(self, provider: str, key: str, model: str) -> ProviderClient:
        info = self.infos()[provider]
        if self.client_factory is not None:
            return self.client_factory(info, key, model)
        ai = self.hub.settings.ai
        budget = HttpBudget(
            connect_seconds=ai.connect_timeout_seconds,
            read_seconds=ai.read_timeout_seconds,
            max_response_bytes=ai.max_response_bytes,
        )
        return make_client(info, key, model, budget)

    def run(self, job_id: str, token: str) -> str:
        """Run one claimed job; return its final status ("superseded" if fenced)."""
        hub = self.hub
        with hub.db.transaction() as conn:
            job = conn.execute(
                select(ai_jobs).where(ai_jobs.c.id == job_id, ai_jobs.c.token == token)
            ).first()
            draft = (
                conn.execute(select(ai_drafts).where(ai_drafts.c.id == job.draft_id)).first()
                if job is not None
                else None
            )
        if job is None or draft is None:
            return "superseded"
        disclosed: dict[str, Any] = {
            "provider": job.provider,
            "model": job.model,
            "prompt_chars": len(draft.prompt),
            "plan": job.kind == "source",
            "authoring_context": {
                "alhazen_version": _alhazen_version(),
                "items": [
                    "experiment scaffold",
                    "package manifest schema",
                    "documentation schema",
                    "alhazen modes and API summary",
                ],
            },
            "start_from": None,
            "calls": 0,
            "sent_bytes": 0,
        }
        guarded: GuardedClient | None = None
        try:
            outcome, guarded = self._attempt(job, draft, token, disclosed)
        except JobFenced:
            return "superseded"
        if guarded is not None:
            disclosed["calls"] = guarded.calls
            disclosed["sent_bytes"] = guarded.sent_bytes
        usage = guarded.usage if guarded is not None else None
        return self._finish(job, token, outcome, usage, disclosed)

    def _attempt(
        self, job: Any, draft: Any, token: str, disclosed: dict[str, Any]
    ) -> tuple[_Outcome, GuardedClient | None]:
        hub = self.hub
        try:
            key = keys.reveal(hub, job.user_id, job.provider)
        except HubError as exc:
            return _Outcome("failed", error_code=exc.code, error_message=exc.message), None
        try:
            start, start_disclosed = self._start(draft)
        except HubError:
            return (
                _Outcome(
                    "failed",
                    error_code="start_unavailable",
                    error_message="The version this draft starts from is no longer available",
                ),
                None,
            )
        disclosed["start_from"] = start_disclosed
        guarded = GuardedClient(hub, job.id, token, self.make_client(job.provider, key, job.model))
        del key
        kit = self.kit
        try:
            ctx = kit.build_context(_alhazen_version(), start)
            _merge_disclosure(disclosed, kit.disclosure(ctx, job.kind))
            if job.kind == "plan":
                plan = kit.plan(guarded, draft.prompt, ctx)
                plain = to_plain(plan)
                return (
                    _Outcome("done", result={"plan": plain}, plan_json=json.dumps(plain)),
                    guarded,
                )
            request = json.loads(job.request_json)
            plan = kit.plan_from_dict(request["plan"])
            bundle = kit.generate_source(guarded, plan, ctx)
            return self._package(job, bundle), guarded
        except JobFenced:
            raise
        except ProviderError as exc:
            code = _PROVIDER_CODES.get(exc.kind, "provider_error")
            return _Outcome("failed", error_code=code, error_message=exc.message[:500]), guarded
        except Exception as exc:
            if kit.is_plan_invalid(exc) or kit.is_source_invalid(exc):
                report = to_plain(getattr(exc, "report", None))
                return (
                    _Outcome(
                        "failed",
                        result={"report": report},
                        error_code="generation_invalid",
                        error_message="The model's answer did not pass validation after one repair",
                    ),
                    guarded,
                )
            log.exception("AI job %s failed unexpectedly", job.id)
            return (
                _Outcome(
                    "failed", error_code="internal", error_message="The job failed on the hub"
                ),
                guarded,
            )

    def _start(self, draft: Any) -> tuple[Any | None, dict[str, Any] | None]:
        """The start-from version's text files, re-checked against the
        download rule NOW (it may have been unpublished since the draft)."""
        if draft.start_version_id is None:
            return None, None
        hub = self.hub
        with hub.db.transaction() as conn:
            exp, ver = catalog.readable_version(
                conn, draft.user_id, draft.start_experiment_id, draft.start_version_id
            )
            title = exp.title if exp.owner_id == draft.user_id else _published_title(conn, exp)
        ai = hub.settings.ai
        files, listed, skipped = read_source_files(
            hub.store.path(ver.storage_key),
            json.loads(ver.manifest),
            max_total=ai.max_context_bytes,
            max_file=ai.max_context_file_bytes,
        )
        disclosed = {
            "experiment_id": exp.id,
            "version_id": ver.id,
            "version": ver.version,
            "files": listed,
            "bytes": sum(item["bytes"] for item in listed),
            "skipped": skipped,
        }
        return self.kit.start_from(title, ver.version, files), disclosed

    def _package(self, job: Any, bundle: Any) -> _Outcome:
        """Package the kit's files as a real release ZIP in the draft's area,
        with the same checks an upload passes (never executed)."""
        hub = self.hub
        files: dict[str, bytes] = dict(bundle.files)
        manifest = dict(bundle.manifest)
        report = to_plain(getattr(bundle, "report", None))
        metadata = {k: v for k, v in manifest.items() if k not in ("files", "schema_version")}
        work = Path(tempfile.mkdtemp(prefix="ai-bundle-", dir=hub.store.root / "tmp"))
        try:
            out = work / "bundle.zip"
            archive = getattr(bundle, "archive", None)
            if archive:
                # The kit packaged it already (author.bundle_archive); the
                # hub re-reads the bytes with its own checks.
                out.write_bytes(bytes(archive))
                info = packages.inspect_bundle(out)
            else:
                source = work / "source"
                for rel, data in files.items():
                    target = source / Path(packages.safe_relative(rel))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                info = packages.build_bundle(source, out, metadata, sorted(files))
            catalog.check_documentation(out, info.manifest)
            key = hub.store.ai_bundle_key(job.draft_id, job.id)
            hub.store.install_ai_bundle(out, key)
        except (packages.PackageError, HubError, OSError) as exc:
            message = str(exc.message if isinstance(exc, HubError) else exc)
            return _Outcome(
                "failed",
                result={"report": report, "package_error": message[:2000]},
                error_code="generation_invalid",
                error_message="The generated source does not form a valid package",
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        manifest_view = {k: v for k, v in info.manifest.items() if k != "files"}
        return _Outcome(
            "done",
            result={
                "valid": True,
                "files": [dict(entry) for entry in info.manifest["files"]],
                "manifest": manifest_view,
                "report": report,
                "bundle": {"sha256": info.sha256, "size": info.size},
            },
            bundle_key=key,
        )

    def _finish(
        self,
        job: Any,
        token: str,
        outcome: _Outcome,
        usage: dict[str, int] | None,
        disclosed: dict[str, Any],
    ) -> str:
        hub = self.hub
        now = hub.clock()
        try:
            with hub.db.transaction() as conn:
                done = conn.execute(
                    update(ai_jobs)
                    .where(
                        ai_jobs.c.id == job.id,
                        ai_jobs.c.token == token,
                        ai_jobs.c.status == "running",
                    )
                    .values(
                        status=outcome.status,
                        result_json=None if outcome.result is None else json.dumps(outcome.result),
                        error_code=outcome.error_code,
                        error_message=outcome.error_message,
                        usage_json=None if usage is None else json.dumps(usage),
                        disclosed_json=json.dumps(disclosed),
                        finished_at=now,
                        lease_until=None,
                        token=None,
                    )
                ).rowcount
                if not done:
                    raise JobFenced(job.id)
                if outcome.status == "done":
                    pointer = (
                        ai_drafts.c.plan_job_id if job.kind == "plan" else ai_drafts.c.source_job_id
                    )
                    values: dict[str, Any] = {
                        "status": _DRAFT_ON_SUCCESS[job.kind],
                        "updated_at": now,
                    }
                    if outcome.plan_json is not None:
                        values["plan_json"] = outcome.plan_json
                    conn.execute(
                        update(ai_drafts)
                        .where(ai_drafts.c.id == job.draft_id, pointer == job.id)
                        .values(**values)
                    )
                else:
                    revert_draft(conn, job, now)
        except JobFenced:
            self._discard(outcome)
            return "superseded"
        except BaseException:
            self._discard(outcome)
            raise
        return outcome.status

    def _discard(self, outcome: _Outcome) -> None:
        """A packaged bundle whose result was never recorded is unreachable."""
        if outcome.bundle_key is not None:
            self.hub.store.path(outcome.bundle_key).unlink(missing_ok=True)


def _merge_disclosure(disclosed: dict[str, Any], kit: dict[str, Any] | None) -> None:
    """Fold the kit's account of the request into the job's record: its
    context items and sizes, and which start-from files it actually sent
    (it may leave some of those the hub read out; both lists are kept)."""
    if not kit:
        return
    disclosed["authoring_context"]["context"] = kit.get("context", [])
    disclosed["authoring_context"]["bytes"] = kit.get("context_bytes")
    start, sent = disclosed.get("start_from"), kit.get("start_from")
    if start is not None and sent:
        start["files"] = sent.get("files", start["files"])
        start["bytes"] = sent.get("bytes", start["bytes"])
        start["skipped"] = [*start.get("skipped", []), *sent.get("omitted", [])]


def _published_title(conn: Connection, exp: Any) -> str:
    from alhazen.hub.schema import publications

    title = conn.execute(
        select(publications.c.title).where(publications.c.experiment_id == exp.id)
    ).scalar_one_or_none()
    return str(title or exp.title)


def _alhazen_version() -> str:
    from alhazen.version import __version__

    return str(__version__)


def read_source_files(
    path: Path, manifest: dict[str, Any], *, max_total: int, max_file: int
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, str]]]:
    """The declared text files of a stored release, within budgets.

    Returns (files, disclosed list [{path, bytes}], skipped [{path, reason}]).
    A file is skipped when it is not UTF-8 text, larger than ``max_file``, or
    would take the total past ``max_total``; skipped files are never sent.
    """
    files: dict[str, str] = {}
    listed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for entry in manifest.get("files", []):
            rel = str(entry["path"])
            size = int(entry["size"])
            if size > max_file:
                skipped.append({"path": rel, "reason": "too_large"})
                continue
            if total + size > max_total:
                skipped.append({"path": rel, "reason": "context_budget"})
                continue
            data = archive.read(rel)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": rel, "reason": "not_text"})
                continue
            if "\x00" in text:
                skipped.append({"path": rel, "reason": "not_text"})
                continue
            files[rel] = text
            listed.append({"path": rel, "bytes": len(data)})
            total += len(data)
    return files, listed, skipped


# -- the worker threads -----------------------------------------------------------


class AIWorker:
    """``settings.ai.workers`` threads that drain the job queue. Woken when a
    job is queued; also polls, so lapsed leases are retaken after a restart."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # The last pass failure (exception class), for operators; cleared by
        # a clean pass. A failed pass leaves its job leased, so it is retried.
        self.last_error: str | None = None

    def start(self) -> None:
        if self._threads:
            return
        for i in range(self.runner.hub.settings.ai.workers):
            thread = threading.Thread(target=self._loop, name=f"alhazen-hub-ai-{i}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout)
        self._threads = []

    def wake(self) -> None:
        self._wake.set()

    def drain(self, job_id: str | None = None) -> int:
        """Run due jobs in the calling thread until none is left; return how
        many ran (tests and operator commands)."""
        ran = 0
        while not self._stop.is_set():
            claimed = claim_next(self.runner.hub, job_id)
            if claimed is None:
                return ran
            self.runner.run(*claimed)
            ran += 1
            if job_id is not None:
                return ran
        return ran

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - recorded and logged; the loop must survive
                self.last_error = type(exc).__name__
                log.exception("AI worker pass failed; its job's lease will lapse and be retried")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()
