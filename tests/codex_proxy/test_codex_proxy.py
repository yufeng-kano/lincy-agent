"""Tests for the native Codex proxy transport."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import pytest

from codex_proxy.auth import CODEX_AUTH_FALLBACK_TOKEN_ID, StoredCodexToken, StoredCodexTokenStore
from codex_proxy.service import (
    FAILURE_COOLDOWN_SECONDS,
    CodexProxyService,
    CodexTokenManager,
    CodexTokenUnavailableError,
    CodexUpstreamError,
)
from codex_proxy.settings import CodexProxySettings
from lincy.llm.schema import (
    CodexCompactRequest,
    CodexNativeRequest,
    Message,
    ToolDefinition,
    ToolParameter,
)


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


def _settings_with_codex_auth(tmp_path: Path, *, account_id: str) -> CodexProxySettings:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id=account_id),
                    "refresh_token": "refresh-token",
                },
            }
        )
    )
    # Point the store at an empty tmp path so the official auth file is the
    # only pool entry, instead of touching the real default token store.
    return CodexProxySettings(codex_auth_path=auth_path, token_path=tmp_path / "tokens.json")


class _AsyncResponse:
    def __init__(
        self,
        text: str,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._text = text
        self.headers = headers or {"content-type": "text/event-stream"}

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> dict:
        return json.loads(self._text)


class _AsyncClient:
    def __init__(self, effects: list[_AsyncResponse], calls: list[dict]):
        self._effects = effects
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(
        self,
        url: str,
        headers: dict,
        json: dict | None = None,
        data: dict | None = None,
    ):
        call = {"method": "POST", "url": url, "headers": headers}
        if json is not None:
            call["json"] = json
        if data is not None:
            call["data"] = data
        self._calls.append(call)
        effect = self._effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _patch_async_httpx(
    monkeypatch, effects: list[_AsyncResponse | Exception], calls: list[dict]
) -> None:
    monkeypatch.setattr(
        "codex_proxy.service.httpx.AsyncClient",
        lambda timeout: _AsyncClient(effects, calls),
    )


def _fresh_token(
    *,
    token_id: str,
    account_id: str,
    created_at: datetime,
    refresh_token: str | None = None,
) -> StoredCodexToken:
    return StoredCodexToken(
        id=token_id,
        access_token=_make_fake_jwt(account_id=account_id),
        refresh_token=refresh_token,
        account_id=account_id,
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        source="oauth_browser",
        client_id="client-id",
        created_at=created_at,
    )


def _expired_token(
    *,
    token_id: str,
    account_id: str,
    created_at: datetime,
    refresh_token: str | None = "refresh-old",
) -> StoredCodexToken:
    return StoredCodexToken(
        id=token_id,
        access_token=_make_fake_jwt(account_id=account_id, exp=1_700_000_000),
        refresh_token=refresh_token,
        account_id=account_id,
        expires_at=datetime.now(tz=UTC) - timedelta(hours=1),
        source="oauth_browser",
        client_id="client-id",
        created_at=created_at,
    )


def _request(model: str = "gpt-5.4") -> CodexNativeRequest:
    return CodexNativeRequest(model=model, messages=[Message(role="user", content="hi")])


def _isolated_settings(tmp_path: Path, *, token_path: Path) -> CodexProxySettings:
    """Settings pinned away from any real ~/.codex/auth.json on this machine.

    Pool tests care only about the store; a developer's own `codex login`
    state must never leak in as a surprise extra pool entry.
    """

    return CodexProxySettings(codex_auth_path=tmp_path / "no-official-auth.json", token_path=token_path)


@pytest.mark.asyncio
async def test_proxy_service_calls_upstream_and_parses_text(monkeypatch, tmp_path: Path):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello from codex"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18,"input_tokens_details":{"cached_tokens":3}}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_proxy"))

    request = CodexNativeRequest(
        model="gpt-5.2-codex",
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content="hi"),
        ],
    )

    response = await service.chat(request)

    assert response.content == "hello from codex"
    assert response.prompt_tokens == 11
    assert response.cache_read_tokens == 3
    assert calls[0]["url"].endswith("/codex/responses")
    assert calls[0]["headers"]["chatgpt-account-id"] == "acct_proxy"
    assert calls[0]["json"]["instructions"] == "You are helpful."


@pytest.mark.asyncio
async def test_proxy_service_refreshes_expired_official_codex_auth(
    monkeypatch,
    tmp_path: Path,
):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(
                        account_id="acct_expired",
                        exp=1_700_000_000,
                    ),
                    "refresh_token": "refresh-expired",
                },
            }
        )
    )
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps(
                {
                    "access_token": _make_fake_jwt(account_id="acct_refreshed"),
                    "refresh_token": "refresh-next",
                }
            ),
            headers={"content-type": "application/json"},
        ),
        _AsyncResponse(sse),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(
        CodexProxySettings(codex_auth_path=auth_path, token_path=tmp_path / "tokens.json")
    )

    response = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[Message(role="user", content="hi")],
        )
    )

    assert response.content == "ok"
    assert calls[0]["url"] == "https://auth.openai.com/oauth/token"
    assert calls[0]["data"]["refresh_token"] == "refresh-expired"
    assert calls[1]["headers"]["chatgpt-account-id"] == "acct_refreshed"


@pytest.mark.asyncio
async def test_proxy_service_translates_tools_and_response_schema(
    monkeypatch,
    tmp_path: Path,
):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_tools"))
    request = CodexNativeRequest(
        model="gpt-5.2-codex",
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
        reasoning_effort="medium",
    )

    response = await service.chat(request)

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    payload = calls[0]["json"]
    assert payload["tools"][0]["name"] == "read_file"
    assert payload["text"]["format"]["schema"]["type"] == "object"
    assert payload["reasoning"]["effort"] == "medium"


@pytest.mark.asyncio
async def test_proxy_service_drops_max_output_tokens_for_chatgpt_backend(
    monkeypatch,
    tmp_path: Path,
):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_tokens"))
    request = CodexNativeRequest(
        model="gpt-5.4",
        messages=[Message(role="user", content="hi")],
        max_output_tokens=64,
    )

    response = await service.chat(request)

    assert response.content == "ok"
    assert "max_output_tokens" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_proxy_service_forwards_prompt_cache_key(monkeypatch, tmp_path: Path):
    sse = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [_AsyncResponse(sse)]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_cache"))
    request = CodexNativeRequest(
        model="gpt-5.4",
        messages=[Message(role="user", content="hi")],
        prompt_cache_key="session-1:brain:20260411",
    )

    response = await service.chat(request)

    assert response.content == "ok"
    assert calls[0]["json"]["prompt_cache_key"] == "session-1:brain:20260411"


@pytest.mark.asyncio
async def test_proxy_service_replays_turn_state_within_same_turn(
    monkeypatch,
    tmp_path: Path,
):
    sse_first = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}"}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    sse_second = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":1,"total_tokens":8}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            sse_first,
            headers={
                "content-type": "text/event-stream",
                "x-codex-turn-state": "ts-1",
            },
        ),
        _AsyncResponse(sse_second),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(
        _settings_with_codex_auth(tmp_path, account_id="acct_turn_state")
    )

    first = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[Message(role="user", content="hi")],
            session_id="20260411_abcdef",
            turn_id="turn_000123",
        )
    )
    second = await service.chat(
        CodexNativeRequest(
            model="gpt-5.4",
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content=None, tool_calls=first.tool_calls),
                Message(
                    role="tool",
                    tool_call_id="call_1",
                    name="read_file",
                    content="README contents",
                ),
            ],
            session_id="20260411_abcdef",
            turn_id="turn_000123",
        )
    )

    assert second.content == "done"
    assert "x-codex-turn-state" not in calls[0]["headers"]
    assert calls[0]["headers"]["session_id"] == "20260411_abcdef"
    assert calls[1]["headers"]["x-codex-turn-state"] == "ts-1"
    assert calls[1]["headers"]["session_id"] == "20260411_abcdef"
    assert json.loads(calls[1]["headers"]["x-codex-turn-metadata"]) == {
        "turn_id": "turn_000123"
    }


@pytest.mark.asyncio
async def test_proxy_service_calls_compact_endpoint_and_maps_compaction_items(
    monkeypatch,
    tmp_path: Path,
):
    effects = [
        _AsyncResponse(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "compaction_summary",
                            "encrypted_content": "enc_123",
                        }
                    ]
                }
            ),
            headers={"content-type": "application/json"},
        )
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_settings_with_codex_auth(tmp_path, account_id="acct_compact"))

    response = await service.compact(
        CodexCompactRequest(
            model="gpt-5.4",
            messages=[
                Message(role="system", content="You are helpful."),
                Message(role="user", content="hi"),
            ],
        )
    )

    assert calls[0]["url"].endswith("/codex/responses/compact")
    assert calls[0]["json"]["instructions"] == "You are helpful."
    assert response.messages[0].codex_compaction_encrypted_content == "enc_123"


# --- Token pool: priority, dedup, benching ---


@pytest.mark.asyncio
async def test_acquire_prioritizes_store_tokens_over_official_auth(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id="acct_official"),
                    "refresh_token": "refresh-official",
                },
            }
        )
    )
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(
        _fresh_token(
            token_id="store-tok", account_id="acct_store", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    manager = CodexTokenManager(CodexProxySettings(codex_auth_path=auth_path, token_path=store_path))

    token, token_id = await manager.acquire()

    assert token_id == "store-tok"
    assert token.account_id == "acct_store"


@pytest.mark.asyncio
async def test_official_auth_deduped_when_store_has_same_account(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id="acct_shared"),
                    "refresh_token": "refresh-official",
                },
            }
        )
    )
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(
        _fresh_token(
            token_id="store-tok", account_id="acct_shared", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    manager = CodexTokenManager(CodexProxySettings(codex_auth_path=auth_path, token_path=store_path))

    entries = await manager.pool_entries()

    assert [e.token_id for e in entries] == ["store-tok"]


@pytest.mark.asyncio
async def test_official_auth_included_when_account_differs_from_store(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "last_refresh": "2026-04-11T01:02:03Z",
                "tokens": {
                    "access_token": _make_fake_jwt(account_id="acct_official"),
                    "refresh_token": "refresh-official",
                },
            }
        )
    )
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(
        _fresh_token(
            token_id="store-tok", account_id="acct_store", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    manager = CodexTokenManager(CodexProxySettings(codex_auth_path=auth_path, token_path=store_path))

    entries = await manager.pool_entries()

    assert [e.token_id for e in entries] == ["store-tok", CODEX_AUTH_FALLBACK_TOKEN_ID]


@pytest.mark.asyncio
async def test_mark_failed_benches_token_and_rejoins_after_cooldown(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="backup", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr("codex_proxy.service._now", lambda: clock["t"])
    manager = CodexTokenManager(_isolated_settings(tmp_path, token_path=store_path))

    _, token_id = await manager.acquire()
    assert token_id == "primary"

    manager.mark_failed("primary")
    _, token_id = await manager.acquire()
    assert token_id == "backup"

    clock["t"] += FAILURE_COOLDOWN_SECONDS + 1
    _, token_id = await manager.acquire()
    assert token_id == "primary"


@pytest.mark.asyncio
async def test_pool_entries_reports_benched_status(tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="backup", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    manager = CodexTokenManager(_isolated_settings(tmp_path, token_path=store_path))
    manager.mark_failed("primary")

    entries = await manager.pool_entries()

    assert [(e.token_id, e.benched) for e in entries] == [("primary", True), ("backup", False)]


@pytest.mark.asyncio
async def test_token_manager_raises_when_no_tokens_available(tmp_path: Path):
    service = CodexProxyService(
        CodexProxySettings(
            codex_auth_path=tmp_path / "missing-auth.json", token_path=tmp_path / "tokens.json"
        )
    )
    with pytest.raises(CodexTokenUnavailableError, match="proxy codex login"):
        await service.chat(_request())


# --- Refresh writeback: store tokens persist, official-file tokens do not ---


@pytest.mark.asyncio
async def test_store_token_refresh_writes_back_to_store(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).save(
        _expired_token(
            token_id="tok", account_id="acct_old", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
    )
    refreshed_access_token = _make_fake_jwt(account_id="acct_old")
    effects = [
        _AsyncResponse(
            json.dumps({"access_token": refreshed_access_token, "refresh_token": "refresh-next"}),
            headers={"content-type": "application/json"},
        )
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    manager = CodexTokenManager(_isolated_settings(tmp_path, token_path=store_path))

    token, token_id = await manager.acquire()

    assert token_id == "tok"
    assert token.source == "oauth_refresh"
    assert token.access_token == refreshed_access_token
    stored = StoredCodexTokenStore(store_path).load_all()
    assert len(stored) == 1
    assert stored[0].access_token == refreshed_access_token
    assert stored[0].source == "oauth_refresh"


@pytest.mark.asyncio
async def test_official_auth_refresh_stays_in_memory_and_never_touches_the_file(
    monkeypatch, tmp_path: Path
):
    auth_path = tmp_path / "auth.json"
    original_text = json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": "2026-04-11T01:02:03Z",
            "tokens": {
                "access_token": _make_fake_jwt(account_id="acct_official", exp=1_700_000_000),
                "refresh_token": "refresh-official",
            },
        }
    )
    auth_path.write_text(original_text)
    store_path = tmp_path / "tokens.json"
    refreshed_access_token = _make_fake_jwt(account_id="acct_official")
    effects = [
        _AsyncResponse(
            json.dumps({"access_token": refreshed_access_token, "refresh_token": "refresh-next"}),
            headers={"content-type": "application/json"},
        )
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    manager = CodexTokenManager(CodexProxySettings(codex_auth_path=auth_path, token_path=store_path))

    token, token_id = await manager.acquire()

    assert token_id == CODEX_AUTH_FALLBACK_TOKEN_ID
    assert token.access_token == refreshed_access_token
    # The official auth file itself must be untouched.
    assert auth_path.read_text() == original_text
    # And the refreshed token must not have leaked into the store either.
    assert StoredCodexTokenStore(store_path).load_all() == []


# --- Upstream failover: 401/403/429 and read-timeout ---


@pytest.mark.asyncio
async def test_chat_fails_over_to_next_token_on_401(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="backup", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    sse_ok = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps({"error": "unauthorized"}), status_code=401, headers={"content-type": "application/json"}
        ),
        _AsyncResponse(sse_ok),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_isolated_settings(tmp_path, token_path=store_path))

    response = await service.chat(_request())

    assert response.content == "ok"
    assert calls[0]["headers"]["chatgpt-account-id"] == "acct_a"
    assert calls[1]["headers"]["chatgpt-account-id"] == "acct_b"


@pytest.mark.asyncio
async def test_chat_fails_over_to_next_token_on_429(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="backup", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    sse_ok = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps({"error": "rate_limited"}), status_code=429, headers={"content-type": "application/json"}
        ),
        _AsyncResponse(sse_ok),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_isolated_settings(tmp_path, token_path=store_path))

    response = await service.chat(_request())

    assert response.content == "ok"
    assert calls[0]["headers"]["chatgpt-account-id"] == "acct_a"
    assert calls[1]["headers"]["chatgpt-account-id"] == "acct_b"


@pytest.mark.asyncio
async def test_chat_fails_over_to_next_token_on_read_timeout(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="backup", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    sse_ok = "\n\n".join(
        [
            'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6}}}',
        ]
    )
    effects = [
        httpx.ReadTimeout("timed out while waiting for response headers"),
        _AsyncResponse(sse_ok),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_isolated_settings(tmp_path, token_path=store_path))

    response = await service.chat(_request())

    assert response.content == "ok"
    assert calls[0]["headers"]["chatgpt-account-id"] == "acct_a"
    assert calls[1]["headers"]["chatgpt-account-id"] == "acct_b"


@pytest.mark.asyncio
async def test_chat_surfaces_upstream_error_when_all_tokens_fail(monkeypatch, tmp_path: Path):
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="a", account_id="acct_a", created_at=datetime(2026, 2, 1, tzinfo=UTC)
            ),
            _fresh_token(
                token_id="b", account_id="acct_b", created_at=datetime(2026, 1, 1, tzinfo=UTC)
            ),
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps({"error": "unauthorized"}), status_code=401, headers={"content-type": "application/json"}
        ),
        _AsyncResponse(
            json.dumps({"error": "unauthorized"}), status_code=401, headers={"content-type": "application/json"}
        ),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_isolated_settings(tmp_path, token_path=store_path))

    with pytest.raises(CodexUpstreamError) as excinfo:
        await service.chat(_request())

    assert excinfo.value.status_code == 401
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_401_surfaces_upstream_error_when_only_fallback_is_unusable(monkeypatch, tmp_path: Path):
    # Primary is fresh but gets 401; the only other token is expired with no refresh,
    # so failover cannot proceed. The client must see the real 401, not a 503.
    store_path = tmp_path / "tokens.json"
    StoredCodexTokenStore(store_path).replace_all(
        [
            _fresh_token(
                token_id="primary", account_id="acct_a", created_at=datetime(2026, 6, 1, tzinfo=UTC)
            ),
            _expired_token(
                token_id="backup",
                account_id="acct_b",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                refresh_token=None,
            ),
        ]
    )
    effects = [
        _AsyncResponse(
            json.dumps({"error": "unauthorized"}), status_code=401, headers={"content-type": "application/json"}
        ),
    ]
    calls: list[dict] = []
    _patch_async_httpx(monkeypatch, effects, calls)
    service = CodexProxyService(_isolated_settings(tmp_path, token_path=store_path))

    with pytest.raises(CodexUpstreamError) as excinfo:
        await service.chat(_request())

    assert excinfo.value.status_code == 401
    assert len(calls) == 1  # backup is never called; it is unusable
