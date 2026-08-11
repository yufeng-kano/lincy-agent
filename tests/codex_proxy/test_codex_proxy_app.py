"""Tests for the native Codex proxy app: inbound gate, usage snapshot, and the
browser / manual login flows (including the background OAuth callback listener).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket

import httpx
import pytest
from starlette.testclient import TestClient

from codex_proxy.app import create_app
from codex_proxy.auth import (
    CODEX_AUTH_FALLBACK_TOKEN_ID,
    CodexBrowserAuthorization,
    StoredCodexToken,
    StoredCodexTokenStore,
)
from codex_proxy.service import CodexProxyService
from codex_proxy.settings import CodexProxySettings

_LOOPBACK = ("127.0.0.1", 55001)
_REMOTE = ("192.168.1.50", 55002)


def _make_fake_jwt(*, account_id: str = "acct_123", exp: int = 2_200_000_000) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }

    def _encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_encode(header)}.{_encode(payload)}.signature"


def _isolated_settings(tmp_path: Path, **overrides) -> CodexProxySettings:
    """Settings pinned away from any real ~/.codex/auth.json on this machine."""

    base = dict(
        codex_auth_path=tmp_path / "no-official-auth.json",
        token_path=tmp_path / "tokens.json",
    )
    base.update(overrides)
    return CodexProxySettings(**base)


def _token_for_usage(
    account_id: str, *, token_id: str = "tok", created_at: datetime | None = None
) -> StoredCodexToken:
    return StoredCodexToken(
        id=token_id,
        access_token=_make_fake_jwt(account_id=account_id),
        refresh_token=None,
        account_id=account_id,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        source="oauth_browser",
        client_id="client-id",
        created_at=created_at or datetime.now(tz=UTC),
    )


# --- Inbound gate: /chat and /compact stay ungated; the management surface is gated ---


def test_chat_stays_ungated_for_remote_clients_without_key(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key="secret")
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.post(
        "/chat",
        json={"model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
    )

    # Not a 401 from the inbound gate -- the empty token pool surfaces its own
    # 503 instead, proving the request reached the service layer ungated.
    assert response.status_code == 503


def test_health_stays_open_for_remote_clients(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key="secret")
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_usage_loopback_client_needs_no_key(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key=None)
    client = TestClient(create_app(settings), client=_LOOPBACK)

    response = client.get("/usage")

    assert response.status_code == 200
    assert response.json() == {"accounts": [], "models": []}


def test_usage_remote_client_rejected_when_no_key_configured(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key=None)
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.get("/usage")

    assert response.status_code == 401
    assert "CODEX_PROXY_API_KEY" in response.json()["error"]


def test_usage_remote_client_with_valid_x_api_key(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key="secret")
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.get("/usage", headers={"x-api-key": "secret"})

    assert response.status_code == 200


def test_usage_remote_client_with_valid_bearer_key(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key="secret")
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.get("/usage", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200


def test_usage_remote_client_with_wrong_key_rejected(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key="secret")
    client = TestClient(create_app(settings), client=_REMOTE)

    response = client.get("/usage", headers={"x-api-key": "wrong"})

    assert response.status_code == 401


# --- Usage snapshot: account identity, window labels, caching ---


def _usage_get_stub(calls: list[str], payload: dict, status_code: int = 200):
    """Stand-in for service._sync_usage_get (the stdlib urllib usage fetch)."""

    def _stub(url: str, headers: dict, timeout: float) -> tuple[int, str]:
        calls.append(url)
        return status_code, json.dumps(payload)

    return _stub


# Shape matches the fact-#1 payload from GET /backend-api/codex/usage
# (docs/dev/provider-api-spec.md), with both primary (Week) and secondary (5h)
# windows populated.
_USAGE_PAYLOAD_BOTH_WINDOWS = {
    "email": "user@example.com",
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 2,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 601427,
            "reset_at": 1784953872,
        },
        "secondary_window": {
            "used_percent": 45.5,
            "limit_window_seconds": 18000,
            "reset_after_seconds": 3600,
            "reset_at": 1784900000,
        },
    },
}


def test_usage_reports_account_and_windows_with_labels(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1"))
    calls: list[str] = []
    monkeypatch.setattr(
        "codex_proxy.service._sync_usage_get",
        _usage_get_stub(calls, _USAGE_PAYLOAD_BOTH_WINDOWS),
    )
    settings = _isolated_settings(tmp_path, token_path=store_path, api_key=None)
    client = TestClient(create_app(settings), client=_LOOPBACK)

    response = client.get("/usage")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["accounts"]) == 1
    account = payload["accounts"][0]
    assert account["status"] == "active"
    assert account["account"] == {"email": "user@example.com", "plan_type": "plus"}
    windows = account["usage"]["windows"]
    assert windows[0]["label"] == "Week"
    assert windows[0]["utilization"] == 2.0
    assert windows[1]["label"] == "5h"
    assert windows[1]["utilization"] == 45.5
    assert datetime.fromisoformat(windows[0]["resets_at"]).timestamp() == 1784953872
    assert datetime.fromisoformat(windows[1]["resets_at"]).timestamp() == 1784900000
    assert calls[0].endswith("/codex/usage")
    assert payload["models"] == []


def test_usage_snapshot_is_cached_then_refresh_bypasses(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1"))
    calls: list[str] = []
    monkeypatch.setattr(
        "codex_proxy.service._sync_usage_get",
        _usage_get_stub(calls, _USAGE_PAYLOAD_BOTH_WINDOWS),
    )
    settings = _isolated_settings(tmp_path, token_path=store_path, api_key=None)
    client = TestClient(create_app(settings), client=_LOOPBACK)

    first = client.get("/usage").json()
    calls_after_first = len(calls)
    second = client.get("/usage").json()

    assert second == first
    assert len(calls) == calls_after_first  # served from cache

    third = client.get("/usage?refresh=true")

    assert third.status_code == 200
    assert len(calls) > calls_after_first  # cache bypassed


@pytest.mark.asyncio
async def test_usage_serves_stale_data_when_fetch_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("codex_proxy.service.USAGE_CACHE_TTL_SECONDS", 0.0)
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1"))
    state = {"fail": False}

    def _flaky_usage_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
        if state["fail"]:
            return 429, json.dumps({"error": "rate_limited"})
        return 200, json.dumps(_USAGE_PAYLOAD_BOTH_WINDOWS)

    monkeypatch.setattr("codex_proxy.service._sync_usage_get", _flaky_usage_get)
    settings = _isolated_settings(tmp_path, token_path=store_path)
    service = CodexProxyService(settings)

    first = await service.usage_snapshot()
    assert first["accounts"][0]["stale"] is False

    state["fail"] = True
    second = await service.usage_snapshot()
    account = second["accounts"][0]

    assert account["stale"] is True
    assert account["usage"]["windows"][0]["label"] == "Week"
    assert "429" in account["error"]


@pytest.mark.asyncio
async def test_usage_auth_failure_is_unusable_not_error(monkeypatch, tmp_path: Path):
    """Invalidated store tokens stay listed so they can be removed, but 401
    is 'no usable account' — not a red usage-fetch failure."""

    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1"))
    body = {
        "error": {
            "message": "Your authentication token has been invalidated. Please try signing in again.",
            "type": "invalid_request_error",
            "code": "token_invalidated",
            "param": None,
        }
    }
    monkeypatch.setattr(
        "codex_proxy.service._sync_usage_get",
        _usage_get_stub([], body, status_code=401),
    )
    settings = _isolated_settings(tmp_path, token_path=store_path)
    service = CodexProxyService(settings)

    payload = await service.usage_snapshot()
    account = payload["accounts"][0]

    assert account["usage"] is None
    assert account["status"] == "unusable"
    assert account["error"] is None


@pytest.mark.asyncio
async def test_usage_auth_failure_omits_official_cli_fallback(monkeypatch, tmp_path: Path):
    """Dead ~/.codex/auth.json must not surface as a dashboard error row."""

    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id="acct_cli"),
                    "refresh_token": "refresh-token",
                    "account_id": "acct_cli",
                },
            }
        )
    )
    body = {
        "error": {
            "message": "Your authentication token has been invalidated. Please try signing in again.",
            "type": "invalid_request_error",
            "code": "token_invalidated",
        }
    }
    monkeypatch.setattr(
        "codex_proxy.service._sync_usage_get",
        _usage_get_stub([], body, status_code=401),
    )
    settings = _isolated_settings(
        tmp_path, codex_auth_path=auth_path, token_path=tmp_path / "tokens.json"
    )
    service = CodexProxyService(settings)

    payload = await service.usage_snapshot()

    assert payload["accounts"] == []


@pytest.mark.asyncio
async def test_usage_error_extracts_nested_openai_message_for_transient_failure(
    monkeypatch, tmp_path: Path
):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1"))
    body = {
        "error": {
            "message": "Rate limit reached for requests",
            "type": "requests",
            "code": "rate_limit_exceeded",
            "param": None,
        }
    }
    monkeypatch.setattr(
        "codex_proxy.service._sync_usage_get",
        _usage_get_stub([], body, status_code=429),
    )
    settings = _isolated_settings(tmp_path, token_path=store_path)
    service = CodexProxyService(settings)

    payload = await service.usage_snapshot()
    account = payload["accounts"][0]

    assert account["usage"] is None
    assert account["error"] == (
        "usage fetch failed: HTTP 429: Rate limit reached for requests"
    )
    assert "{" not in account["error"]


@pytest.mark.asyncio
async def test_token_store_edits_invalidate_usage_snapshot_cache(tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(_token_for_usage("acct_1", token_id="tok"))
    settings = _isolated_settings(tmp_path, token_path=store_path)
    service = CodexProxyService(settings)
    service._usage_cache = (10**12, {"accounts": [], "models": []})

    assert service.promote_token("tok") is True
    assert service._usage_cache is None

    service._usage_cache = (10**12, {"accounts": [], "models": []})
    assert service.remove_token("tok") is True
    assert service._usage_cache is None

    service._usage_cache = (10**12, {"accounts": [], "models": []})
    assert service.promote_token("missing") is False
    assert service._usage_cache is not None


# --- Login flow: begin, manual complete, browser callback listener ---


@pytest.mark.asyncio
async def test_manual_login_complete_with_pasted_callback_url(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    settings = _isolated_settings(tmp_path, token_path=store_path, callback_bind_port=0, api_key=None)
    fixed_auth = CodexBrowserAuthorization(
        authorization_url="https://auth.openai.com/oauth/authorize?state=state-1",
        code_verifier="verifier",
        state="state-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.begin_authorization",
        lambda self: fixed_auth,
    )
    exchanged = _token_for_usage("acct_manual", token_id="added")

    def fake_exchange(self, code, *, returned_state, authorization):
        assert code == "auth-code"
        assert returned_state == "state-1"
        assert authorization is fixed_auth
        return exchanged

    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.exchange_callback_code",
        fake_exchange,
    )

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        begin = await client.post("/login")
        assert begin.status_code == 200
        payload = begin.json()
        assert payload["authorization_url"] == fixed_auth.authorization_url
        login_id = payload["login_id"]

        pending = await client.get(f"/login/{login_id}")
        assert pending.json() == {"status": "pending", "token_id": None}

        done = await client.post(
            f"/login/{login_id}/complete",
            json={"value": "http://localhost:1455/auth/callback?code=auth-code&state=state-1"},
        )
        assert done.status_code == 200
        assert done.json() == {"ok": True, "token_id": "added"}

        completed = await client.get(f"/login/{login_id}")
        assert completed.json() == {"status": "completed", "token_id": "added"}

        # The flow is consumed once completed; replaying it fails.
        replay = await client.post(
            f"/login/{login_id}/complete", json={"value": "auth-code#state-1"}
        )
        assert replay.status_code == 404

    assert [t.id for t in StoredCodexTokenStore(store_path).load_all()] == ["added"]


@pytest.mark.asyncio
async def test_manual_login_accepts_code_hash_state_form(monkeypatch, tmp_path: Path):
    settings = _isolated_settings(tmp_path, callback_bind_port=0, api_key=None)
    fixed_auth = CodexBrowserAuthorization(
        authorization_url="https://auth.openai.com/oauth/authorize?state=state-1",
        code_verifier="verifier",
        state="state-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.begin_authorization",
        lambda self: fixed_auth,
    )
    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.exchange_callback_code",
        lambda self, code, *, returned_state, authorization: _token_for_usage(
            "acct_manual", token_id="added"
        ),
    )

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        begin = await client.post("/login")
        login_id = begin.json()["login_id"]

        done = await client.post(f"/login/{login_id}/complete", json={"value": "auth-code#state-1"})
        assert done.status_code == 200
        assert done.json()["token_id"] == "added"


@pytest.mark.asyncio
async def test_manual_login_state_mismatch_maps_to_400(monkeypatch, tmp_path: Path):
    settings = _isolated_settings(tmp_path, callback_bind_port=0, api_key=None)
    fixed_auth = CodexBrowserAuthorization(
        authorization_url="https://auth.openai.com/oauth/authorize?state=state-1",
        code_verifier="verifier",
        state="state-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.begin_authorization",
        lambda self: fixed_auth,
    )

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        begin = await client.post("/login")
        login_id = begin.json()["login_id"]

        done = await client.post(f"/login/{login_id}/complete", json={"value": "code#other-state"})
        assert done.status_code == 400
        assert "state mismatch" in done.json()["error"].lower()


@pytest.mark.asyncio
async def test_login_status_unknown_id_is_expired(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key=None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/login/does-not-exist")

    assert response.status_code == 200
    assert response.json() == {"status": "expired", "token_id": None}


@pytest.mark.asyncio
async def test_manual_complete_unknown_login_id_returns_404(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key=None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/login/does-not-exist/complete", json={"value": "code#state"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_browser_callback_completes_login_via_background_listener(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    settings = _isolated_settings(tmp_path, token_path=store_path, callback_bind_port=0, api_key=None)
    fixed_auth = CodexBrowserAuthorization(
        authorization_url="https://auth.openai.com/oauth/authorize?state=state-1",
        code_verifier="verifier",
        state="state-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.begin_authorization",
        lambda self: fixed_auth,
    )
    exchanged = _token_for_usage("acct_cb", token_id="added")

    def fake_exchange(self, code, *, returned_state, authorization):
        assert code == "auth-code"
        assert returned_state == "state-1"
        return exchanged

    monkeypatch.setattr(
        "codex_proxy.service.CodexOAuthClient.exchange_callback_code",
        fake_exchange,
    )

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        begin = await client.post("/login")
        assert begin.status_code == 200
        payload = begin.json()
        assert "listener_error" not in payload
        login_id = payload["login_id"]

        port = app.state.service.callback_listener_port
        assert port

        # Simulate the browser's redirect landing on the local listener.
        async with httpx.AsyncClient() as raw_client:
            callback_response = await raw_client.get(
                f"http://127.0.0.1:{port}/auth/callback",
                params={"code": "auth-code", "state": "state-1"},
            )
        assert callback_response.status_code == 200
        assert "Codex login complete" in callback_response.text

        status = await client.get(f"/login/{login_id}")
        assert status.json() == {"status": "completed", "token_id": "added"}

    assert [t.id for t in StoredCodexTokenStore(store_path).load_all()] == ["added"]


@pytest.mark.asyncio
async def test_begin_login_reports_bind_failure_but_manual_complete_still_works(
    monkeypatch, tmp_path: Path
):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    blocked_port = blocker.getsockname()[1]
    try:
        settings = _isolated_settings(tmp_path, callback_bind_port=blocked_port, api_key=None)
        fixed_auth = CodexBrowserAuthorization(
            authorization_url="https://auth.openai.com/oauth/authorize?state=state-1",
            code_verifier="verifier",
            state="state-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
        monkeypatch.setattr(
            "codex_proxy.service.CodexOAuthClient.begin_authorization",
            lambda self: fixed_auth,
        )
        monkeypatch.setattr(
            "codex_proxy.service.CodexOAuthClient.exchange_callback_code",
            lambda self, code, *, returned_state, authorization: _token_for_usage(
                "acct_fallback", token_id="added"
            ),
        )

        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            begin = await client.post("/login")
            payload = begin.json()
            assert "listener_error" in payload

            done = await client.post(
                f"/login/{payload['login_id']}/complete",
                json={"value": "auth-code#state-1"},
            )
            assert done.status_code == 200
            assert done.json()["token_id"] == "added"
    finally:
        blocker.close()


# --- Token management endpoints ---


@pytest.mark.asyncio
async def test_tokens_promote_and_remove_endpoints(tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    store = StoredCodexTokenStore(store_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    store.save(_token_for_usage("acct_older", token_id="older", created_at=base))
    store.save(
        _token_for_usage("acct_newer", token_id="newer", created_at=base + timedelta(minutes=1))
    )

    settings = _isolated_settings(tmp_path, token_path=store_path, api_key=None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert [t.id for t in store.load_all()] == ["newer", "older"]

        resp = await client.post("/tokens/older/promote")
        assert resp.status_code == 200
        assert [t.id for t in store.load_all()] == ["older", "newer"]

        resp = await client.delete("/tokens/newer")
        assert resp.status_code == 200
        assert [t.id for t in store.load_all()] == ["older"]

        assert (await client.post("/tokens/missing/promote")).status_code == 404
        assert (await client.delete("/tokens/missing")).status_code == 404


@pytest.mark.asyncio
async def test_official_auth_fallback_cannot_be_promoted_or_removed(tmp_path: Path):
    settings = _isolated_settings(tmp_path, api_key=None)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        promote = await client.post(f"/tokens/{CODEX_AUTH_FALLBACK_TOKEN_ID}/promote")
        assert promote.status_code == 404
        assert "codex login" in promote.json()["error"]

        remove = await client.delete(f"/tokens/{CODEX_AUTH_FALLBACK_TOKEN_ID}")
        assert remove.status_code == 404
        assert "codex login" in remove.json()["error"]
