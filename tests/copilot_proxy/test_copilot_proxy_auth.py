"""Tests for Copilot proxy login and token storage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


from copilot_proxy.__main__ import run_login
from copilot_proxy.auth import (
    GitHubAccessToken,
    GitHubDeviceCode,
    GitHubDeviceFlowClient,
    GitHubTokenStore,
    StoredGitHubToken,
)
from copilot_proxy.settings import CopilotProxySettings


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
    def __init__(self, effects: list[dict], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, headers: dict, data: dict):
        self._calls.append({
            "method": "POST",
            "url": url,
            "headers": headers,
            "data": data,
        })
        return _SyncResponse(self._effects.pop(0))

    def get(self, url: str, headers: dict):
        self._calls.append({
            "method": "GET",
            "url": url,
            "headers": headers,
        })
        return _SyncResponse(self._effects.pop(0))


def _patch_sync_httpx(monkeypatch, effects: list[dict], calls: list[dict]) -> None:
    monkeypatch.setattr(
        "copilot_proxy.auth.httpx.Client",
        lambda timeout: _SyncClient(effects, calls),
    )


def test_token_store_round_trip(tmp_path: Path):
    path = tmp_path / "github-token.json"
    store = GitHubTokenStore(path)
    token = StoredGitHubToken(
        github_token="gho_test",
        scope="read:user",
        client_id="client-id",
        created_at="2026-03-13T00:00:00Z",
        github_login="octocat",
    )

    store.save(token)
    loaded = store.load()

    assert loaded is not None
    assert loaded.github_token == "gho_test"
    assert loaded.github_login == "octocat"


def test_settings_from_env_uses_saved_token(monkeypatch, tmp_path: Path):
    token_path = tmp_path / "github-token.json"
    GitHubTokenStore(token_path).save(
        StoredGitHubToken(
            github_token="gho_saved",
            scope="read:user",
            client_id="client-id",
            created_at="2026-03-13T00:00:00Z",
        )
    )
    monkeypatch.setenv("COPILOT_PROXY_TOKEN_PATH", str(token_path))
    monkeypatch.delenv("COPILOT_PROXY_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    settings = CopilotProxySettings.from_env()

    assert settings.github_token == "gho_saved"
    assert settings.token_path == token_path


def test_device_flow_client_polls_and_verifies(monkeypatch):
    effects = [
        {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 1,
        },
        {"error": "authorization_pending"},
        {
            "access_token": "gho_device",
            "token_type": "bearer",
            "scope": "read:user",
        },
        {"token": "copilot-token", "expires_at": 9999999999},
        {"login": "octocat"},
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)
    monkeypatch.setattr("copilot_proxy.auth.time.sleep", lambda _seconds: None)
    client = GitHubDeviceFlowClient(
        auth_base_url="https://github.com",
        github_api_base_url="https://api.github.com",
        client_id="client-id",
        scope="read:user",
        request_timeout=30.0,
        editor_version="1.109.5",
        editor_plugin_version="copilot-chat/0.38.2",
        user_agent="GitHubCopilotChat/0.38.2",
        api_version="2025-10-01",
    )

    device_code = client.request_device_code()
    access_token = client.poll_access_token(device_code)
    client.verify_copilot_access(access_token.access_token)
    github_login = client.fetch_user_login(access_token.access_token)

    assert device_code.user_code == "ABCD-EFGH"
    assert access_token.access_token == "gho_device"
    assert github_login == "octocat"
    assert calls[0]["url"].endswith("/login/device/code")
    assert calls[1]["url"].endswith("/login/oauth/access_token")
    assert calls[3]["url"].endswith("/copilot_internal/v2/token")
    assert calls[4]["url"].endswith("/user")


def test_run_login_saves_token(monkeypatch, tmp_path: Path):
    token_path = tmp_path / "github-token.json"
    settings = CopilotProxySettings(
        github_token="",
        token_path=token_path,
    )

    class _FakeDeviceFlow:
        def request_device_code(self):
            return GitHubDeviceCode(
                device_code="dev-1",
                user_code="ABCD-EFGH",
                verification_uri="https://github.com/login/device",
                expires_in=900,
                interval=1,
            )

        def poll_access_token(self, _device_code):
            return GitHubAccessToken(
                access_token="gho_saved",
                token_type="bearer",
                scope="read:user",
            )

        def verify_copilot_access(self, _github_token):
            return None

        def fetch_user_login(self, _github_token):
            return "octocat"

        def build_stored_token(self, access_token, *, github_login):
            return StoredGitHubToken(
                github_token=access_token.access_token,
                token_type=access_token.token_type,
                scope=access_token.scope,
                client_id="client-id",
                created_at="2026-03-13T00:00:00Z",
                github_login=github_login,
            )

    monkeypatch.setattr(
        "copilot_proxy.__main__.CopilotProxySettings.for_login_from_env",
        lambda: settings,
    )
    monkeypatch.setattr(
        "copilot_proxy.__main__._build_device_flow_client",
        lambda _settings: _FakeDeviceFlow(),
    )
    monkeypatch.setattr(
        "copilot_proxy.__main__.webbrowser.open",
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

    saved = GitHubTokenStore(token_path).load()
    assert result == 0
    assert saved is not None
    assert saved.github_token == "gho_saved"
    assert saved.github_login == "octocat"
