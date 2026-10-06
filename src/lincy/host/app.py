"""FastAPI app assembly: control router always, web dashboard optionally.

Expected ``lincy.web`` interface (written in parallel by the web phase):

    lincy.web.settings.WebSettings.from_config(config: AppConfig) -> WebSettings
    lincy.web.state.WebState()  (shared by the router and the lifespan)
    lincy.web.api.create_router(settings: WebSettings, state: WebState) -> APIRouter
    lincy.web.api.mount_static(app: FastAPI, settings: WebSettings) -> None
    lincy.web.lifespan.web_lifespan(app: FastAPI, settings: WebSettings, state: WebState)
        async context manager; sets app.state.web_status to
        "loading" / "ready" / "unavailable" and never raises for data
        initialization failures (web is an observer, not fatal).

The web modules are imported inside include_web() so tests can build the
control-only app without them. The host forbids lazy imports because a
module first imported after `git pull` would load unverified code; this one
is safe because include_web() runs once at startup, before any upgrade can
begin.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..agent.handle import AgentHandle
from ..agent.ui_event_stream import UiEventStore
from ..core.schema import AppConfig
from .control_api import RuntimeInfo, create_control_router, install_error_handlers
from .upgrade import UpgradeManager


def include_web(app: FastAPI, config: AppConfig) -> None:
    """Add dashboard routes, the web lifespan and the SPA static mount."""
    from ..web.api import create_router, mount_static
    from ..web.lifespan import web_lifespan
    from ..web.settings import WebSettings
    from ..web.state import WebState

    settings = WebSettings.from_config(config)
    state = WebState()
    app.include_router(create_router(settings, state))
    app.router.lifespan_context = lambda app: web_lifespan(app, settings, state)
    # The SPA fallback catches every unmatched path, so it must be mounted last.
    mount_static(app, settings)


def create_app(
    *,
    handle: AgentHandle,
    event_store: UiEventStore,
    info: RuntimeInfo,
    upgrade: UpgradeManager,
    config: AppConfig,
    web: bool = True,
) -> FastAPI:
    app = FastAPI(title="lincy", docs_url=None, redoc_url=None)
    install_error_handlers(app)
    app.include_router(create_control_router(handle, info, upgrade, event_store))
    if web:
        include_web(app, config)
    return app
