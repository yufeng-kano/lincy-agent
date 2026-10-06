from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lincy.web.api import create_router, mount_static
from lincy.web.cache import DashboardSummary
from lincy.web.settings import WebSettings
from lincy.web.state import WebState

_LIVE = {
    "active": True,
    "session_id": "s1",
    "prompt_tokens": 1234,
    "soft_limit": 128_000,
    "hard_limit": 200_000,
}


class _FakeCache:
    def __init__(self) -> None:
        self.live_soft_limit: int | None = None

    def get_live_status(self, soft_limit: int) -> dict:
        self.live_soft_limit = soft_limit
        return _LIVE

    def get_dashboard(self, date_from: date, date_to: date) -> DashboardSummary:
        return DashboardSummary(
            date_from=date_from,
            date_to=date_to,
            total_cost=1.5,
            total_turns=3,
            total_sessions=1,
            total_prompt_tokens=4000,
            read_cache_rate=0.5,
            total_cache_read=2000,
            total_cache_write=100,
            cache_hit_rate=0.9,
            write_cache_measurable=True,
            daily_costs=[{"date": date_from.isoformat(), "cost": 1.5, "turns": 3}],
            pricing_sources=[],
            pricing_stale=False,
        )


def _settings(tmp_path: Path) -> WebSettings:
    static_dir = tmp_path / "dist"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>spa</html>")
    (static_dir / "favicon.svg").write_text("<svg/>")
    (static_dir / "assets" / "app.js").write_text("console.log(1)")
    return WebSettings(
        sessions_dir=tmp_path / "sessions",
        static_dir=static_dir,
        ui_events_path=tmp_path / "ui_events" / "events.jsonl",
        soft_limit_tokens=128_000,
        pricing_url="http://pricing.invalid/prices.json",
        pricing_cache_path=tmp_path / "pricing.json",
        pricing_cache_ttl_hours=24,
    )


@pytest.fixture
def settings(tmp_path) -> WebSettings:
    return _settings(tmp_path)


def _client(settings: WebSettings, state: WebState) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(settings, state))
    mount_static(app, settings)
    return TestClient(app)


def test_live_passes_soft_limit_and_returns_cache_status(settings):
    cache = _FakeCache()
    client = _client(settings, WebState(cache=cache))

    resp = client.get("/api/live")

    assert resp.status_code == 200
    assert resp.json() == _LIVE
    assert cache.live_soft_limit == 128_000


def test_dashboard_shape(settings):
    client = _client(settings, WebState(cache=_FakeCache()))

    resp = client.get("/api/dashboard?from=2026-10-01&to=2026-10-06")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["date_from"] == "2026-10-01"
    assert payload["date_to"] == "2026-10-06"
    assert payload["total_cost"] == 1.5
    assert set(payload) == {
        "date_from",
        "date_to",
        "total_cost",
        "total_turns",
        "total_sessions",
        "total_prompt_tokens",
        "read_cache_rate",
        "total_cache_read",
        "total_cache_write",
        "cache_hit_rate",
        "write_cache_measurable",
        "daily_costs",
        "pricing_sources",
        "pricing_stale",
    }


def test_cache_routes_return_503_until_cache_is_loaded(settings):
    client = _client(settings, WebState())

    assert client.get("/api/live").status_code == 503
    assert client.get("/api/dashboard").status_code == 503


def test_spa_fallback_serves_index_html_for_unknown_paths(settings):
    client = _client(settings, WebState(cache=_FakeCache()))

    resp = client.get("/monitor/sessions/abc")

    assert resp.status_code == 200
    assert resp.text == "<html>spa</html>"


def test_spa_serves_public_files_and_assets(settings):
    client = _client(settings, WebState(cache=_FakeCache()))

    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_spa_fallback_does_not_serve_files_outside_static_dir(settings, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    client = _client(settings, WebState(cache=_FakeCache()))

    resp = client.get("/%2e%2e/secret.txt")

    assert resp.status_code == 200
    assert resp.text == "<html>spa</html>"


def test_removed_routes_fall_through_to_spa(settings):
    client = _client(settings, WebState(cache=_FakeCache()))

    # /health and /api/chat/* are gone; only the SPA fallback answers GETs now.
    assert client.get("/health").text == "<html>spa</html>"
    assert client.post("/api/chat/messages", json={"content": "hi"}).status_code == 405
