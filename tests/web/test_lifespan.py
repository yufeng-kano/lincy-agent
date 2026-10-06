from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI

import lincy.web.lifespan as lifespan_mod
from lincy.web.lifespan import web_lifespan
from lincy.web.settings import WebSettings
from lincy.web.state import WebState


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        sessions_dir=tmp_path / "sessions",
        static_dir=tmp_path / "dist",
        ui_events_path=tmp_path / "ui_events" / "events.jsonl",
        soft_limit_tokens=128_000,
        pricing_url="http://pricing.invalid/prices.json",
        pricing_cache_path=tmp_path / "pricing.json",
        pricing_cache_ttl_hours=24,
    )


@pytest.fixture
def started_watchers(monkeypatch) -> list[str]:
    started: list[str] = []

    async def fake_watch_sessions(_dir, _cache, _broadcast, stop_event, *, soft_limit):
        started.append("sessions")
        await stop_event.wait()

    async def fake_watch_ui_events(_path, _broadcast, stop_event):
        started.append("ui_events")
        await stop_event.wait()

    monkeypatch.setattr(lifespan_mod, "watch_sessions", fake_watch_sessions)
    monkeypatch.setattr(lifespan_mod, "watch_ui_events", fake_watch_ui_events)
    return started


async def _wait_until_settled(app: FastAPI) -> None:
    for _ in range(200):
        if app.state.web_status != "loading":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("web_status stayed loading")


@pytest.mark.asyncio
async def test_status_goes_loading_then_ready(tmp_path, monkeypatch, started_watchers):
    release = asyncio.Event()

    async def fake_fetch_pricing(_url, _cache_path, _ttl):
        await release.wait()
        return {}

    monkeypatch.setattr(lifespan_mod, "fetch_pricing", fake_fetch_pricing)
    app = FastAPI()
    state = WebState()

    async with web_lifespan(app, _settings(tmp_path), state):
        # Initialization runs in the background; the server is already serving.
        await asyncio.sleep(0)
        assert app.state.web_status == "loading"
        assert state.cache is None

        release.set()
        await _wait_until_settled(app)

        assert app.state.web_status == "ready"
        assert state.cache is not None
        await asyncio.sleep(0)
        assert sorted(started_watchers) == ["sessions", "ui_events"]


@pytest.mark.asyncio
async def test_pricing_failure_marks_unavailable_without_raising(
    tmp_path, monkeypatch, started_watchers
):
    async def failing_fetch_pricing(_url, _cache_path, _ttl):
        raise RuntimeError("pricing source down")

    monkeypatch.setattr(lifespan_mod, "fetch_pricing", failing_fetch_pricing)
    app = FastAPI()
    state = WebState()

    async with web_lifespan(app, _settings(tmp_path), state):
        await _wait_until_settled(app)

        assert app.state.web_status == "unavailable"
        assert state.cache is None
        assert started_watchers == []


@pytest.mark.asyncio
async def test_exit_while_loading_cancels_initialization(tmp_path, monkeypatch, started_watchers):
    async def hanging_fetch_pricing(_url, _cache_path, _ttl):
        await asyncio.Event().wait()

    monkeypatch.setattr(lifespan_mod, "fetch_pricing", hanging_fetch_pricing)
    app = FastAPI()
    state = WebState()

    async with web_lifespan(app, _settings(tmp_path), state):
        await asyncio.sleep(0)

    assert app.state.web_status == "loading"
    assert state.cache is None
    assert started_watchers == []
