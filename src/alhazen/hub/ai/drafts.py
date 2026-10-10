"""AI drafts: the HTTP-facing service of AI-assisted authoring.

Hides: who may do what with a draft (only its owner, who must be signed
in; a miss and another user's draft answer the same 404), the draft state
machine, job admission (per-user active and daily limits), and acceptance,
which turns a validated generated bundle into a private version through
the same code path as an upload (`catalog.accept_version`).

Draft states (docs/hub/ai.md): describing (no valid plan) -> planning ->
planned -> generating -> generated -> accepted; any state but accepted may
be discarded, and a discarded or accepted draft starts no more jobs. An
accepted version is never deleted by anything here.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, func, insert, select, update

from alhazen.hub import catalog, packages
from alhazen.hub.ai import keys
from alhazen.hub.ai.jobs import ERROR_STATUS, Runner, revert_draft
from alhazen.hub.ai.providers import ProviderError
from alhazen.hub.auth import Principal, audit, has_hidden_characters
from alhazen.hub.context import Hub, new_id
from alhazen.hub.errors import HubError, conflict, invalid, not_found
from alhazen.hub.quota import lock_owner
from alhazen.hub.schema import ai_drafts, ai_jobs, experiments, versions
from alhazen.hub.storage import sha256_file
from alhazen.hub.timefmt import iso

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
ACTIVE = ("queued", "running")
DAY_MS = 24 * 3600 * 1000
MAX_PLAN_TITLE = 120
MAX_PLAN_NOTES = 4000


# -- views ---------------------------------------------------------------------


def draft_view(row: Any) -> dict[str, Any]:
    start = (
        {"experiment_id": row.start_experiment_id, "version_id": row.start_version_id}
        if row.start_version_id
        else None
    )
    return {
        "id": row.id,
        "status": row.status,
        "prompt": row.prompt,
        "provider": row.provider,
        "model": row.model,
        "start_from": start,
        "plan_job_id": row.plan_job_id,
        "source_job_id": row.source_job_id,
        "experiment_id": row.experiment_id,
        "version_id": row.version_id,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def job_view(row: Any) -> dict[str, Any]:
    error = None
    if row.error_code is not None:
        error = {
            "code": row.error_code,
            "status": ERROR_STATUS.get(row.error_code, 500),
            "message": row.error_message or "",
        }
    return {
        "id": row.id,
        "draft_id": row.draft_id,
        "kind": row.kind,
        "status": row.status,
        "provider": row.provider,
        "model": row.model,
        "error": error,
        "result": json.loads(row.result_json) if row.result_json else None,
        "usage": json.loads(row.usage_json) if row.usage_json else None,
        "disclosed": json.loads(row.disclosed_json) if row.disclosed_json else None,
        "attempts": int(row.attempts),
        "created_at": iso(row.created_at),
        "started_at": iso(row.started_at),
        "finished_at": iso(row.finished_at),
    }


# -- status and keys ---------------------------------------------------------------


def status(hub: Hub, runner: Runner, principal: Principal | None) -> dict[str, Any]:
    infos = runner.infos()
    out: dict[str, Any] = {
        "enabled": hub.settings.ai.enabled,
        "providers": [info.public() for info in infos.values()],
        "keys": [],
        "limits": {
            "max_prompt_chars": hub.settings.ai.max_prompt_chars,
            "max_active_jobs": hub.settings.ai.max_active_jobs_per_user,
            "max_jobs_per_day": hub.settings.ai.max_jobs_per_day,
        },
    }
    if principal is not None:
        with hub.db.transaction() as conn:
            out["keys"] = keys.list_keys(conn, principal.user_id)
    return out


def put_key(
    hub: Hub, runner: Runner, principal: Principal, provider: str, body: dict[str, Any]
) -> dict[str, Any]:
    keys.require_enabled(hub)
    provider = keys.check_provider(provider)
    unknown = sorted(set(body) - {"key", "verify"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    key = keys.check_key_shape(body.get("key"))
    verify = body.get("verify", False)
    if not isinstance(verify, bool):
        raise invalid("verify must be true or false")
    result: dict[str, Any] = {}
    if verify:
        result = _verify(runner, provider, key)
    view = keys.store(hub, principal.user_id, provider, key)
    del key
    return {**view, **result}


def _verify(runner: Runner, provider: str, key: str) -> dict[str, Any]:
    """One cheap authenticated call. A refused key is not stored."""
    info = runner.infos()[provider]
    client = runner.make_client(provider, key, info.default_model)
    check = getattr(client, "verify", None)
    if not callable(check):
        return {"verified": False, "verify_error": "unsupported"}
    try:
        check()
    except ProviderError as exc:
        if exc.kind == "auth":
            raise HubError(
                400, "key_rejected", f"{info.name} refused this key; nothing was stored"
            ) from None
        code = "provider_timeout" if exc.kind == "timeout" else "provider_error"
        return {"verified": False, "verify_error": code}
    return {"verified": True}


def delete_key(hub: Hub, principal: Principal, provider: str) -> None:
    provider = keys.check_provider(provider)
    if not keys.remove(hub, principal.user_id, provider):
        raise not_found("No key stored for that provider")


# -- admission -------------------------------------------------------------------


def _admit(conn: Connection, hub: Hub, user_id: str, now: int) -> None:
    """429 when the user is at the active-job or daily limit (under the owner
    lock, so two concurrent requests cannot both pass)."""
    ai = hub.settings.ai
    lock_owner(conn, user_id)
    active = conn.execute(
        select(func.count())
        .select_from(ai_jobs)
        .where(ai_jobs.c.user_id == user_id, ai_jobs.c.status.in_(ACTIVE))
    ).scalar_one()
    if active >= ai.max_active_jobs_per_user:
        raise HubError(
            429,
            "ai_jobs_busy",
            f"You already have {active} AI jobs running, the most one account may run at once",
            headers={"Retry-After": "10"},
        )
    today = conn.execute(
        select(func.count())
        .select_from(ai_jobs)
        .where(ai_jobs.c.user_id == user_id, ai_jobs.c.created_at > now - DAY_MS)
    ).scalar_one()
    if today >= ai.max_jobs_per_day:
        raise HubError(
            429,
            "ai_daily_limit",
            f"You have started {today} AI jobs in the last 24 hours, this hub's daily limit",
            headers={"Retry-After": "3600"},
        )


def _enqueue(
    conn: Connection,
    hub: Hub,
    draft: Any,
    kind: str,
    request: dict[str, Any],
    now: int,
) -> str:
    _admit(conn, hub, draft.user_id, now)
    job_id = new_id()
    conn.execute(
        insert(ai_jobs).values(
            id=job_id,
            user_id=draft.user_id,
            draft_id=draft.id,
            kind=kind,
            status="queued",
            provider=draft.provider,
            model=draft.model,
            request_json=json.dumps(request, sort_keys=True),
            result_json=None,
            error_code=None,
            error_message=None,
            usage_json=None,
            disclosed_json=None,
            attempts=0,
            created_at=now,
            started_at=None,
            finished_at=None,
            lease_until=None,
            token=None,
        )
    )
    pointer = "plan_job_id" if kind == "plan" else "source_job_id"
    values: dict[str, Any] = {
        pointer: job_id,
        "status": "planning" if kind == "plan" else "generating",
        "updated_at": now,
    }
    if kind == "plan":
        values.update(plan_json=None, source_job_id=None)
    conn.execute(update(ai_drafts).where(ai_drafts.c.id == draft.id).values(**values))
    return job_id


def _prompt(hub: Hub, value: object) -> str:
    limit = hub.settings.ai.max_prompt_chars
    if not isinstance(value, str) or not value.strip():
        raise invalid("prompt is required")
    value = _lines(value).strip()
    if len(value) > limit:
        raise invalid(f"prompt may be at most {limit} characters", "prompt_too_long")
    if has_hidden_characters(value, multiline=True):
        raise invalid("prompt contains control or invisible characters")
    return value


def _lines(value: str) -> str:
    """Pasted text keeps its line breaks as plain newlines."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _model(runner: Runner, provider: str, value: object) -> str:
    if value is None or value == "":
        return runner.infos()[provider].default_model
    if not isinstance(value, str) or not _MODEL.match(value) or ".." in value:
        raise invalid("model must be a provider model name", "invalid_model")
    return value


# -- drafts ------------------------------------------------------------------------


def create_draft(
    hub: Hub, runner: Runner, principal: Principal, body: dict[str, Any]
) -> dict[str, Any]:
    keys.require_enabled(hub)
    unknown = sorted(set(body) - {"prompt", "start_from", "provider", "model"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    prompt = _prompt(hub, body.get("prompt"))
    provider = keys.check_provider(body.get("provider"))
    model = _model(runner, provider, body.get("model"))
    start = body.get("start_from")
    start_exp = start_ver = None
    if start is not None:
        if (
            not isinstance(start, dict)
            or set(start) != {"experiment_id", "version_id"}
            or not all(isinstance(start[k], str) for k in start)
        ):
            raise invalid("start_from must be {experiment_id, version_id} or null")
        start_exp, start_ver = start["experiment_id"], start["version_id"]
    now = hub.clock()
    with hub.db.transaction() as conn:
        if not keys.has_key(conn, principal.user_id, provider):
            raise keys.key_required(provider)
        if start_exp is not None and start_ver is not None:
            catalog.readable_version(conn, principal.user_id, start_exp, start_ver)
        draft_id = new_id()
        conn.execute(
            insert(ai_drafts).values(
                id=draft_id,
                user_id=principal.user_id,
                experiment_id=None,
                start_experiment_id=start_exp,
                start_version_id=start_ver,
                provider=provider,
                model=model,
                prompt=prompt,
                plan_json=None,
                plan_job_id=None,
                source_job_id=None,
                version_id=None,
                status="describing",
                created_at=now,
                updated_at=now,
            )
        )
        draft = _draft(conn, principal, draft_id)
        job_id = _enqueue(conn, hub, draft, "plan", {}, now)
        audit(
            conn,
            now,
            f"user:{principal.user_id}",
            "ai.draft.create",
            f"ai-draft:{draft_id}",
            {"provider": provider, "model": model, "start_version": start_ver},
        )
        return {"draft": draft_view(_draft(conn, principal, draft_id)), "job": _job(conn, job_id)}


def _draft(conn: Connection, principal: Principal, draft_id: str, *, lock: bool = False) -> Any:
    query = select(ai_drafts).where(
        ai_drafts.c.id == draft_id, ai_drafts.c.user_id == principal.user_id
    )
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).first()
    if row is None:
        raise not_found("Draft not found")
    return row


def _job(conn: Connection, job_id: str) -> dict[str, Any]:
    return job_view(conn.execute(select(ai_jobs).where(ai_jobs.c.id == job_id)).one())


def list_drafts(hub: Hub, principal: Principal, limit: int, offset: int) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        rows = conn.execute(
            select(ai_drafts)
            .where(ai_drafts.c.user_id == principal.user_id, ai_drafts.c.status != "discarded")
            .order_by(ai_drafts.c.created_at.desc(), ai_drafts.c.id)
            .limit(limit + 1)
            .offset(offset)
        ).all()
    return {
        "items": [draft_view(r) for r in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def get_draft(hub: Hub, principal: Principal, draft_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        row = _draft(conn, principal, draft_id)
        jobs = conn.execute(
            select(ai_jobs)
            .where(ai_jobs.c.draft_id == draft_id)
            .order_by(ai_jobs.c.created_at, ai_jobs.c.id)
        ).all()
    return {
        "draft": draft_view(row),
        "plan": json.loads(row.plan_json) if row.plan_json else None,
        "jobs": [job_view(j) for j in jobs],
    }


def _usable(row: Any) -> None:
    if row.status in ("accepted", "discarded"):
        raise conflict(f"draft_{row.status}", f"This draft is {row.status}; start a new one")


def replan(hub: Hub, principal: Principal, draft_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Plan again (after a failed plan, or to revise one), optionally with a
    new prompt. Clears any generated source pointer."""
    keys.require_enabled(hub)
    unknown = sorted(set(body) - {"prompt"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _draft(conn, principal, draft_id, lock=True)
        _usable(row)
        if row.status in ("planning", "generating"):
            raise conflict("draft_busy", "A job is already running for this draft")
        if "prompt" in body:
            conn.execute(
                update(ai_drafts)
                .where(ai_drafts.c.id == draft_id)
                .values(prompt=_prompt(hub, body["prompt"]))
            )
        if not keys.has_key(conn, principal.user_id, row.provider):
            raise keys.key_required(row.provider)
        job_id = _enqueue(conn, hub, row, "plan", {}, now)
        return {"job": _job(conn, job_id)}


def generate(hub: Hub, principal: Principal, draft_id: str, body: dict[str, Any]) -> dict[str, Any]:
    keys.require_enabled(hub)
    unknown = sorted(set(body) - {"plan_edits"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    edits = body.get("plan_edits") or {}
    if not isinstance(edits, dict) or set(edits) - {"title", "notes"}:
        raise invalid("plan_edits may hold only title and notes")
    edits = {k: _lines(v) if isinstance(v, str) else v for k, v in edits.items()}
    for name, limit in (("title", MAX_PLAN_TITLE), ("notes", MAX_PLAN_NOTES)):
        if name in edits and (
            not isinstance(edits[name], str)
            or len(edits[name]) > limit
            or has_hidden_characters(edits[name], multiline=name == "notes")
        ):
            raise invalid(f"plan_edits.{name} must be text of at most {limit} characters")
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _draft(conn, principal, draft_id, lock=True)
        _usable(row)
        if row.status not in ("planned", "generated") or not row.plan_json:
            raise conflict("plan_required", "This draft has no accepted plan to generate from")
        if not keys.has_key(conn, principal.user_id, row.provider):
            raise keys.key_required(row.provider)
        plan = json.loads(row.plan_json)
        if edits:
            plan.update({k: v.strip() if k == "title" else v for k, v in edits.items()})
            conn.execute(
                update(ai_drafts)
                .where(ai_drafts.c.id == draft_id)
                .values(plan_json=json.dumps(plan))
            )
        job_id = _enqueue(conn, hub, row, "source", {"plan": plan}, now)
        return {"job": _job(conn, job_id)}


def discard(hub: Hub, principal: Principal, draft_id: str) -> None:
    """Discard a draft: cancel its jobs and remove its generated packages.
    An accepted version stays exactly as it is."""
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _draft(conn, principal, draft_id, lock=True)
        if row.status == "discarded":
            return
        conn.execute(
            update(ai_jobs)
            .where(ai_jobs.c.draft_id == draft_id, ai_jobs.c.status.in_(ACTIVE))
            .values(status="cancelled", finished_at=now, lease_until=None, token=None)
        )
        conn.execute(
            update(ai_drafts)
            .where(ai_drafts.c.id == draft_id)
            .values(status="discarded", updated_at=now)
        )
        audit(
            conn, now, f"user:{principal.user_id}", "ai.draft.discard", f"ai-draft:{draft_id}", {}
        )
    hub.store.remove_ai_draft(draft_id)


def get_job(hub: Hub, principal: Principal, job_id: str) -> dict[str, Any]:
    with hub.db.transaction() as conn:
        row = conn.execute(
            select(ai_jobs).where(ai_jobs.c.id == job_id, ai_jobs.c.user_id == principal.user_id)
        ).first()
        if row is None:
            raise not_found("Job not found")
        return {"job": job_view(row)}


def cancel_job(hub: Hub, principal: Principal, job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job. A running provider call is not
    interrupted, but its result is never recorded and no further call is made."""
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = conn.execute(
            select(ai_jobs)
            .where(ai_jobs.c.id == job_id, ai_jobs.c.user_id == principal.user_id)
            .with_for_update()
        ).first()
        if row is None:
            raise not_found("Job not found")
        if row.status == "cancelled":
            return {"job": job_view(row)}
        if row.status not in ACTIVE:
            raise conflict("job_finished", f"This job has already {row.status}")
        conn.execute(
            update(ai_jobs)
            .where(ai_jobs.c.id == job_id)
            .values(status="cancelled", finished_at=now, lease_until=None, token=None)
        )
        revert_draft(conn, row, now)
        return {"job": _job(conn, job_id)}


# -- acceptance ------------------------------------------------------------------


def _relabelled(runner: Runner, stored: Path, changes: dict[str, str]) -> bytes:
    """The stored generated package rebuilt with the person's title,
    description or license (through the kit, so LICENSE follows a new
    license). The result is validated again by accept_version."""
    info = packages.inspect_bundle(stored)
    with zipfile.ZipFile(stored) as archive:
        files = {entry["path"]: archive.read(entry["path"]) for entry in info.manifest["files"]}
    metadata = {k: v for k, v in info.manifest.items() if k not in ("files", "schema_version")}
    old_license = str(metadata.get("license", ""))
    metadata.update(changes)
    try:
        return runner.kit.relabel(files, metadata, old_license)
    except packages.PackageError as exc:
        raise HubError(422, "invalid_package", str(exc)) from None


def accept(
    hub: Hub, runner: Runner, principal: Principal, draft_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Create the private experiment (once) and a version from the draft's
    validated generated package, through the upload pipeline.

    Order: (1) lock the draft, check it, create its experiment if it has
    none and remember it on the draft; (2) `catalog.accept_version` with a
    private copy of the stored package (it re-runs every package and
    documentation check, quota and version rules); (3) mark the draft
    accepted. A retry after a failure in (2) reuses the experiment; an
    identical package is never stored twice (accept_version returns the
    existing version for identical bytes).
    """
    unknown = sorted(set(body) - {"title", "summary", "license"})
    if unknown:
        raise invalid(f"unknown field(s): {', '.join(unknown)}", "unknown_field")
    now = hub.clock()
    with hub.db.transaction() as conn:
        row = _draft(conn, principal, draft_id, lock=True)
        if row.status == "accepted":
            raise conflict(
                "already_accepted",
                "This draft was already accepted",
                experiment_id=row.experiment_id,
                version_id=row.version_id,
            )
        if row.status != "generated" or row.source_job_id is None:
            raise conflict("not_generated", "This draft has no generated source to accept")
        job = conn.execute(select(ai_jobs).where(ai_jobs.c.id == row.source_job_id)).one()
        result = json.loads(job.result_json) if job.result_json else {}
        if job.status != "done" or not result.get("valid"):
            raise conflict("generation_invalid", "The generated source did not pass validation")
        plan = json.loads(row.plan_json) if row.plan_json else {}
        package = result.get("manifest") or {}
        fields = catalog.parse_metadata(
            {
                "title": body.get("title", package.get("title", plan.get("title", ""))),
                "summary": body.get("summary", str(plan.get("summary", ""))[:280]),
                "license": body.get("license", package.get("license", "")),
                "description": str(plan.get("summary", ""))[:20_000],
            },
            partial=False,
        )
        # What the package itself must say differently (title, description,
        # license): only fields the person set explicitly.
        relabel: dict[str, str] = {}
        if "title" in body and fields["title"] != package.get("title"):
            relabel["title"] = fields["title"]
        if (
            "summary" in body
            and fields["summary"]
            and fields["summary"] != package.get("description")
        ):
            relabel["description"] = fields["summary"]
        if "license" in body and fields["license"] and fields["license"] != package.get("license"):
            relabel["license"] = fields["license"]
        experiment_id = row.experiment_id
        if experiment_id is None:
            experiment_id = catalog.insert_experiment(
                conn, principal.user_id, fields, now, hub.settings.limits.max_experiments_per_owner
            )
            conn.execute(
                update(ai_drafts)
                .where(ai_drafts.c.id == draft_id)
                .values(experiment_id=experiment_id, updated_at=now)
            )
        else:
            conn.execute(
                update(experiments)
                .where(
                    experiments.c.id == experiment_id, experiments.c.owner_id == principal.user_id
                )
                .values(
                    title=fields["title"],
                    summary=fields["summary"],
                    license=fields["license"],
                    updated_at=now,
                )
            )
        source_job_id = row.source_job_id
        key = hub.store.ai_bundle_key(draft_id, source_job_id)
    stored = hub.store.path(key)
    if not stored.is_file():
        raise HubError(
            500, "bundle_missing", "The generated package is no longer stored; generate it again"
        )
    temp = hub.store.new_temp()
    try:
        if relabel:
            temp.write_bytes(_relabelled(runner, stored, relabel))
        else:
            shutil.copyfile(stored, temp)
        digest, size = sha256_file(temp), temp.stat().st_size
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    # accept_version consumes (removes) temp however it ends.
    version, _created = catalog.accept_version(
        hub, principal, experiment_id, temp, digest, size, ai_draft_id=draft_id
    )
    now = hub.clock()
    with hub.db.transaction() as conn:
        marked = conn.execute(
            update(ai_drafts)
            .where(
                ai_drafts.c.id == draft_id,
                ai_drafts.c.status == "generated",
                ai_drafts.c.source_job_id == source_job_id,
            )
            .values(status="accepted", version_id=version["id"], updated_at=now)
        ).rowcount
        if not marked:
            current = _draft(conn, principal, draft_id)
            raise conflict(
                "already_accepted" if current.status == "accepted" else "draft_changed",
                "This draft changed while it was being accepted; reload it",
                experiment_id=current.experiment_id,
                version_id=current.version_id,
            )
        audit(
            conn,
            now,
            f"user:{principal.user_id}",
            "ai.draft.accept",
            f"ai-draft:{draft_id}",
            {"experiment": experiment_id, "version": version["id"]},
        )
        exp = conn.execute(select(experiments).where(experiments.c.id == experiment_id)).one()
        ver = conn.execute(select(versions).where(versions.c.id == version["id"])).one()
        return {
            "experiment": catalog.private_view(conn, exp),
            "version": catalog.owner_version_view(ver),
        }
