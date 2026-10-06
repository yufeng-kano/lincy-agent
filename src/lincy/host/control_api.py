"""Control API: /api/agent/*, the HTTP face of AgentHandle plus health and upgrade."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..agent.handle import AgentError, AgentHandle
from ..agent.ui_event_stream import UiEventStore
from .errors import HostError
from .upgrade import UpgradeManager


@dataclass(frozen=True)
class RuntimeInfo:
    started_at: str
    git_sha: str


class MessageRequest(BaseModel):
    content: str
    channel: str = "cli"


class ReloadRequest(BaseModel):
    target: Literal["all", "system-prompt"] = "all"


class ShellInputRequest(BaseModel):
    text: str | None = None
    key: str | None = None


_ACCEPTED = {"status": "accepted"}


def install_error_handlers(app: FastAPI) -> None:
    """Map AgentError / HostError onto {error} with their status code."""

    async def _error_response(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    app.add_exception_handler(AgentError, _error_response)
    app.add_exception_handler(HostError, _error_response)


def create_control_router(
    handle: AgentHandle,
    info: RuntimeInfo,
    upgrade: UpgradeManager,
    event_store: UiEventStore,
) -> APIRouter:
    router = APIRouter(prefix="/api/agent")

    @router.get("/health")
    def health(request: Request) -> dict:
        return {
            "status": "ok",
            "state": handle.state().value,
            "pid": os.getpid(),
            "session_id": handle.session_id(),
            "started_at": info.started_at,
            "git_sha": info.git_sha,
            "upgrade": asdict(upgrade.status()),
            "web": getattr(request.app.state, "web_status", "disabled"),
        }

    @router.post("/shutdown", status_code=202)
    def shutdown() -> dict:
        handle.request_shutdown(graceful=True)
        return {"status": "shutting_down"}

    @router.post("/upgrade", status_code=202)
    def start_upgrade() -> dict:
        started = upgrade.start(handle)
        return {"status": "started", "from_sha": started.from_sha}

    @router.get("/channels")
    def channels() -> dict:
        return {"channels": handle.channels()}

    @router.post("/messages", status_code=202)
    def submit_message(body: MessageRequest) -> dict:
        handle.submit(body.content, body.channel)
        return {"status": "accepted", "channel": body.channel}

    @router.post("/turn/cancel", status_code=202)
    def cancel_turn() -> dict:
        handle.cancel_turn()
        return _ACCEPTED

    @router.post("/session/new", status_code=202)
    def new_session() -> dict:
        handle.request_new_session()
        return _ACCEPTED

    @router.post("/session/compact", status_code=202)
    def compact_session() -> dict:
        handle.request_compact()
        return _ACCEPTED

    @router.post("/session/clear", status_code=202)
    def clear_session() -> dict:
        handle.request_clear()
        return _ACCEPTED

    @router.post("/reload", status_code=202)
    def reload(body: ReloadRequest = ReloadRequest()) -> dict:
        handle.request_reload(body.target)
        return {"status": "accepted", "target": body.target}

    @router.get("/events")
    def events(limit: int = Query(500, ge=1, le=2000)) -> dict:
        # Current run only: the store rotates its file on every start.
        return {
            "events": [
                record.model_dump(mode="json") for record in event_store.recent_events(limit)
            ]
        }

    @router.get("/shell/sessions")
    def shell_sessions() -> dict:
        return {"sessions": handle.shell_sessions()}

    @router.post("/shell/sessions/{session_id}/input")
    def shell_input(session_id: str, body: ShellInputRequest) -> dict:
        return {"result": handle.shell_send(session_id, text=body.text, key=body.key)}

    @router.post("/shell/sessions/{session_id}/cancel")
    def shell_cancel(session_id: str) -> dict:
        return {"result": handle.shell_cancel(session_id)}

    return router
