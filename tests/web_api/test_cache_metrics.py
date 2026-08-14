from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from lincy.session.schema import SessionMetadata
from chat_web_api.cache import MetricsCache, ResponseMetrics, TurnMetrics
from chat_web_api.pricing import ModelPricing
from chat_web_api.session_reader import SessionFiles


def _meta(session_id: str) -> SessionMetadata:
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    return SessionMetadata(
        session_id=session_id,
        user_id="u1",
        display_name="User",
        created_at=now,
        updated_at=now,
        status="active",
    )


def test_session_summary_uses_read_cache_rate_and_marks_codex_write_unmeasurable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="codex",
            model="gpt-5.4",
            prompt_tokens=1000,
            completion_tokens=100,
            cache_read_tokens=900,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]
    cache._turns["s1"] = []

    summary = cache.get_session_summary("s1")

    assert summary is not None
    assert summary.read_cache_rate == 0.9
    assert summary.write_cache_measurable is False


def test_all_requests_reports_openrouter_write_cache_as_measurable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="openrouter",
            model="claude-sonnet-4.5",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=128,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["read_cache_rate"] == 0.5
    assert rows[0]["write_cache_measurable"] is True


def test_all_requests_reports_pricing_source_status():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=0,
            latency_ms=100,
            cost=0.001,
            turn_id="turn_000001",
            pricing_source="local_override",
            pricing_source_url="https://api-docs.deepseek.com/quick_start/pricing",
            pricing_stale=False,
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))
    summary = cache.get_session_summary("s1")

    assert rows[0]["pricing_source"] == "local_override"
    assert rows[0]["pricing_stale"] is False
    assert summary is not None
    assert summary.pricing_sources == [
        {
            "source": "local_override",
            "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "stale": False,
            "count": 1,
        }
    ]


def test_ollama_session_summary_marks_read_cache_rate_unavailable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="ollama",
            model="kimi-k2.6:cloud",
            prompt_tokens=1000,
            completion_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]
    cache._turns["s1"] = []

    summary = cache.get_session_summary("s1")

    assert summary is not None
    assert summary.read_cache_rate is None


def test_all_requests_reports_ollama_read_cache_rate_unavailable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="ollama",
            model="kimi-k2.6:cloud",
            prompt_tokens=2000,
            completion_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=100,
            cost=None,
            turn_id="turn_000001",
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["read_cache_rate"] is None


def test_live_status_uses_current_turn_brain_max_without_turn_record():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="claude_code",
            model="claude-opus-5",
            prompt_tokens=100,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=None,
            turn_id="turn_000002",
            client_label="brain",
        ),
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 1, tzinfo=UTC),
            round=2,
            provider="claude_code",
            model="claude-opus-5",
            prompt_tokens=200,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=None,
            turn_id="turn_000002",
            client_label="brain",
        ),
    ]

    status = cache.get_live_status(400_000)

    assert status is not None
    assert status["prompt_tokens"] == 200


def test_live_status_ignores_worker_responses_for_prompt_and_hard_limit():
    brain_pricing = ModelPricing(0.0, 0.0, max_input_tokens=400_000)
    worker_pricing = ModelPricing(0.0, 0.0, max_input_tokens=1_000_000)
    cache = MetricsCache(Path("/tmp"), {
        "brain-model": brain_pricing,
        "worker-model": worker_pricing,
    })
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider=None,
            model="brain-model",
            prompt_tokens=150,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=None,
            turn_id="turn_000002",
            client_label="brain",
        ),
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 1, tzinfo=UTC),
            round=1,
            provider=None,
            model="worker-model",
            prompt_tokens=999_999,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=None,
            turn_id="turn_000002",
            client_label="worker-1",
        ),
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 2, tzinfo=UTC),
            round=1,
            provider=None,
            model="worker-model",
            prompt_tokens=888_888,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=None,
            turn_id="turn_000003",
            client_label="web_fetch_summarizer",
        ),
    ]

    status = cache.get_live_status(400_000)

    assert status is not None
    assert status["prompt_tokens"] == 150
    assert status["hard_limit"] == 400_000


def test_all_requests_filters_by_response_ts_not_session_created():
    """Multi-day sessions must still surface recent requests (e.g. grok)."""
    cache = MetricsCache(Path("/tmp"), {})
    created = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)
    cache._files["s1"] = SessionFiles(
        session_dir=Path("/tmp/s1"),
        meta=SessionMetadata(
            session_id="s1",
            user_id="u1",
            display_name="User",
            created_at=created,
            updated_at=datetime(2026, 7, 11, 1, 0, tzinfo=UTC),
            status="active",
        ),
    )
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 7, 10, 6, 0, tzinfo=UTC),
            round=1,
            provider="claude_code",
            model="claude-opus-4-8",
            prompt_tokens=100,
            completion_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=10,
            cost=0.1,
            turn_id="turn_000001",
        ),
        ResponseMetrics(
            ts=datetime(2026, 7, 11, 1, 5, tzinfo=UTC),
            round=1,
            provider="grok",
            model="grok-4.5",
            prompt_tokens=200,
            completion_tokens=20,
            cache_read_tokens=150,
            cache_write_tokens=0,
            latency_ms=20,
            cost=0.2,
            turn_id="turn_000002",
        ),
    ]

    today_only = cache.get_all_requests(date(2026, 7, 11), date(2026, 7, 11))
    assert len(today_only) == 1
    assert today_only[0]["provider"] == "grok"
    assert today_only[0]["model"] == "grok-4.5"
    assert today_only[0]["read_cache_rate"] == 0.75

    # Newest first: grok before older claude call.
    week = cache.get_all_requests(date(2026, 7, 5), date(2026, 7, 11))
    assert [r["provider"] for r in week] == ["grok", "claude_code"]

    sessions = cache.get_sessions_in_range(date(2026, 7, 11), date(2026, 7, 11))
    assert len(sessions) == 1
    assert sessions[0].session_id == "s1"

    dash = cache.get_dashboard(date(2026, 7, 11), date(2026, 7, 11))
    assert dash.total_sessions == 1
    assert dash.total_turns == 0  # no turns recorded in fixture
    assert dash.total_prompt_tokens == 200
    assert dash.total_cost == 0.2
    assert dash.daily_costs[0]["date"] == "2026-07-11"
    assert dash.daily_costs[0]["prompt_tokens"] == 200


def _response(**overrides) -> ResponseMetrics:
    base = dict(
        ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
        round=1,
        provider="kano_proxy",
        model="lincy-brain-agent",
        prompt_tokens=1000,
        completion_tokens=100,
        cache_read_tokens=500,
        cache_write_tokens=0,
        latency_ms=100,
        cost=None,
        turn_id="turn_000001",
    )
    base.update(overrides)
    return ResponseMetrics(**base)


def test_all_requests_exposes_the_serving_fallback():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        _response(
            served_provider="heyroute",
            served_model="deepseek-v3",
            served_candidate_index=1,
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    # The intended profile is unchanged; the served fields are additive.
    assert rows[0]["provider"] == "kano_proxy"
    assert rows[0]["model"] == "lincy-brain-agent"
    assert rows[0]["served_provider"] == "heyroute"
    assert rows[0]["served_model"] == "deepseek-v3"
    assert rows[0]["served_by_fallback"] is True


def test_all_requests_marks_primary_served_rows_as_not_fallback():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        _response(
            served_provider="kano_proxy",
            served_model="lincy-brain-agent",
            served_candidate_index=0,
        )
    ]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["served_by_fallback"] is False


def test_all_requests_reports_unknown_serving_provider_for_old_sessions():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [_response()]

    rows = cache.get_all_requests(date(2026, 4, 11), date(2026, 4, 11))

    assert rows[0]["served_provider"] is None
    assert rows[0]["served_model"] is None
    assert rows[0]["served_by_fallback"] is None


def test_session_detail_responses_carry_served_fields():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        _response(
            served_provider="heyroute",
            served_model="deepseek-v3",
            served_candidate_index=1,
        )
    ]
    cache._turns["s1"] = [
        TurnMetrics(
            turn_id="turn_000001",
            ts_started=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            ts_finished=datetime(2026, 4, 11, 12, 1, tzinfo=UTC),
            channel="cli",
            sender="alice",
            status="completed",
            llm_rounds=1,
            max_prompt_tokens=1000,
            total_prompt_tokens=1000,
            read_cache_rate=None,
            cache_read_tokens=500,
            cache_write_tokens=0,
            write_cache_measurable=True,
            total_cost=None,
            responses=cache._responses["s1"],
        )
    ]

    detail = cache.get_session_detail("s1")

    assert detail is not None
    response = detail["turns"][0]["responses"][0]
    assert response["served_provider"] == "heyroute"
    assert response["served_by_fallback"] is True
