from __future__ import annotations

import asyncio

import pytest

import lincy.web.watcher as watcher_mod
from lincy.agent.ui_event_stream import UiEventStore, serialize_ui_event
from lincy.ui.events import WarningEvent


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
