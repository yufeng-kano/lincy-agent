"""Web dashboard lifespan: pricing, metrics cache, and file watchers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .cache import MetricsCache
from .pricing import fetch_pricing
from .settings import WebSettings
from .state import WebState
from .watcher import watch_sessions, watch_ui_events

logger = logging.getLogger(__name__)


async def _initialize(
    app: FastAPI,
    settings: WebSettings,
    state: WebState,
    stop: asyncio.Event,
    watchers: list[asyncio.Task],
) -> None:
    # Agent UI events must stream from the first second: the agent accepts
    # messages as soon as it is ready, and the watcher reads from the current
    # end of file, so starting it after the pricing fetch would drop events.
    watchers.append(
        asyncio.create_task(watch_ui_events(settings.ui_events_path, state.ws.broadcast, stop))
    )
    try:
        pricing = await fetch_pricing(
            settings.pricing_url,
            settings.pricing_cache_path,
            settings.pricing_cache_ttl_hours,
        )
        cache = MetricsCache(settings.sessions_dir, pricing)
        # Reading every session file is blocking IO; keep the event loop serving.
        await asyncio.to_thread(cache.refresh_all)
        state.cache = cache
        logger.info("Loaded %d sessions from %s", len(cache._files), settings.sessions_dir)
        watchers.append(
            asyncio.create_task(
                watch_sessions(
                    settings.sessions_dir,
                    cache,
                    state.ws.broadcast,
                    stop,
                    soft_limit=settings.soft_limit_tokens,
                )
            )
        )
    except Exception:
        # The dashboard is an observer; it must never take the agent down.
        logger.exception("Web dashboard initialization failed")
        app.state.web_status = "unavailable"
        return
    app.state.web_status = "ready"


@asynccontextmanager
async def web_lifespan(app: FastAPI, settings: WebSettings, state: WebState) -> AsyncIterator[None]:
    """Run dashboard initialization in the background of the server lifespan.

    Initialization runs as a task instead of before ``yield`` because the
    pricing fetch may take up to its HTTP timeout, and the server (and
    /api/agent/health) must come up without waiting for it.
    ``app.state.web_status`` goes loading -> ready | unavailable.
    """
    app.state.web_status = "loading"
    stop = asyncio.Event()
    watchers: list[asyncio.Task] = []
    init_task = asyncio.create_task(_initialize(app, settings, state, stop, watchers))
    try:
        yield
    finally:
        stop.set()
        for task in (init_task, *watchers):
            task.cancel()
        for task in (init_task, *watchers):
            try:
                await task
            except asyncio.CancelledError:
                pass
