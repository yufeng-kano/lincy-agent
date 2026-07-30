from __future__ import annotations

import asyncio

import httpx
import pytest

import chat_web_api.watcher as watcher_mod
from lincy.agent.ui_event_stream import UiEventStore, serialize_ui_event
from lincy.tui.events import ToolCallEvent, WarningEvent
from chat_web_api.app import create_app
from chat_web_api.settings import WebApiSettings


def _settings(tmp_path) -> WebApiSettings:
    return WebApiSettings(
        sessions_dir=tmp_path / "sessions",
        web_chat_events_path=tmp_path / "web_chat" / "events.jsonl",
        ui_events_path=tmp_path / "ui_events" / "events.jsonl",
        pricing_cache_path=tmp_path / "pricing.json",
    )


@pytest.mark.asyncio
async def test_agent_events_returns_recent_records(tmp_path):
    settings = _settings(tmp_path)
    store = UiEventStore(settings.ui_events_path)
    store.append(serialize_ui_event(WarningEvent(message="first"), seq=1))
    second = store.append(
        serialize_ui_event(ToolCallEvent(name="worker-3 execute_shell", summary="ls"), seq=2)
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agent/events?limit=1")

    assert resp.status_code == 200
    payload = resp.json()["events"]
    assert payload == [second.model_dump(mode="json")]
    assert payload[0]["agent"] == "worker-3"
    assert payload[0]["data"]["name"] == "execute_shell"


@pytest.mark.asyncio
async def test_agent_events_returns_empty_when_no_file(tmp_path):
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agent/events")

    assert resp.status_code == 200
    assert resp.json() == {"events": []}


@pytest.mark.asyncio
async def test_agent_events_rejects_out_of_range_limit(tmp_path):
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/agent/events?limit=5000")

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_watch_ui_events_broadcasts_appended_records(tmp_path, monkeypatch):
    events_path = tmp_path / "ui_events" / "events.jsonl"
    store = UiEventStore(events_path)
    appended: list = []

    async def fake_awatch(_watched_dir, *, stop_event=None):
        # Append after the watcher captured its start offset, then report the change.
        appended.append(store.append(serialize_ui_event(WarningEvent(message="hi"), seq=1)))
        yield {("modified", str(events_path))}

    monkeypatch.setattr(watcher_mod, "awatch", fake_awatch)
    sent: list[dict] = []

    async def broadcast(message: dict) -> None:
        sent.append(message)

    await watcher_mod.watch_ui_events(events_path, broadcast, asyncio.Event())

    assert sent == [
        {"type": "agent_event", "event": appended[0].model_dump(mode="json")}
    ]


@pytest.mark.asyncio
async def test_watch_ui_events_ignores_other_files(tmp_path, monkeypatch):
    events_path = tmp_path / "ui_events" / "events.jsonl"
    store = UiEventStore(events_path)

    async def fake_awatch(_watched_dir, *, stop_event=None):
        store.append(serialize_ui_event(WarningEvent(message="hi"), seq=1))
        yield {("modified", str(events_path.parent / "events.prev.jsonl"))}

    monkeypatch.setattr(watcher_mod, "awatch", fake_awatch)
    sent: list[dict] = []

    async def broadcast(message: dict) -> None:
        sent.append(message)

    await watcher_mod.watch_ui_events(events_path, broadcast, asyncio.Event())

    assert sent == []
