"""Tests for inbound auth on the Claude Code proxy app.

Loopback clients are always exempt; non-loopback clients must present the
configured inbound API key and are rejected outright when no key is set.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from claude_code_proxy.app import _stream_with_keepalive, create_app
from claude_code_proxy.settings import ClaudeCodeProxySettings

_BODY = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}

_LOOPBACK = ("127.0.0.1", 55001)
_REMOTE = ("192.168.1.50", 55002)


class _AsyncResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.headers = {
            "content-type": "application/json",
            "anthropic-ratelimit-unified-5h-utilization": "0.42",
        }
        self.content = json.dumps(payload).encode("utf-8")


class _AsyncClient:
    """Mock upstream httpx.AsyncClient that always succeeds."""

    def __init__(self, calls: list[dict]):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, headers: dict, json: dict):
        self._calls.append({"url": url, "headers": headers, "json": json})
        return _AsyncResponse({"content": [{"type": "text", "text": "ok"}]})


def _client(
    monkeypatch,
    *,
    api_key: str | None,
    peer: tuple[str, int] | None,
) -> tuple[TestClient, list[dict]]:
    """Return a TestClient with the given peer address plus upstream call log."""

    calls: list[dict] = []
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(calls),
    )
    settings = ClaudeCodeProxySettings(access_token="tok-upstream", api_key=api_key)
    app = create_app(settings)
    if peer is None:
        # Keep TestClient's default peer ("testclient"), which is not an IP.
        return TestClient(app), calls
    return TestClient(app, client=peer), calls


def test_loopback_client_needs_no_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "ok"
    assert len(calls) == 1


def test_loopback_client_skips_key_check_even_when_key_configured(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_LOOPBACK)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert len(calls) == 1


def test_ipv4_mapped_loopback_counts_as_local(monkeypatch):
    client, calls = _client(
        monkeypatch, api_key="secret", peer=("::ffff:127.0.0.1", 55003)
    )

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_rejected_when_no_key_configured(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=_REMOTE)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert "CLAUDE_CODE_PROXY_API_KEY" in response.json()["error"]
    assert calls == []


def test_remote_client_with_valid_x_api_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"x-api-key": "secret"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_with_valid_bearer_key(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_remote_client_with_wrong_key_rejected(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post(
        "/v1/messages", json=_BODY, headers={"x-api-key": "wrong"}
    )

    assert response.status_code == 401
    assert calls == []


def test_remote_client_without_key_header_rejected(monkeypatch):
    client, calls = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert calls == []


def test_unparseable_peer_treated_as_remote(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=None)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 401
    assert calls == []


class _FakeUpstream:
    """Upstream whose first byte arrives after a configurable delay."""

    def __init__(self, delay: float, chunks: list[bytes]):
        self._delay = delay
        self._chunks = chunks

    async def aiter_raw(self):
        await asyncio.sleep(self._delay)
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_stream_keepalive_pings_until_first_byte():
    upstream = _FakeUpstream(0.12, [b"event: message_start\n\n", b"data: {}\n\n"])

    chunks = [c async for c in _stream_with_keepalive(upstream, interval=0.05)]

    assert chunks[0] == b": keepalive\n\n"
    # Real bytes follow the pings, in order and unmodified.
    assert chunks[-2:] == [b"event: message_start\n\n", b"data: {}\n\n"]


@pytest.mark.asyncio
async def test_stream_keepalive_is_silent_for_fast_upstreams():
    upstream = _FakeUpstream(0.0, [b"event: message_start\n\n"])

    chunks = [c async for c in _stream_with_keepalive(upstream, interval=0.5)]

    assert chunks == [b"event: message_start\n\n"]


@pytest.mark.asyncio
async def test_stream_keepalive_handles_empty_upstream():
    upstream = _FakeUpstream(0.0, [])

    chunks = [c async for c in _stream_with_keepalive(upstream, interval=0.5)]

    assert chunks == []


def test_client_beta_header_is_merged_into_upstream_request(monkeypatch):
    client, calls = _client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.post(
        "/v1/messages",
        json={**_BODY, "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]}},
        headers={"anthropic-beta": "context-management-2025-06-27, claude-code-20250219"},
    )

    assert response.status_code == 200
    betas = calls[0]["headers"]["anthropic-beta"].split(",")
    assert "context-management-2025-06-27" in betas
    assert betas.count("claude-code-20250219") == 1
    # The extra body field must reach upstream untouched.
    assert calls[0]["json"]["context_management"] == {
        "edits": [{"type": "clear_tool_uses_20250919"}]
    }


def test_server_tools_without_input_schema_pass_through(monkeypatch):
    """Server tools (advisor, web_search, ...) lack description/input_schema.

    Regression: strict tool validation returned 422 before the request ever
    reached upstream. Tools must be forwarded verbatim, extra fields included.
    """
    client, calls = _client(monkeypatch, api_key=None, peer=_LOOPBACK)

    tools = [
        {
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
                "$schema": "http://json-schema.org/draft-07/schema#",
            },
        },
        {"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-4-6"},
    ]

    response = client.post("/v1/messages", json={**_BODY, "tools": tools})

    assert response.status_code == 200
    assert calls[0]["json"]["tools"] == tools


def test_ratelimit_headers_are_passed_back_to_client(monkeypatch):
    client, _ = _client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.post("/v1/messages", json=_BODY)

    assert response.status_code == 200
    assert response.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.42"


def test_health_stays_open_for_remote_clients(monkeypatch):
    client, _ = _client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


_PROFILE_PAYLOAD = {
    "account": {"email": "user@example.com", "display_name": "User"},
    "organization": {
        "organization_type": "claude_max",
        "rate_limit_tier": "default_claude_max_5x",
    },
}
_USAGE_PAYLOAD = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-07-12T20:29:59+00:00"},
    "seven_day": {"utilization": 1.0, "resets_at": "2026-07-18T23:59:59+00:00"},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 3,
            "severity": "normal",
            "resets_at": "2026-07-12T20:29:59+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 1,
            "severity": "normal",
            "resets_at": "2026-07-18T23:59:59+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 14,
            "severity": "normal",
            "resets_at": "2026-07-18T23:59:59+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
}
_MODELS_PAYLOAD = {
    "data": [
        {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"},
        {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
    ]
}


class _UsageResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self) -> dict:
        return self._payload


class _UsageAsyncClient:
    """Mock upstream httpx client answering the OAuth account endpoints."""

    def __init__(self, calls: list[str]):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, headers: dict | None = None):
        self._calls.append(url)
        if "oauth/profile" in url:
            return _UsageResponse(_PROFILE_PAYLOAD)
        if "oauth/usage" in url:
            return _UsageResponse(_USAGE_PAYLOAD)
        if "/v1/models" in url:
            return _UsageResponse(_MODELS_PAYLOAD)
        raise AssertionError(f"unexpected GET {url}")


def _usage_client(
    monkeypatch,
    *,
    api_key: str | None,
    peer: tuple[str, int] | None,
) -> tuple[TestClient, list[str]]:
    calls: list[str] = []
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _UsageAsyncClient(calls),
    )
    settings = ClaudeCodeProxySettings(access_token="tok-upstream", api_key=api_key)
    app = create_app(settings)
    if peer is None:
        return TestClient(app), calls
    return TestClient(app, client=peer), calls


def test_usage_reports_account_usage_and_models(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.get("/usage")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["accounts"]) == 1
    account = payload["accounts"][0]
    assert account["status"] == "active"
    assert account["account"]["email"] == "user@example.com"
    assert account["account"]["rate_limit_tier"] == "default_claude_max_5x"
    assert account["usage"]["five_hour"]["utilization"] == 3.0
    assert account["usage"]["seven_day"]["utilization"] == 1.0
    assert account["usage"]["seven_day_scoped"] == [
        {"label": "Fable", "utilization": 14, "resets_at": "2026-07-18T23:59:59+00:00"}
    ]
    assert [m["id"] for m in payload["models"]] == ["claude-opus-4-8", "claude-sonnet-5"]


def test_usage_snapshot_is_cached_between_requests(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key=None, peer=_LOOPBACK)

    first = client.get("/usage").json()
    upstream_calls = len(calls)
    second = client.get("/usage").json()

    assert first == second
    assert len(calls) == upstream_calls  # served from cache, no new upstream calls


def test_usage_refresh_param_bypasses_cache(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key=None, peer=_LOOPBACK)

    client.get("/usage")
    upstream_calls = len(calls)
    response = client.get("/usage?refresh=true")

    assert response.status_code == 200
    assert len(calls) > upstream_calls  # cache skipped, upstream swept again


def test_usage_requires_key_for_remote_clients(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.get("/usage")

    assert response.status_code == 401
    assert calls == []


def test_usage_allows_remote_clients_with_key(monkeypatch):
    client, _ = _usage_client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.get("/usage", headers={"x-api-key": "secret"})

    assert response.status_code == 200
    assert response.json()["accounts"][0]["status"] == "active"


def test_models_passthrough_returns_upstream_payload(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key=None, peer=_LOOPBACK)

    response = client.get("/v1/models?limit=5")

    assert response.status_code == 200
    assert response.json() == _MODELS_PAYLOAD
    assert any(url.endswith("/v1/models?limit=5") for url in calls)


def test_models_requires_key_for_remote_clients(monkeypatch):
    client, calls = _usage_client(monkeypatch, api_key="secret", peer=_REMOTE)

    response = client.get("/v1/models")

    assert response.status_code == 401
    assert calls == []


def test_usage_reports_store_tokens_in_priority_order(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    from claude_code_proxy.auth import StoredClaudeCodeToken, StoredClaudeCodeTokenStore

    path = tmp_path / "tokens.json"
    monkeypatch.setattr("claude_code_proxy.auth.default_token_path", lambda: path)

    def _token(token_id: str, created: datetime) -> StoredClaudeCodeToken:
        return StoredClaudeCodeToken(
            id=token_id,
            access_token=f"tok-{token_id}",
            refresh_token=None,
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            source="oauth_browser",
            client_id="client-id",
            created_at=created,
        )

    StoredClaudeCodeTokenStore(path).replace_all(
        [
            _token("newer", datetime(2026, 6, 1, tzinfo=UTC)),
            _token("older", datetime(2026, 1, 1, tzinfo=UTC)),
        ]
    )

    calls: list[str] = []
    monkeypatch.setattr(
        "claude_code_proxy.service.httpx.AsyncClient",
        lambda timeout: _UsageAsyncClient(calls),
    )
    app = create_app(ClaudeCodeProxySettings())
    client = TestClient(app, client=_LOOPBACK)

    payload = client.get("/usage").json()

    assert [(a["id"], a["status"]) for a in payload["accounts"]] == [
        ("newer", "active"),
        ("older", "standby"),
    ]
    assert all(a["account"]["email"] == "user@example.com" for a in payload["accounts"])
