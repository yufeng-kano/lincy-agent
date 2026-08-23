"""FastAPI app for the monitoring web API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from lincy.agent.ui_event_stream import UiEventStore
from lincy.agent.web_chat import WebChatMessageRequest, WebChatStore

from .cache import MetricsCache
from .context_composition import analyze_latest_brain_request
from .pricing import fetch_pricing
from .settings import WebApiSettings
from .watcher import watch_sessions, watch_ui_events, watch_web_chat_events

logger = logging.getLogger(__name__)


async def _post_web_chat_message_to_control(
    settings: WebApiSettings,
    text: str,
    channel: str = "cli",
) -> tuple[int, dict]:
    """Forward one remote-TUI message to chat-cli's control API."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{settings.control_base_url}/web-chat/messages",
                json={"content": text, "channel": channel},
            )
    except httpx.RequestError:
        return 503, {"error": "chat-cli control API is unavailable"}

    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text or "invalid control API response"}
    return response.status_code, payload


async def _fetch_channels_from_control(
    settings: WebApiSettings,
) -> tuple[int, dict]:
    """Fetch selectable send channels from chat-cli's control API."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.control_base_url}/channels")
    except httpx.RequestError:
        return 503, {"error": "chat-cli control API is unavailable"}

    try:
        payload = response.json()
    except ValueError:
        payload = {"error": response.text or "invalid control API response"}
    return response.status_code, payload


class _WebSocketManager:
    """Tracks connected WebSocket clients and broadcasts messages."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


def create_app(settings: WebApiSettings) -> FastAPI:
    ws_manager = _WebSocketManager()
    cache_holder: dict[str, MetricsCache] = {}
    watcher_stop = asyncio.Event()
    chat_store = WebChatStore(settings.web_chat_events_path)
    ui_event_store = UiEventStore(settings.ui_events_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # Startup: load pricing, build cache, start watcher
        pricing = await fetch_pricing(
            settings.pricing_url,
            settings.pricing_cache_path,
            settings.pricing_cache_ttl_hours,
        )
        cache = MetricsCache(settings.sessions_dir, pricing)
        cache.refresh_all()
        cache_holder["cache"] = cache
        logger.info(
            "Loaded %d sessions from %s", len(cache._files), settings.sessions_dir
        )

        # Start file watcher
        watcher_task = asyncio.create_task(
            watch_sessions(
                settings.sessions_dir,
                cache,
                ws_manager.broadcast,
                watcher_stop,
                soft_limit=settings.soft_limit_tokens,
            )
        )
        chat_watcher_task = asyncio.create_task(
            watch_web_chat_events(
                settings.web_chat_events_path,
                ws_manager.broadcast,
                watcher_stop,
            )
        )
        ui_event_watcher_task = asyncio.create_task(
            watch_ui_events(
                settings.ui_events_path,
                ws_manager.broadcast,
                watcher_stop,
            )
        )
        yield
        # Shutdown
        watcher_stop.set()
        for task in (watcher_task, chat_watcher_task, ui_event_watcher_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="chat-web-api", docs_url=None, redoc_url=None, lifespan=lifespan)

    def _cache() -> MetricsCache:
        return cache_holder["cache"]

    # --- Health ---

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- REST API ---

    @app.get("/api/dashboard")
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

    @app.get("/api/sessions")
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

    @app.get("/api/requests")
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

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> dict:
        detail = _cache().get_session_detail(session_id)
        if detail is None:
            return {"error": "session not found"}
        return detail

    @app.get("/api/live")
    async def live() -> dict:
        status = _cache().get_live_status(settings.soft_limit_tokens)
        if status is None:
            return {"active": False}
        return status

    @app.get("/api/context/composition")
    async def context_composition() -> dict:
        # requests.jsonl holds full message payloads (10MB+) and is never
        # cached (see docs/dev/web-dashboard.md); parse it on demand here,
        # off the event loop since it's blocking file IO + CPU work.
        return await run_in_threadpool(
            analyze_latest_brain_request,
            settings.sessions_dir,
            settings.soft_limit_tokens,
        )

    @app.get("/api/chat/events")
    async def chat_events(limit: int = Query(200, ge=1, le=1000)) -> dict:
        return {
            "events": [
                event.model_dump(mode="json")
                for event in chat_store.recent_events(limit)
            ]
        }

    @app.get("/api/agent/events")
    async def agent_events(limit: int = Query(500, ge=1, le=2000)) -> dict:
        # Current run only: the store rotates its file on every chat-cli start.
        return {
            "events": [
                record.model_dump(mode="json")
                for record in ui_event_store.recent_events(limit)
            ]
        }

    @app.get("/api/chat/channels")
    async def chat_channels() -> JSONResponse:
        status_code, payload = await _fetch_channels_from_control(settings)
        return JSONResponse(payload, status_code=status_code)

    @app.post("/api/chat/messages")
    async def chat_message(request: WebChatMessageRequest) -> JSONResponse:
        text = request.content.strip()
        if not text:
            return JSONResponse({"error": "content is required"}, status_code=400)
        channel = (request.channel or "cli").strip().lower() or "cli"

        status_code, payload = await _post_web_chat_message_to_control(
            settings,
            text,
            channel=channel,
        )
        return JSONResponse(payload, status_code=status_code)

    # --- WebSocket ---

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws_manager.connect(ws)
        try:
            while True:
                # Keep connection alive; client can send pings
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.disconnect(ws)

    # --- Static files (Vue SPA) ---

    if settings.static_dir and (settings.static_dir / "index.html").exists():
        assets_dir = settings.static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> FileResponse:
            # Serve static files from public root (favicon.svg, icons.svg, etc.)
            candidate = settings.static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(settings.static_dir / "index.html"))

    return app
