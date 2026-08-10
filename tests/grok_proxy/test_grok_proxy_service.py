"""Tests for Grok proxy upstream forwarding and token manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import anyio
import pytest

from grok_proxy.auth import (
    DEFAULT_GROK_OAUTH_CLIENT_ID,
    GrokOAuthTokens,
    GrokTokenStore,
    StoredGrokToken,
)
from grok_proxy.service import GrokProxyService, GrokTokenManager, GrokUpstreamError
from grok_proxy.settings import GrokProxySettings


def _stored(
    *,
    access_token: str = "access-fresh",
    refresh_token: str = "refresh-1",
    expires_at: datetime | None = None,
) -> StoredGrokToken:
    now = datetime.now(tz=UTC)
    return StoredGrokToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at or (now + timedelta(hours=6)),
        client_id=DEFAULT_GROK_OAUTH_CLIENT_ID,
        token_endpoint="https://auth.x.ai/oauth2/token",
        source="oauth_device_code",
        created_at=now,
        updated_at=now,
    )


class _AsyncResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        if isinstance(payload, bytes):
            self._content = payload
        else:
            self._content = json.dumps(payload or {}).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8")

    async def aread(self) -> bytes:
        return self._content

    async def aclose(self) -> None:
        return None


class _AsyncClient:
    def __init__(self, responses: list[_AsyncResponse], calls: list[dict]):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method: str, url: str, headers: dict, json=None, params=None):
        self._calls.append({
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
            "params": params,
        })
        return self._responses.pop(0)

    async def aclose(self) -> None:
        return None


def test_token_manager_returns_fresh_token_without_refresh(tmp_path: Path):
    path = tmp_path / "token.json"
    GrokTokenStore(path).save(_stored(access_token="keep-me"))
    settings = GrokProxySettings(token_path=path, refresh_skew_seconds=3600)
    manager = GrokTokenManager(settings)

    async def _run() -> str:
        return await manager.acquire()

    token = anyio.run(_run)
    assert token == "keep-me"


def test_token_manager_refreshes_when_near_expiry(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.json"
    near = datetime.now(tz=UTC) + timedelta(minutes=10)
    GrokTokenStore(path).save(_stored(access_token="old", expires_at=near))
    settings = GrokProxySettings(token_path=path, refresh_skew_seconds=3600)
    manager = GrokTokenManager(settings)

    def _refresh(refresh_token: str, *, token_endpoint: str | None = None):
        assert refresh_token == "refresh-1"
        return GrokOAuthTokens(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_in=7200,
        )

    monkeypatch.setattr(manager._oauth, "refresh", _refresh)

    async def _run() -> str:
        return await manager.acquire()

    token = anyio.run(_run)
    assert token == "new-access"
    saved = GrokTokenStore(path).load()
    assert saved is not None
    assert saved.access_token == "new-access"
    assert saved.refresh_token == "new-refresh"
    assert saved.source == "oauth_refresh"


def test_forward_json_injects_authorization(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.json"
    GrokTokenStore(path).save(_stored(access_token="bearer-xyz"))
    settings = GrokProxySettings(
        token_path=path,
        xai_base_url="https://api.x.ai/v1",
    )
    service = GrokProxyService(settings)
    calls: list[dict] = []
    responses = [
        _AsyncResponse(payload={"id": "chatcmpl-1", "choices": []}),
    ]

    monkeypatch.setattr(
        "grok_proxy.service.httpx.AsyncClient",
        lambda *args, **kwargs: _AsyncClient(responses, calls),
    )

    async def _run():
        return await service.forward_json(
            "POST",
            "/v1/chat/completions",
            json_body={"model": "grok-4.3", "messages": []},
        )

    body, media_type, status = anyio.run(_run)
    assert status == 200
    assert media_type == "application/json"
    assert json.loads(body)["id"] == "chatcmpl-1"
    assert calls[0]["url"] == "https://api.x.ai/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer bearer-xyz"


def test_forward_json_forwards_x_grok_conv_id(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.json"
    GrokTokenStore(path).save(_stored(access_token="bearer-xyz"))
    settings = GrokProxySettings(token_path=path, xai_base_url="https://api.x.ai/v1")
    service = GrokProxyService(settings)
    calls: list[dict] = []
    responses = [_AsyncResponse(payload={"ok": True})]
    monkeypatch.setattr(
        "grok_proxy.service.httpx.AsyncClient",
        lambda *args, **kwargs: _AsyncClient(responses, calls),
    )

    async def _run():
        return await service.forward_json(
            "POST",
            "/v1/chat/completions",
            json_body={"model": "grok-4.3", "messages": []},
            extra_headers={"x-grok-conv-id": "sess:brain:bucket"},
        )

    anyio.run(_run)
    assert calls[0]["headers"]["x-grok-conv-id"] == "sess:brain:bucket"
    assert calls[0]["headers"]["Authorization"] == "Bearer bearer-xyz"


def test_forward_json_retries_once_on_401(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.json"
    GrokTokenStore(path).save(_stored(access_token="stale"))
    settings = GrokProxySettings(token_path=path)
    service = GrokProxyService(settings)
    calls: list[dict] = []
    responses = [
        _AsyncResponse(status_code=401, payload={"error": "unauthorized"}),
        _AsyncResponse(payload={"ok": True}),
    ]

    async def _acquire(*, force_refresh: bool = False) -> str:
        return "refreshed" if force_refresh else "stale"

    monkeypatch.setattr(service._tokens, "acquire", _acquire)
    monkeypatch.setattr(
        "grok_proxy.service.httpx.AsyncClient",
        lambda *args, **kwargs: _AsyncClient(responses, calls),
    )

    async def _run():
        return await service.forward_json(
            "POST",
            "/v1/responses",
            json_body={"model": "grok-4.3", "input": "hi"},
        )

    body, _media_type, status = anyio.run(_run)
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert len(calls) == 2
    assert calls[0]["headers"]["Authorization"] == "Bearer stale"
    assert calls[1]["headers"]["Authorization"] == "Bearer refreshed"


def test_forward_json_surfaces_upstream_403(monkeypatch, tmp_path: Path):
    path = tmp_path / "token.json"
    GrokTokenStore(path).save(_stored())
    settings = GrokProxySettings(token_path=path)
    service = GrokProxyService(settings)
    calls: list[dict] = []
    responses = [
        _AsyncResponse(
            status_code=403,
            payload={"error": "tier denied"},
        ),
    ]
    monkeypatch.setattr(
        "grok_proxy.service.httpx.AsyncClient",
        lambda *args, **kwargs: _AsyncClient(responses, calls),
    )

    async def _run():
        return await service.forward_json(
            "POST",
            "/v1/chat/completions",
            json_body={"model": "grok-4.3", "messages": []},
        )

    with pytest.raises(GrokUpstreamError) as exc:
        anyio.run(_run)
    assert exc.value.status_code == 403
    assert b"tier denied" in exc.value.body


def test_env_access_token_bypass_no_store(monkeypatch, tmp_path: Path):
    settings = GrokProxySettings(
        token_path=tmp_path / "unused.json",
        access_token="env-token",
    )
    service = GrokProxyService(settings)
    calls: list[dict] = []
    responses = [_AsyncResponse(payload={"ok": True})]
    monkeypatch.setattr(
        "grok_proxy.service.httpx.AsyncClient",
        lambda *args, **kwargs: _AsyncClient(responses, calls),
    )

    async def _run():
        return await service.forward_json("GET", "/v1/models")

    _body, _media, status = anyio.run(_run)
    assert status == 200
    assert calls[0]["headers"]["Authorization"] == "Bearer env-token"
