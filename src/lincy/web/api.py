"""Web dashboard HTTP routes and static SPA mount.

Interface used by the host (``lincy.host``) to compose the single FastAPI app::

    settings = WebSettings.from_config(config)
    state = WebState()
    app.include_router(create_router(settings, state))   # with the other API routers
    # inside the app lifespan:
    async with web_lifespan(app, settings, state): ...
    mount_static(app, settings)                          # last: catch-all SPA fallback

This module never imports ``lincy.host``; agent events (``/api/agent/events``)
and every other ``/api/agent/*`` route belong to the host's control router.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cache import MetricsCache
from .context_composition import analyze_latest_brain_request
from .settings import WebSettings
from .state import WebState


def create_router(settings: WebSettings, state: WebState) -> APIRouter:
    router = APIRouter()

    def _cache() -> MetricsCache:
        # web_lifespan is still loading, or failed (health reports web=unavailable).
        if state.cache is None:
            raise HTTPException(status_code=503, detail="dashboard data is not available")
        return state.cache

    @router.get("/api/dashboard")
    async def dashboard(
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
    ) -> dict:
        today = date.today()
        df = date_from or today
        dt = date_to or today
        summary = _cache().get_dashboard(df, dt)
        return {
            "date_from": summary.date_from.isoformat(),
            "date_to": summary.date_to.isoformat(),
            "total_cost": summary.total_cost,
            "total_turns": summary.total_turns,
            "total_sessions": summary.total_sessions,
            "total_prompt_tokens": summary.total_prompt_tokens,
            "read_cache_rate": summary.read_cache_rate,
            "total_cache_read": summary.total_cache_read,
            "total_cache_write": summary.total_cache_write,
            "cache_hit_rate": summary.cache_hit_rate,
            "write_cache_measurable": summary.write_cache_measurable,
            "daily_costs": summary.daily_costs,
            "pricing_sources": summary.pricing_sources,
            "pricing_stale": summary.pricing_stale,
        }

    @router.get("/api/sessions")
    async def sessions(
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict:
        today = date.today()
        df = date_from or (today - timedelta(days=30))
        dt = date_to or today
        all_sessions = _cache().get_sessions_in_range(df, dt)
        page = all_sessions[offset : offset + limit]
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "status": s.status,
                    "created_at": s.created_at.isoformat(),
                    "updated_at": s.updated_at.isoformat(),
                    "turn_count": s.turn_count,
                    "total_cost": s.total_cost,
                    "read_cache_rate": s.read_cache_rate,
                    "cache_hit_rate": s.cache_hit_rate,
                    "total_cache_write": s.total_cache_write,
                    "write_cache_measurable": s.write_cache_measurable,
                    "peak_prompt_tokens": s.peak_prompt_tokens,
                    "pricing_sources": s.pricing_sources,
                    "pricing_stale": s.pricing_stale,
                }
                for s in page
            ],
            "total": len(all_sessions),
        }

    @router.get("/api/requests")
    async def all_requests(
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        today = date.today()
        df = date_from or (today - timedelta(days=30))
        dt = date_to or today
        all_reqs = _cache().get_all_requests(df, dt)
        page = all_reqs[offset : offset + limit]
        return {
            "requests": page,
            "total": len(all_reqs),
        }

    @router.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> dict:
        detail = _cache().get_session_detail(session_id)
        if detail is None:
            return {"error": "session not found"}
        return detail

    @router.get("/api/live")
    async def live() -> dict:
        status = _cache().get_live_status(settings.soft_limit_tokens)
        if status is None:
            return {"active": False}
        return status

    @router.get("/api/context/composition")
    async def context_composition() -> dict:
        # requests.jsonl holds full message payloads (10MB+) and is never
        # cached (see docs/dev/web-dashboard.md); parse it on demand here,
        # off the event loop since it's blocking file IO + CPU work.
        return await run_in_threadpool(
            analyze_latest_brain_request,
            settings.sessions_dir,
            settings.soft_limit_tokens,
        )

    @router.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await state.ws.connect(ws)
        try:
            while True:
                # Keep connection alive; client can send pings
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state.ws.disconnect(ws)

    return router


def mount_static(app: FastAPI, settings: WebSettings) -> None:
    """Serve the built Vue SPA. Call after every API router is included:
    the fallback route matches any path."""
    static_dir = settings.static_dir
    assets_dir = static_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # Serve static files from public root (favicon.svg, icons.svg, etc.)
        candidate = static_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(static_dir / "index.html"))
