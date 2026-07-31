"""Control API server for external process management.

Runs a FastAPI app in a daemon thread via uvicorn, exposing
/health, /shutdown, /session/new, and /reload endpoints for supervisor integration.
Also exposes the remote-TUI submit path used by chat_web_api.
"""

import logging
import socket
import threading
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import uvicorn
from pydantic import ValidationError

from .agent.web_chat import WebChatMessageRequest

logger = logging.getLogger(__name__)

# (content, channel) -> response payload dict
TuiSubmitFn = Callable[[str, str], dict]
ChannelsFn = Callable[[], list[str]]


def _port_is_available(host: str, port: int) -> bool:
    """Return False when the requested bind address is already occupied."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = host
    if host == "localhost":
        bind_host = "127.0.0.1"
        family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
        except OSError:
            return False
    return True


def _probe_http_host(bind_host: str) -> str:
    """Map wildcard bind hosts to a local address for probe requests."""
    if bind_host in ("0.0.0.0", "localhost"):
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


def _looks_like_control_api(host: str, port: int) -> bool:
    """Best-effort probe to detect an existing chat-cli control server."""
    probe_host = _probe_http_host(host)
    url = f"http://{probe_host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=1.0)
    except Exception:
        return False
    if resp.status_code != 200:
        return False
    try:
        payload = resp.json()
    except ValueError:
        return False
    return payload == {"status": "ok"}


def _assert_control_slot_available(host: str, port: int) -> None:
    """Fail fast when another chat-cli instance already owns the control port."""
    if _port_is_available(host, port):
        return
    if _looks_like_control_api(host, port):
        raise RuntimeError(
            f"chat-cli control API is already running on {host}:{port}; "
            "another chat-cli instance is likely active"
        )
    raise RuntimeError(f"Control API address {host}:{port} is already in use")


def create_app(
    shutdown_fn: Callable[[], None],
    new_session_fn: Callable[[], None] | None = None,
    reload_fn: Callable[[], None] | None = None,
    tui_submit_fn: TuiSubmitFn | None = None,
    channels_fn: ChannelsFn | None = None,
    # Legacy alias kept so older call sites/tests keep working during the rename.
    web_chat_submit_fn: TuiSubmitFn | None = None,
) -> FastAPI:
    """Build FastAPI app with shutdown/health/remote-TUI endpoints."""
    submit_fn = tui_submit_fn or web_chat_submit_fn
    app = FastAPI(title="chat-agent-control", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/shutdown")
    def shutdown() -> JSONResponse:
        shutdown_fn()
        return JSONResponse({"status": "shutting_down"})

    @app.post("/session/new")
    def new_session() -> JSONResponse:
        if new_session_fn is None:
            return JSONResponse(
                {"error": "new-session is not supported"},
                status_code=404,
            )
        new_session_fn()
        return JSONResponse({"status": "new_session_requested"})

    @app.post("/reload")
    def reload() -> JSONResponse:
        if reload_fn is None:
            return JSONResponse(
                {"error": "reload is not supported"},
                status_code=404,
            )
        reload_fn()
        return JSONResponse({"status": "reload_requested"})

    @app.get("/channels")
    def list_channels() -> JSONResponse:
        if channels_fn is None:
            return JSONResponse(
                {"error": "channels listing is not available"},
                status_code=503,
            )
        return JSONResponse({"channels": channels_fn()})

    @app.post("/web-chat/messages")
    def web_chat_message(request: WebChatMessageRequest) -> JSONResponse:
        """Remote-TUI submit (path kept for existing proxies)."""
        text = request.content.strip()
        if not text:
            return JSONResponse({"error": "content is required"}, status_code=400)
        if submit_fn is None:
            return JSONResponse(
                {"error": "remote TUI submit is not available"},
                status_code=503,
            )
        channel = (request.channel or "cli").strip().lower() or "cli"
        try:
            payload = submit_fn(text, channel)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            message = str(exc)
            # Busy turns are conflict; missing runtime is unavailable.
            status = 409 if "processing" in message.lower() else 503
            return JSONResponse({"error": message}, status_code=status)
        except ValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(payload, status_code=202)

    return app


class ControlServer:
    """Run the control API in a daemon thread."""

    def __init__(
        self,
        host: str,
        port: int,
        shutdown_fn: Callable[[], None],
        new_session_fn: Callable[[], None] | None = None,
        reload_fn: Callable[[], None] | None = None,
        tui_submit_fn: TuiSubmitFn | None = None,
        channels_fn: ChannelsFn | None = None,
        web_chat_submit_fn: TuiSubmitFn | None = None,
    ):
        self._host = host
        self._port = port
        self._app = create_app(
            shutdown_fn,
            new_session_fn=new_session_fn,
            reload_fn=reload_fn,
            tui_submit_fn=tui_submit_fn or web_chat_submit_fn,
            channels_fn=channels_fn,
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        _assert_control_slot_available(self._host, self._port)
        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=server.run,
            daemon=True,
            name="control-api",
        )
        self._thread.start()
        logger.info("Control API started on %s:%d", self._host, self._port)
