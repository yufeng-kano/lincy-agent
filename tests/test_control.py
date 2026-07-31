"""Tests for lincy.control (ControlServer FastAPI app)."""

import pytest
import httpx

from lincy import control
from lincy.control import create_app


@pytest.fixture
def app():
    return create_app(shutdown_fn=lambda: None)


@pytest.fixture
def transport(app):
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_health_returns_ok(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_shutdown_calls_fn():
    called = []
    app = create_app(shutdown_fn=lambda: called.append(True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/shutdown")
    assert resp.status_code == 200
    assert resp.json() == {"status": "shutting_down"}
    assert called == [True]


@pytest.mark.asyncio
async def test_shutdown_idempotent():
    count = []
    app = create_app(shutdown_fn=lambda: count.append(1))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/shutdown")
        await client.post("/shutdown")
    assert len(count) == 2


@pytest.mark.asyncio
async def test_new_session_calls_fn():
    called = []
    app = create_app(
        shutdown_fn=lambda: None,
        new_session_fn=lambda: called.append(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/session/new")
    assert resp.status_code == 200
    assert resp.json() == {"status": "new_session_requested"}
    assert called == [True]


@pytest.mark.asyncio
async def test_reload_calls_fn():
    called = []
    app = create_app(
        shutdown_fn=lambda: None,
        reload_fn=lambda: called.append(True),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/reload")
    assert resp.status_code == 200
    assert resp.json() == {"status": "reload_requested"}
    assert called == [True]


@pytest.mark.asyncio
async def test_new_session_returns_404_when_unavailable():
    app = create_app(shutdown_fn=lambda: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/session/new")
    assert resp.status_code == 404
    assert resp.json() == {"error": "new-session is not supported"}


@pytest.mark.asyncio
async def test_reload_returns_404_when_unavailable():
    app = create_app(shutdown_fn=lambda: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/reload")
    assert resp.status_code == 404
    assert resp.json() == {"error": "reload is not supported"}


@pytest.mark.asyncio
async def test_web_chat_message_calls_submit_fn():
    called = []

    def submit(content: str, channel: str) -> dict:
        called.append((content, channel))
        return {"status": "accepted", "channel": channel}

    app = create_app(shutdown_fn=lambda: None, tui_submit_fn=submit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/web-chat/messages", json={"content": " hello "})

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "channel": "cli"}
    assert called == [("hello", "cli")]


@pytest.mark.asyncio
async def test_web_chat_message_forwards_selected_channel():
    called = []

    def submit(content: str, channel: str) -> dict:
        called.append((content, channel))
        return {"status": "accepted", "channel": channel}

    app = create_app(shutdown_fn=lambda: None, tui_submit_fn=submit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/web-chat/messages",
            json={"content": "hi", "channel": "discord"},
        )

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "channel": "discord"}
    assert called == [("hi", "discord")]


@pytest.mark.asyncio
async def test_web_chat_message_rejects_blank_content():
    app = create_app(shutdown_fn=lambda: None, tui_submit_fn=lambda _c, _ch: {})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/web-chat/messages", json={"content": "   "})

    assert resp.status_code == 400
    assert resp.json() == {"error": "content is required"}


@pytest.mark.asyncio
async def test_web_chat_message_returns_503_when_unavailable():
    app = create_app(shutdown_fn=lambda: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/web-chat/messages", json={"content": "hello"})

    assert resp.status_code == 503
    assert resp.json() == {"error": "remote TUI submit is not available"}


@pytest.mark.asyncio
async def test_web_chat_message_returns_409_when_busy():
    def submit(_content: str, _channel: str) -> dict:
        raise RuntimeError("Still processing the previous turn.")

    app = create_app(shutdown_fn=lambda: None, tui_submit_fn=submit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/web-chat/messages", json={"content": "hello"})

    assert resp.status_code == 409
    assert resp.json() == {"error": "Still processing the previous turn."}


@pytest.mark.asyncio
async def test_channels_lists_send_options():
    app = create_app(
        shutdown_fn=lambda: None,
        channels_fn=lambda: ["cli", "discord", "gmail"],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/channels")

    assert resp.status_code == 200
    assert resp.json() == {"channels": ["cli", "discord", "gmail"]}


def test_assert_control_slot_available_detects_existing_chat_cli(monkeypatch):
    monkeypatch.setattr(control, "_port_is_available", lambda _h, _p: False)
    monkeypatch.setattr(control, "_looks_like_control_api", lambda _h, _p: True)

    with pytest.raises(RuntimeError, match="another chat-cli instance is likely active"):
        control._assert_control_slot_available("127.0.0.1", 9001)


def test_assert_control_slot_available_detects_generic_port_conflict(monkeypatch):
    monkeypatch.setattr(control, "_port_is_available", lambda _h, _p: False)
    monkeypatch.setattr(control, "_looks_like_control_api", lambda _h, _p: False)

    with pytest.raises(RuntimeError, match="already in use"):
        control._assert_control_slot_available("127.0.0.1", 9001)
