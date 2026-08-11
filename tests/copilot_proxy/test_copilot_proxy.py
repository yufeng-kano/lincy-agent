"""Tests for the native Copilot proxy transport."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from copilot_proxy.service import CopilotProxyService
from copilot_proxy.settings import CopilotProxySettings
from lincy.llm.schema import CopilotNativeRequest, Message, ToolDefinition, ToolParameter


class _AsyncResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    @property
    def text(self) -> str:
        import json

        return json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _AsyncClient:
    def __init__(self, effects: list[dict], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, headers: dict):
        self._calls.append({"method": "GET", "url": url, "headers": headers})
        effect = self._effects.pop(0)
        return _AsyncResponse(effect)

    async def post(self, url: str, headers: dict, json: dict):
        self._calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        effect = self._effects.pop(0)
        return _AsyncResponse(effect)


def _patch_async_httpx(monkeypatch, effects: list[dict], calls: list[dict]) -> None:
    monkeypatch.setattr(
        "copilot_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(effects, calls),
    )


@pytest.mark.asyncio
async def test_proxy_service_exchanges_token_and_calls_upstream(monkeypatch):
    expires_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    effects = [
        {"token": "copilot-token", "expires_at": expires_at},
        {"choices": [{"message": {"content": "hello from upstream"}}]},
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CopilotProxyService(
        CopilotProxySettings(github_token="gh-token"),
    )

    request = CopilotNativeRequest(
        model="gpt-5",
        messages=[Message(role="user", content="hi")],
        initiator="user",
        interaction_id="turn-1",
        interaction_type="conversation-agent",
        request_id="req-1",
    )

    response = await service.chat(request)

    assert response.content == "hello from upstream"
    assert calls[0]["url"].endswith("/copilot_internal/v2/token")
    assert calls[1]["url"].endswith("/chat/completions")
    assert calls[1]["headers"]["x-initiator"] == "user"
    assert calls[1]["headers"]["x-interaction-id"] == "turn-1"


@pytest.mark.asyncio
async def test_proxy_service_translates_tools_and_response_schema(monkeypatch):
    expires_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    effects = [
        {"token": "copilot-token", "expires_at": expires_at},
        {"choices": [{"message": {"content": "ok"}}]},
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CopilotProxyService(
        CopilotProxySettings(github_token="gh-token"),
    )
    request = CopilotNativeRequest(
        model="gpt-5",
        messages=[Message(role="user", content="hi")],
        tools=[
            ToolDefinition(
                name="read_file",
                description="read file",
                parameters={"path": ToolParameter(type="string", description="path")},
                required=["path"],
            )
        ],
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        initiator="agent",
        interaction_id="turn-2",
        interaction_type="conversation-subagent",
        request_id="req-2",
    )

    await service.chat(request)

    payload = calls[1]["json"]
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert payload["response_format"]["json_schema"]["schema"]["type"] == "object"
