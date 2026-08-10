"""Tests for Grok proxy login, token storage, and refresh helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grok_proxy.__main__ import run_login
from grok_proxy.auth import (
    DEFAULT_GROK_OAUTH_CLIENT_ID,
    DEFAULT_GROK_OAUTH_SCOPE,
    DeviceAuthorizationError,
    GrokDeviceCode,
    GrokOAuthClient,
    GrokOAuthTokens,
    GrokTokenStore,
    StoredGrokToken,
    is_token_fresh,
    is_trusted_xai_oauth_endpoint,
    resolve_expires_at,
)
from grok_proxy.settings import GrokProxySettings


class _SyncResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class _SyncClient:
    def __init__(self, effects: list[dict | tuple[int, dict]], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _next(self) -> _SyncResponse:
        effect = self._effects.pop(0)
        if isinstance(effect, tuple):
            status, payload = effect
            return _SyncResponse(payload, status_code=status)
        return _SyncResponse(effect)

    def post(self, url: str, headers: dict, data: dict):
        self._calls.append({
            "method": "POST",
            "url": url,
            "headers": headers,
            "data": data,
        })
        return self._next()

    def get(self, url: str, headers: dict):
        self._calls.append({
            "method": "GET",
            "url": url,
            "headers": headers,
        })
        return self._next()


def _patch_sync_httpx(monkeypatch, effects: list, calls: list[dict]) -> None:
    monkeypatch.setattr(
        "grok_proxy.auth.httpx.Client",
        lambda timeout: _SyncClient(effects, calls),
    )


def _sample_stored_token(
    *,
    access_token: str = "access-1",
    refresh_token: str = "refresh-1",
    expires_at: datetime | None = None,
) -> StoredGrokToken:
    now = datetime(2026, 5, 1, tzinfo=UTC)
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


def test_token_store_round_trip(tmp_path: Path):
    path = tmp_path / "token.json"
    store = GrokTokenStore(path)
    token = _sample_stored_token()

    store.save(token)
    loaded = store.load()

    assert loaded is not None
    assert loaded.access_token == "access-1"
    assert loaded.refresh_token == "refresh-1"
    assert path.stat().st_mode & 0o777 == 0o600


def test_settings_from_env_uses_saved_token(monkeypatch, tmp_path: Path):
    token_path = tmp_path / "token.json"
    GrokTokenStore(token_path).save(_sample_stored_token())
    monkeypatch.setenv("GROK_PROXY_TOKEN_PATH", str(token_path))
    monkeypatch.delenv("GROK_PROXY_ACCESS_TOKEN", raising=False)

    settings = GrokProxySettings.from_env()

    assert settings.token_path == token_path
    assert settings.port == 4144
    assert settings.access_token is None


def test_settings_from_env_requires_token(monkeypatch, tmp_path: Path):
    token_path = tmp_path / "missing.json"
    monkeypatch.setenv("GROK_PROXY_TOKEN_PATH", str(token_path))
    monkeypatch.delenv("GROK_PROXY_ACCESS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="proxy grok login"):
        GrokProxySettings.from_env()


def test_trusted_endpoint_validation():
    assert is_trusted_xai_oauth_endpoint("https://auth.x.ai/oauth2/token")
    assert is_trusted_xai_oauth_endpoint("https://accounts.x.ai/oauth2/device")
    assert not is_trusted_xai_oauth_endpoint("http://auth.x.ai/oauth2/token")
    assert not is_trusted_xai_oauth_endpoint("https://evil.example/oauth2/token")


def test_is_token_fresh_respects_skew():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    fresh = _sample_stored_token(expires_at=now + timedelta(hours=2))
    near = _sample_stored_token(expires_at=now + timedelta(minutes=30))

    assert is_token_fresh(fresh, skew_seconds=3600, now=now)
    assert not is_token_fresh(near, skew_seconds=3600, now=now)
    assert is_token_fresh(near, skew_seconds=0, now=now)


def test_resolve_expires_at_prefers_expires_in():
    now = datetime(2026, 5, 1, tzinfo=UTC)
    expires = resolve_expires_at(access_token="opaque", expires_in=120, now=now)
    assert expires == now + timedelta(seconds=120)


def test_device_flow_request_and_poll(monkeypatch):
    effects = [
        {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "verification_uri_complete": (
                "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"
            ),
            "expires_in": 900,
            "interval": 1,
        },
        (400, {"error": "authorization_pending"}),
        {
            "access_token": "xai-access",
            "refresh_token": "xai-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)
    monkeypatch.setattr("grok_proxy.auth.time.sleep", lambda _seconds: None)

    client = GrokOAuthClient(request_timeout=30.0)
    device_code = client.request_device_code()
    tokens = client.poll_access_token(
        device_code,
        token_endpoint="https://auth.x.ai/oauth2/token",
    )

    assert device_code.user_code == "ABCD-EFGH"
    assert tokens.access_token == "xai-access"
    assert tokens.refresh_token == "xai-refresh"
    assert calls[0]["url"] == "https://auth.x.ai/oauth2/device/code"
    assert calls[0]["data"]["client_id"] == DEFAULT_GROK_OAUTH_CLIENT_ID
    assert calls[0]["data"]["scope"] == DEFAULT_GROK_OAUTH_SCOPE
    assert calls[1]["data"]["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:device_code"
    )


def test_poll_requires_refresh_token(monkeypatch):
    effects = [
        {
            "access_token": "xai-access",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)
    monkeypatch.setattr("grok_proxy.auth.time.sleep", lambda _seconds: None)

    client = GrokOAuthClient(request_timeout=30.0)
    device = GrokDeviceCode(
        device_code="dev-1",
        user_code="ABCD-EFGH",
        verification_uri="https://accounts.x.ai/oauth2/device",
        expires_in=30,
        interval=1,
    )
    with pytest.raises(DeviceAuthorizationError, match="refresh_token"):
        client.poll_access_token(device, token_endpoint="https://auth.x.ai/oauth2/token")


def test_refresh_reuses_previous_refresh_token(monkeypatch):
    effects = [
        {
            "access_token": "xai-access-2",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)

    client = GrokOAuthClient(request_timeout=30.0)
    tokens = client.refresh(
        "refresh-old",
        token_endpoint="https://auth.x.ai/oauth2/token",
    )
    stored = client.build_stored_token(
        tokens,
        token_endpoint="https://auth.x.ai/oauth2/token",
        source="oauth_refresh",
        previous_refresh_token="refresh-old",
    )

    assert stored.access_token == "xai-access-2"
    assert stored.refresh_token == "refresh-old"
    assert calls[0]["data"]["grant_type"] == "refresh_token"


def test_run_login_saves_token(monkeypatch, tmp_path: Path):
    token_path = tmp_path / "token.json"
    settings = GrokProxySettings(token_path=token_path)

    class _FakeOAuth:
        def discover(self):
            return SimpleNamespace(
                device_authorization_endpoint="https://auth.x.ai/oauth2/device/code",
                token_endpoint="https://auth.x.ai/oauth2/token",
            )

        def request_device_code(self, *, device_authorization_endpoint=None):
            return GrokDeviceCode(
                device_code="dev-1",
                user_code="ABCD-EFGH",
                verification_uri="https://accounts.x.ai/oauth2/device",
                verification_uri_complete=(
                    "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"
                ),
                expires_in=900,
                interval=1,
            )

        def poll_access_token(self, _device_code, *, token_endpoint=None):
            return GrokOAuthTokens(
                access_token="xai-access",
                refresh_token="xai-refresh",
                token_type="Bearer",
                expires_in=3600,
            )

        def build_stored_token(self, tokens, *, token_endpoint, source, created_at=None,
                               previous_refresh_token=None):
            now = datetime(2026, 5, 1, tzinfo=UTC)
            return StoredGrokToken(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token or previous_refresh_token or "",
                expires_at=now + timedelta(hours=1),
                client_id=DEFAULT_GROK_OAUTH_CLIENT_ID,
                token_endpoint=token_endpoint,
                source=source,
                created_at=created_at or now,
                updated_at=now,
            )

    monkeypatch.setattr(
        "grok_proxy.__main__.GrokProxySettings.for_login_from_env",
        lambda: settings,
    )
    monkeypatch.setattr(
        "grok_proxy.__main__._build_oauth_client",
        lambda _settings: _FakeOAuth(),
    )
    monkeypatch.setattr(
        "grok_proxy.__main__.webbrowser.open",
        lambda _url: True,
    )

    result = run_login(
        SimpleNamespace(
            token_path=None,
            client_id=None,
            scope=None,
            no_open_browser=False,
        )
    )

    saved = GrokTokenStore(token_path).load()
    assert result == 0
    assert saved is not None
    assert saved.access_token == "xai-access"
    assert saved.refresh_token == "xai-refresh"
