"""Tests for Claude Code proxy login flows and the multi-token store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from claude_code_proxy.__main__ import (
    _settings_from_serve_args,
    build_serve_parser,
    run_login,
    run_tokens,
)
from claude_code_proxy.auth import (
    ClaudeCodeBrowserAuthorization,
    ClaudeCodeOAuthClient,
    StoredClaudeCodeToken,
    StoredClaudeCodeTokenStore,
)
from claude_code_proxy.settings import ClaudeCodeProxySettings


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

    def post(self, url: str, headers: dict, json: dict):
        self._calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return _SyncResponse(self._effects.pop(0))


def _patch_sync_httpx(monkeypatch, effects: list[dict], calls: list[dict]) -> None:
    monkeypatch.setattr(
        "claude_code_proxy.auth.httpx.Client",
        lambda timeout: _SyncClient(effects, calls),
    )


def _stored_token(
    *,
    access_token: str,
    source: str = "oauth_browser",
    token_id: str = "tok-id",
    created_at: datetime | None = None,
) -> StoredClaudeCodeToken:
    return StoredClaudeCodeToken(
        id=token_id,
        access_token=access_token,
        refresh_token="refresh-token",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        source=source,
        client_id="client-id",
        created_at=created_at or datetime.now(tz=UTC),
    )


def _point_store_at(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "tokens.json"
    monkeypatch.setattr("claude_code_proxy.auth.default_token_path", lambda: path)
    return path


def test_oauth_client_builds_authorization_url_and_exchanges_code(monkeypatch):
    effects = [
        {
            "access_token": "oauth-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)

    client = ClaudeCodeOAuthClient(
        request_timeout=30.0,
        client_id="client-id",
        scope="user:profile user:inference",
    )
    authorization = client.begin_authorization()
    parsed = urlparse(authorization.authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "claude.ai"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["user:profile user:inference"]
    assert query["state"] == [authorization.state]

    stored = client.exchange_manual_code(
        f"auth-code#{authorization.state}",
        authorization=authorization,
    )

    assert stored.access_token == "oauth-token"
    assert stored.source == "oauth_browser"
    assert stored.id  # auto-generated
    assert calls[0]["url"] == "https://console.anthropic.com/v1/oauth/token"
    assert calls[0]["json"]["grant_type"] == "authorization_code"
    assert calls[0]["json"]["state"] == authorization.state


def test_run_login_appends_browser_oauth_token(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    settings = ClaudeCodeProxySettings()

    class _FakeOAuthClient:
        def begin_authorization(self):
            return ClaudeCodeBrowserAuthorization(
                authorization_url="https://claude.ai/oauth/authorize?state=state-1",
                code_verifier="verifier-1",
                state="state-1",
            )

        def exchange_manual_code(self, manual_code, *, authorization):
            assert manual_code == "auth-code#state-1"
            assert authorization.state == "state-1"
            return _stored_token(access_token="oauth-token", token_id="tok-1")

    monkeypatch.setattr(
        "claude_code_proxy.__main__.ClaudeCodeProxySettings.for_login_from_env",
        lambda: settings,
    )
    monkeypatch.setattr(
        "claude_code_proxy.__main__._build_oauth_client",
        lambda _settings: _FakeOAuthClient(),
    )
    monkeypatch.setattr("claude_code_proxy.__main__.webbrowser.open", lambda _url: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "auth-code#state-1")

    result = run_login(
        SimpleNamespace(
            request_timeout=None,
            oauth_client_id=None,
            oauth_scope=None,
            code=None,
            no_open_browser=False,
        )
    )

    saved = StoredClaudeCodeTokenStore(store_path).load_all()
    assert result == 0
    assert len(saved) == 1
    assert saved[0].access_token == "oauth-token"
    assert saved[0].source == "oauth_browser"


def test_second_login_keeps_both_tokens_newest_first(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    store = StoredClaudeCodeTokenStore(store_path)
    store.save(_stored_token(
        access_token="old", token_id="old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    store.save(_stored_token(
        access_token="new", token_id="new", created_at=datetime(2026, 6, 1, tzinfo=UTC)
    ))

    tokens = store.load_all()
    assert [t.id for t in tokens] == ["new", "old"]


def test_promote_moves_token_to_front(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    store = StoredClaudeCodeTokenStore(store_path)
    store.save(_stored_token(
        access_token="a", token_id="a", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    store.save(_stored_token(
        access_token="b", token_id="b", created_at=datetime(2026, 6, 1, tzinfo=UTC)
    ))
    assert [t.id for t in store.load_all()] == ["b", "a"]

    assert store.promote("a") is True
    assert [t.id for t in store.load_all()] == ["a", "b"]
    assert store.promote("missing") is False


def test_remove_deletes_token(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    store = StoredClaudeCodeTokenStore(store_path)
    store.save(_stored_token(access_token="a", token_id="a"))

    assert store.remove("a") is True
    assert store.load_all() == []
    assert store.remove("a") is False


def test_run_tokens_list_and_promote(monkeypatch, tmp_path: Path, capsys):
    store_path = _point_store_at(monkeypatch, tmp_path)
    store = StoredClaudeCodeTokenStore(store_path)
    store.save(_stored_token(
        access_token="a", token_id="alpha", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    ))
    store.save(_stored_token(
        access_token="b", token_id="beta", created_at=datetime(2026, 6, 1, tzinfo=UTC)
    ))

    assert run_tokens(SimpleNamespace(tokens_action="list")) == 0
    listed = capsys.readouterr().out
    assert "beta" in listed and "alpha" in listed
    assert listed.index("beta") < listed.index("alpha")

    assert run_tokens(SimpleNamespace(tokens_action="promote", id="alpha")) == 0
    assert [t.id for t in store.load_all()] == ["alpha", "beta"]

    assert run_tokens(SimpleNamespace(tokens_action="remove", id="missing")) == 1


def test_legacy_single_token_file_is_migrated(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    legacy_path = store_path.with_name("token.json")
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": "legacy-token",
                "refresh_token": "legacy-refresh",
                "expires_at": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
                "source": "oauth_browser",
                "client_id": "client-id",
                "created_at": datetime.now(tz=UTC).isoformat(),
            }
        )
    )

    tokens = StoredClaudeCodeTokenStore(store_path).load_all()

    assert len(tokens) == 1
    assert tokens[0].access_token == "legacy-token"
    assert tokens[0].id  # id assigned during migration
    assert not legacy_path.exists()


def _legacy_payload(access_token: str) -> str:
    return json.dumps(
        {
            "version": 1,
            "access_token": access_token,
            "refresh_token": "legacy-refresh",
            "expires_at": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
            "source": "oauth_browser",
            "client_id": "client-id",
            "created_at": datetime.now(tz=UTC).isoformat(),
        }
    )


def test_migration_preserves_existing_valid_tokens_with_a_bad_record(monkeypatch, tmp_path: Path):
    store_path = _point_store_at(monkeypatch, tmp_path)
    good1 = _stored_token(access_token="g1", token_id="g1").model_dump(mode="json")
    good2 = _stored_token(access_token="g2", token_id="g2").model_dump(mode="json")
    store_path.write_text(json.dumps([good1, good2, {"garbage": 1}]))
    legacy = store_path.with_name("token.json")
    legacy.write_text(_legacy_payload("legacy-token"))

    tokens = StoredClaudeCodeTokenStore(store_path).load_all()

    access = {t.access_token for t in tokens}
    assert access == {"g1", "g2", "legacy-token"}  # nothing lost, legacy folded in
    assert not legacy.exists()


def test_custom_path_store_does_not_touch_default_location_legacy(monkeypatch, tmp_path: Path):
    # Regression: a custom-path store must derive its legacy file from its own path,
    # never delete the real default-location token.json.
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    default_legacy = default_dir / "token.json"
    default_legacy.write_text(_legacy_payload("real-credentials"))
    monkeypatch.setattr(
        "claude_code_proxy.auth.default_token_path", lambda: default_dir / "tokens.json"
    )

    custom_path = tmp_path / "custom" / "tokens.json"
    StoredClaudeCodeTokenStore(custom_path).load_all()

    assert default_legacy.exists()  # the real default-location file is untouched


def test_naive_datetime_is_coerced_to_utc():
    token = StoredClaudeCodeToken.model_validate(
        {
            "id": "x",
            "access_token": "a",
            "expires_at": "2026-06-01T00:00:00",
            "source": "oauth_browser",
            "client_id": "c",
            "created_at": "2026-06-01T00:00:00",
        }
    )
    assert token.created_at.tzinfo is not None
    assert token.expires_at.tzinfo is not None


def test_serve_port_zero_and_timeout_zero_are_honored(monkeypatch):
    for name in ("CLAUDE_CODE_PROXY_PORT", "CLAUDE_CODE_PROXY_REQUEST_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    args = build_serve_parser().parse_args(["--port", "0", "--request-timeout", "0"])
    settings = _settings_from_serve_args(args)
    assert settings.port == 0
    assert settings.request_timeout == 0.0
