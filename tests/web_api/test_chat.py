from __future__ import annotations

import httpx
import pytest

import chat_web_api.app as app_mod
from lincy.agent.web_chat import WebChatStore
from chat_web_api.app import create_app
from chat_web_api.settings import WebApiSettings


def _settings(tmp_path, *, control_base_url: str = "http://control") -> WebApiSettings:
    return WebApiSettings(
        sessions_dir=tmp_path / "sessions",
        web_chat_events_path=tmp_path / "web_chat" / "events.jsonl",
        control_base_url=control_base_url,
        pricing_cache_path=tmp_path / "pricing.json",
    )


@pytest.mark.asyncio
async def test_chat_events_returns_recent_events(tmp_path):
    settings = _settings(tmp_path)
    store = WebChatStore(settings.web_chat_events_path)
    store.append_event(kind="message", role="user", content="first")
    second = store.append_event(kind="message", role="assistant", content="second")
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/chat/events?limit=1")

    assert resp.status_code == 200
    assert resp.json()["events"] == [second.model_dump(mode="json")]


@pytest.mark.asyncio
async def test_chat_message_forwards_to_control_api(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    seen = {}

    async def fake_post(chat_settings: WebApiSettings, text: str, channel: str = "cli"):
        seen["control_base_url"] = chat_settings.control_base_url
        seen["text"] = text
        seen["channel"] = channel
        return 202, {"status": "accepted", "channel": channel}

    monkeypatch.setattr(app_mod, "_post_web_chat_message_to_control", fake_post)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/messages", json={"content": " hello "})

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "channel": "cli"}
    assert seen == {
        "control_base_url": "http://control",
        "text": "hello",
        "channel": "cli",
    }


@pytest.mark.asyncio
async def test_chat_message_forwards_selected_channel(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    seen = {}

    async def fake_post(_settings: WebApiSettings, text: str, channel: str = "cli"):
        seen["text"] = text
        seen["channel"] = channel
        return 202, {"status": "accepted", "channel": channel}

    monkeypatch.setattr(app_mod, "_post_web_chat_message_to_control", fake_post)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/chat/messages",
            json={"content": "hello", "channel": "discord"},
        )

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "channel": "discord"}
    assert seen == {"text": "hello", "channel": "discord"}


@pytest.mark.asyncio
async def test_chat_channels_forwards_to_control_api(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    async def fake_fetch(_settings: WebApiSettings):
        return 200, {"channels": ["cli", "discord"]}

    monkeypatch.setattr(app_mod, "_fetch_channels_from_control", fake_fetch)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/chat/channels")

    assert resp.status_code == 200
    assert resp.json() == {"channels": ["cli", "discord"]}


@pytest.mark.asyncio
async def test_chat_message_returns_503_when_control_unavailable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    async def fake_post(_settings: WebApiSettings, _text: str, channel: str = "cli"):
        return 503, {"error": "chat-cli control API is unavailable"}

    monkeypatch.setattr(app_mod, "_post_web_chat_message_to_control", fake_post)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/messages", json={"content": "hello"})

    assert resp.status_code == 503
    assert resp.json() == {"error": "chat-cli control API is unavailable"}
