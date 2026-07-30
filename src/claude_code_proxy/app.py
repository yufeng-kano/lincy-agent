"""FastAPI app for the native Claude Code proxy."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import secrets
from typing import AsyncIterator

from starlette.background import BackgroundTask
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from lincy.llm.schema import ClaudeCodeRequest

from .auth import normalize_bearer_token
from .service import (
    ClaudeCodeProxyService,
    ClaudeCodeTokenUnavailableError,
    ClaudeCodeUpstreamError,
    ClaudeCodeUpstreamTimeoutError,
)
from .settings import ClaudeCodeProxySettings


async def _close_stream(client, response) -> None:
    await response.aclose()
    await client.aclose()


def _token_unavailable_response(exc: ClaudeCodeTokenUnavailableError) -> JSONResponse:
    """503 for an empty token pool, with Retry-After when the pool self-heals."""

    headers = None
    if exc.retry_after_seconds is not None:
        headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after_seconds)))}
    return JSONResponse({"error": str(exc)}, status_code=503, headers=headers)


KEEPALIVE_INTERVAL_SECONDS = 30.0


async def _stream_with_keepalive(
    upstream,
    interval: float = KEEPALIVE_INTERVAL_SECONDS,
) -> AsyncIterator[bytes]:
    """Relay upstream bytes, emitting SSE comments until the first byte arrives.

    Anthropic keeps the stream silent while it processes very large prompts;
    idle-sensitive middleboxes (Cloudflare tunnel cuts origins after ~100-120s
    without data) kill the connection in that window. SSE comment lines are
    ignored by event parsers and are only injected before the first real byte,
    so events are never split.
    """

    iterator = upstream.aiter_raw().__aiter__()
    first = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(asyncio.shield(first), timeout=interval)
                break
            except TimeoutError:
                yield b": keepalive\n\n"
            except StopAsyncIteration:
                return
    finally:
        if not first.done():
            first.cancel()
    yield chunk
    async for chunk in iterator:
        yield chunk


def _is_loopback_client(request: Request) -> bool:
    """True when the TCP peer is a loopback address (127.0.0.0/8 or ::1).

    Uses the socket peer address only; forwarding headers are untrusted. Peers
    that cannot be parsed as an IP are treated as remote (fail closed).
    """

    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    # A dual-stack bind reports local IPv4 peers as ::ffff:127.0.0.1.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def _reject_unauthorized(request: Request, api_key: str | None) -> JSONResponse | None:
    """Gate non-loopback clients behind the inbound API key.

    Loopback requests always pass. Without a configured key, remote requests
    are always rejected so binding a public host can never silently expose
    upstream quota.
    """

    if _is_loopback_client(request):
        return None
    if not api_key:
        return JSONResponse(
            {
                "error": (
                    "This proxy only accepts localhost requests. Set "
                    "CLAUDE_CODE_PROXY_API_KEY (or serve --api-key) to allow "
                    "remote clients."
                )
            },
            status_code=401,
        )
    provided = request.headers.get("x-api-key") or normalize_bearer_token(
        request.headers.get("authorization", "")
    )
    if provided and secrets.compare_digest(provided, api_key):
        return None
    return JSONResponse(
        {
            "error": (
                "Invalid or missing API key. Provide it via x-api-key or "
                "Authorization: Bearer."
            )
        },
        status_code=401,
    )


class LoginCompleteRequest(BaseModel):
    """`code#state` pasted back from the Anthropic callback page."""

    code: str = Field(min_length=1)


def create_app(settings: ClaudeCodeProxySettings) -> FastAPI:
    app = FastAPI(title="chat-agent-claude-code-proxy", docs_url=None, redoc_url=None)
    service = ClaudeCodeProxyService(settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/usage")
    async def usage(raw_request: Request, refresh: bool = False):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        return await service.usage_snapshot(force_refresh=refresh)

    # Token-store management for the web dashboard. Same inbound gate as the
    # data plane: loopback is trusted, remote needs the inbound API key.

    @app.post("/tokens/{token_id}/promote")
    async def promote_token(token_id: str, raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        if not service.promote_token(token_id):
            return JSONResponse({"error": f"no token with id {token_id}"}, status_code=404)
        return {"ok": True}

    @app.delete("/tokens/{token_id}")
    async def remove_token(token_id: str, raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        if not service.remove_token(token_id):
            return JSONResponse({"error": f"no token with id {token_id}"}, status_code=404)
        return {"ok": True}

    @app.post("/login")
    async def begin_login(raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        return service.begin_login()

    @app.post("/login/{login_id}/complete")
    async def complete_login(login_id: str, request: LoginCompleteRequest, raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        try:
            token = await service.complete_login(login_id, request.code)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        if token is None:
            return JSONResponse(
                {"error": "unknown or expired login flow; start again"}, status_code=404
            )
        return {"ok": True, "token_id": token.id}

    @app.get("/v1/models")
    async def models(raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        try:
            body, media_type = await service.forward_models(raw_request.url.query)
        except ClaudeCodeUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except ClaudeCodeTokenUnavailableError as exc:
            return _token_unavailable_response(exc)
        return Response(content=body, media_type=media_type)

    @app.post("/v1/messages")
    async def messages(request: ClaudeCodeRequest, raw_request: Request):
        rejection = _reject_unauthorized(raw_request, settings.api_key)
        if rejection is not None:
            return rejection
        client_betas = raw_request.headers.get("anthropic-beta")
        try:
            if request.stream:
                client, upstream = await service.open_stream(request, client_betas)
                return StreamingResponse(
                    _stream_with_keepalive(upstream),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/event-stream"),
                    headers=ClaudeCodeProxyService.passthrough_headers(upstream.headers),
                    background=BackgroundTask(_close_stream, client, upstream),
                )

            body, media_type, passthrough = await service.forward_json(request, client_betas)
        except ClaudeCodeUpstreamError as exc:
            return Response(
                content=exc.body,
                status_code=exc.status_code,
                media_type=exc.media_type,
            )
        except ClaudeCodeTokenUnavailableError as exc:
            return _token_unavailable_response(exc)
        except ClaudeCodeUpstreamTimeoutError as exc:
            return JSONResponse({"error": str(exc)}, status_code=504)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return Response(content=body, media_type=media_type, headers=passthrough)

    return app
