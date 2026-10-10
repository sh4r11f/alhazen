"""HTTP routes of AI-assisted authoring, under /api/hub/v1/ai and the
library's DELETE (docs/hub/api-contract.md "AI authoring").

Hides nothing of its own: each route reads its body through the app's
bounded JSON reader, resolves the caller with the app's member/writer checks
(cookie writes need Origin + CSRF, as everywhere), and calls one service
function in a worker thread. Every authorization decision is in
`alhazen.hub.ai.drafts`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from alhazen.hub import catalog
from alhazen.hub.ai import drafts
from alhazen.hub.ai.jobs import AIWorker
from alhazen.hub.auth import Principal
from alhazen.hub.context import Hub

API = "/api/hub/v1"


@dataclass(frozen=True)
class RouteKit:
    """The app's request helpers the routes reuse (defined in app.create_app)."""

    reader: Callable[[Request], Awaitable[Principal | None]]
    member: Callable[[Request], Awaitable[Principal]]
    writer: Callable[[Request], Awaitable[Principal]]
    read_json: Callable[[Request, int], Awaitable[dict[str, Any]]]
    ident: Callable[[str, str], str]
    page: Callable[[Request], tuple[int, int]]
    call: Callable[..., Awaitable[Any]]


def add_routes(app: FastAPI, hub: Hub, worker: AIWorker, kit: RouteKit) -> None:
    runner = worker.runner
    limit = hub.settings.limits.max_json_bytes
    call = kit.call

    @app.get(f"{API}/ai/status")
    async def ai_status(request: Request) -> dict[str, Any]:
        principal = await kit.reader(request)
        return await call(drafts.status, hub, runner, principal)

    @app.put(API + "/ai/keys/{provider}")
    async def put_key(request: Request, provider: str) -> dict[str, Any]:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        return await call(drafts.put_key, hub, runner, principal, provider, body)

    @app.delete(API + "/ai/keys/{provider}", status_code=204)
    async def delete_key(request: Request, provider: str) -> Response:
        principal = await kit.writer(request)
        await call(drafts.delete_key, hub, principal, provider)
        return Response(status_code=204)

    @app.post(f"{API}/ai/drafts")
    async def create_draft(request: Request) -> JSONResponse:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        created = await call(drafts.create_draft, hub, runner, principal, body)
        worker.wake()
        return JSONResponse(created, status_code=202)

    @app.get(f"{API}/ai/drafts")
    async def list_drafts(request: Request) -> dict[str, Any]:
        principal = await kit.member(request)
        page_limit, offset = kit.page(request)
        return await call(drafts.list_drafts, hub, principal, page_limit, offset)

    @app.get(API + "/ai/drafts/{draft_id}")
    async def get_draft(request: Request, draft_id: str) -> dict[str, Any]:
        principal = await kit.member(request)
        return await call(drafts.get_draft, hub, principal, kit.ident(draft_id, "Draft not found"))

    @app.delete(API + "/ai/drafts/{draft_id}", status_code=204)
    async def discard_draft(request: Request, draft_id: str) -> Response:
        principal = await kit.writer(request)
        await call(drafts.discard, hub, principal, kit.ident(draft_id, "Draft not found"))
        return Response(status_code=204)

    @app.post(API + "/ai/drafts/{draft_id}/plan")
    async def replan(request: Request, draft_id: str) -> JSONResponse:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        job = await call(
            drafts.replan, hub, principal, kit.ident(draft_id, "Draft not found"), body
        )
        worker.wake()
        return JSONResponse(job, status_code=202)

    @app.post(API + "/ai/drafts/{draft_id}/generate")
    async def generate(request: Request, draft_id: str) -> JSONResponse:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        job = await call(
            drafts.generate, hub, principal, kit.ident(draft_id, "Draft not found"), body
        )
        worker.wake()
        return JSONResponse(job, status_code=202)

    @app.post(API + "/ai/drafts/{draft_id}/repair")
    async def repair(request: Request, draft_id: str) -> JSONResponse:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        job = await call(
            drafts.repair, hub, principal, kit.ident(draft_id, "Draft not found"), body
        )
        worker.wake()
        return JSONResponse(job, status_code=202)

    @app.post(API + "/ai/drafts/{draft_id}/accept")
    async def accept(request: Request, draft_id: str) -> dict[str, Any]:
        principal = await kit.writer(request)
        body = await kit.read_json(request, limit)
        return await call(
            drafts.accept, hub, runner, principal, kit.ident(draft_id, "Draft not found"), body
        )

    @app.get(API + "/ai/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> dict[str, Any]:
        principal = await kit.member(request)
        return await call(drafts.get_job, hub, principal, kit.ident(job_id, "Job not found"))

    @app.post(API + "/ai/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
        principal = await kit.writer(request)
        return await call(drafts.cancel_job, hub, principal, kit.ident(job_id, "Job not found"))

    # The library's missing remove (the UI's Remove button).
    @app.delete(API + "/library/{experiment_id}", status_code=204)
    async def library_remove(request: Request, experiment_id: str) -> Response:
        principal = await kit.writer(request)
        await call(
            catalog.library_remove,
            hub,
            principal,
            kit.ident(experiment_id, "Experiment not found"),
        )
        return Response(status_code=204)
