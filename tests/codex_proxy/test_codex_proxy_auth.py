"""Tests for Codex proxy auth: official-file loading, the multi-token store,
browser OAuth, and manual callback parsing.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from codex_proxy.auth import (
    CODEX_AUTH_FALLBACK_TOKEN_ID,
    CodexAuthLoader,
    CodexOAuthClient,
    StoredCodexToken,
    StoredCodexTokenStore,
    build_pkce_pair,
    build_state_token,
    default_codex_auth_path,
    default_token_path,
    extract_chatgpt_account_id,
    new_token_id,
    parse_manual_callback_value,
)
from codex_proxy.settings import CodexProxySettings


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


def _stored_token(
    *,
    access_token: str,
    source: str = "oauth_browser",
    token_id: str = "tok-id",
    account_id: str = "acct_1",
    created_at: datetime | None = None,
) -> StoredCodexToken:
    return StoredCodexToken(
        id=token_id,
        access_token=access_token,
        refresh_token="refresh-token",
        account_id=account_id,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        source=source,
        client_id="client-id",
        created_at=created_at or datetime.now(tz=UTC),
    )


# --- CodexAuthLoader (official ~/.codex/auth.json, read-only) ---


def test_codex_auth_loader_reads_official_auth_json(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    access_token = _make_fake_jwt(account_id="acct_loader")
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "refresh-loader",
                },
            }
        )
    )

    loaded = CodexAuthLoader(path=auth_path).load()

    assert loaded is not None
    assert loaded.id == CODEX_AUTH_FALLBACK_TOKEN_ID
    assert loaded.account_id == "acct_loader"
    assert loaded.refresh_token == "refresh-loader"
    assert loaded.source == "codex_auth"
    assert extract_chatgpt_account_id(loaded.access_token) == "acct_loader"


def test_codex_auth_loader_returns_none_when_file_missing(tmp_path: Path):
    assert CodexAuthLoader(path=tmp_path / "missing.json").load() is None


# --- Settings: codex_auth_path stays pinned; token_path is a real override ---


def test_settings_from_env_pins_codex_auth_path_and_honors_token_path(monkeypatch, tmp_path: Path):
    custom_auth_path = tmp_path / "custom-auth.json"
    custom_token_path = tmp_path / "custom-tokens.json"
    monkeypatch.setenv("CODEX_PROXY_CODEX_AUTH_PATH", str(custom_auth_path))
    monkeypatch.setenv("CODEX_PROXY_TOKEN_PATH", str(custom_token_path))

    settings = CodexProxySettings.from_env()

    # There is no env override for codex_auth_path: only ~/.codex/auth.json is
    # ever treated as "the official CLI auth file".
    assert settings.codex_auth_path == default_codex_auth_path()
    assert settings.codex_auth_path != custom_auth_path
    assert settings.token_path == custom_token_path


def test_settings_from_env_defaults_token_path(monkeypatch):
    monkeypatch.delenv("CODEX_PROXY_TOKEN_PATH", raising=False)

    settings = CodexProxySettings.from_env()

    assert settings.token_path == default_token_path()


def test_default_token_path_uses_codex_proxy_directory():
    path = default_token_path()
    assert path.name == "tokens.json"
    assert "codex-proxy" in str(path)


# --- StoredCodexTokenStore (multi-token store) ---


def test_token_store_round_trip(tmp_path: Path):
    store = StoredCodexTokenStore(tmp_path / "tokens.json")
    store.save(_stored_token(access_token="a", token_id="a"))

    tokens = store.load_all()

    assert len(tokens) == 1
    assert tokens[0].access_token == "a"
    assert tokens[0].account_id == "acct_1"


def test_token_store_orders_newest_first(tmp_path: Path):
    store = StoredCodexTokenStore(tmp_path / "tokens.json")
    store.save(
        _stored_token(access_token="old", token_id="old", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.save(
        _stored_token(access_token="new", token_id="new", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    )

    assert [t.id for t in store.load_all()] == ["new", "old"]


def test_token_store_promote_moves_to_front(tmp_path: Path):
    store = StoredCodexTokenStore(tmp_path / "tokens.json")
    store.save(
        _stored_token(access_token="a", token_id="a", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    store.save(
        _stored_token(access_token="b", token_id="b", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    )
    assert [t.id for t in store.load_all()] == ["b", "a"]

    assert store.promote("a") is True
    assert [t.id for t in store.load_all()] == ["a", "b"]
    assert store.promote("missing") is False


def test_token_store_remove(tmp_path: Path):
    store = StoredCodexTokenStore(tmp_path / "tokens.json")
    store.save(_stored_token(access_token="a", token_id="a"))

    assert store.remove("a") is True
    assert store.load_all() == []
    assert store.remove("a") is False


def test_store_file_has_restrictive_perms_and_lock_file_is_created(tmp_path: Path):
    path = tmp_path / "tokens.json"
    store = StoredCodexTokenStore(path)

    store.save(_stored_token(access_token="a", token_id="a"))

    assert (path.stat().st_mode & 0o777) == 0o600
    assert path.with_suffix(path.suffix + ".lock").exists()


def test_malformed_record_does_not_poison_the_store(tmp_path: Path):
    path = tmp_path / "tokens.json"
    good = _stored_token(access_token="good", token_id="good").model_dump(mode="json")
    path.write_text(json.dumps([good, {"garbage": True}]))

    assert [t.id for t in StoredCodexTokenStore(path).load_all()] == ["good"]


def test_replace_all_overwrites_store(tmp_path: Path):
    store = StoredCodexTokenStore(tmp_path / "tokens.json")
    store.save(_stored_token(access_token="a", token_id="a"))

    store.replace_all([_stored_token(access_token="b", token_id="b")])

    assert [t.id for t in store.load_all()] == ["b"]


def test_new_token_id_returns_distinct_ids():
    assert new_token_id() != new_token_id()


# --- PKCE / state helpers ---


def test_build_pkce_pair_challenge_matches_verifier():
    verifier, challenge = build_pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert len(verifier) > 32


def test_build_state_token_is_unique_each_call():
    assert build_state_token() != build_state_token()


# --- Browser OAuth client ---


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
        self._calls.append({"method": "POST", "url": url, "headers": headers, "data": data})
        return _SyncResponse(self._effects.pop(0))


def _patch_sync_httpx(monkeypatch, effects: list[dict], calls: list[dict]) -> None:
    monkeypatch.setattr(
        "codex_proxy.auth.httpx.Client",
        lambda timeout: _SyncClient(effects, calls),
    )


def test_oauth_client_builds_authorization_url_and_exchanges_code(monkeypatch):
    effects = [
        {
            "access_token": _make_fake_jwt(account_id="acct_oauth"),
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
    ]
    calls: list[dict] = []
    _patch_sync_httpx(monkeypatch, effects, calls)

    client = CodexOAuthClient(
        request_timeout=30.0,
        client_id="client-id",
        scope="openid profile email offline_access",
    )
    authorization = client.begin_authorization()
    parsed = urlparse(authorization.authorization_url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert query["state"] == [authorization.state]
    assert query["originator"] == ["codex_cli_rs"]

    stored = client.exchange_callback_code(
        "auth-code",
        returned_state=authorization.state,
        authorization=authorization,
    )

    assert stored.account_id == "acct_oauth"
    assert stored.source == "oauth_browser"
    assert stored.id  # auto-generated, not the fixed fallback sentinel
    assert stored.id != CODEX_AUTH_FALLBACK_TOKEN_ID
    assert calls[0]["url"] == "https://auth.openai.com/oauth/token"
    assert calls[0]["data"]["grant_type"] == "authorization_code"
    assert calls[0]["data"]["redirect_uri"] == authorization.redirect_uri


def test_oauth_client_exchange_rejects_state_mismatch():
    client = CodexOAuthClient(request_timeout=30.0)
    authorization = client.begin_authorization()

    with pytest.raises(ValueError, match="state mismatch"):
        client.exchange_callback_code(
            "auth-code", returned_state="wrong-state", authorization=authorization
        )


# --- parse_manual_callback_value ---


def test_parse_manual_callback_value_accepts_full_url():
    code, state = parse_manual_callback_value(
        "http://localhost:1455/auth/callback?code=abc123&state=xyz789"
    )
    assert code == "abc123"
    assert state == "xyz789"


def test_parse_manual_callback_value_accepts_code_hash_state():
    code, state = parse_manual_callback_value("abc123#xyz789")
    assert code == "abc123"
    assert state == "xyz789"


def test_parse_manual_callback_value_strips_whitespace():
    code, state = parse_manual_callback_value("  abc123#xyz789  ")
    assert code == "abc123"
    assert state == "xyz789"


@pytest.mark.parametrize("garbage", ["not-a-valid-value", "", "   ", "abc123", "abc123#"])
def test_parse_manual_callback_value_rejects_garbage(garbage: str):
    with pytest.raises(ValueError):
        parse_manual_callback_value(garbage)


def test_parse_manual_callback_value_surfaces_url_error_param():
    with pytest.raises(ValueError, match="access_denied"):
        parse_manual_callback_value(
            "http://localhost:1455/auth/callback?error=access_denied&state=xyz"
        )


def test_parse_manual_callback_value_rejects_url_missing_code():
    with pytest.raises(ValueError):
        parse_manual_callback_value("http://localhost:1455/auth/callback?state=xyz789")
