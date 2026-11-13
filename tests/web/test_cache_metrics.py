from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lincy.session.schema import SessionMetadata
from lincy.web.cache import MetricsCache, ResponseMetrics, TurnMetrics
from lincy.web.pricing import ModelPricing
from lincy.web.session_reader import SessionFiles


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


def test_session_summary_uses_read_cache_rate_and_marks_openai_write_unmeasurable():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        ResponseMetrics(
            ts=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
            round=1,
            provider="openai",
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
            provider="kano_proxy",
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
            provider="kano_proxy",
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
            pricing_model_key="brain-model",
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
            pricing_model_key="worker-model",
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
    """Multi-day sessions must still surface recent requests."""
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
            provider="kano_proxy",
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
            provider="openrouter",
            model="x-ai/grok-4.5",
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
    assert today_only[0]["provider"] == "openrouter"
    assert today_only[0]["model"] == "x-ai/grok-4.5"
    assert today_only[0]["read_cache_rate"] == 0.75

    # Newest first: openrouter before the older kano_proxy call.
    week = cache.get_all_requests(date(2026, 7, 5), date(2026, 7, 11))
    assert [r["provider"] for r in week] == ["openrouter", "kano_proxy"]

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
        model="brain-agent",
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
    assert rows[0]["model"] == "brain-agent"
    assert rows[0]["served_provider"] == "heyroute"
    assert rows[0]["served_model"] == "deepseek-v3"
    assert rows[0]["served_by_fallback"] is True


def test_all_requests_marks_primary_served_rows_as_not_fallback():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        _response(
            served_provider="kano_proxy",
            served_model="brain-agent",
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


def test_dashboard_reports_brain_only_read_cache_rate():
    """Workers run on separate contexts, so the brain rate must not be diluted."""
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [
        _response(client_label="brain", prompt_tokens=1000, cache_read_tokens=900),
        _response(client_label="brain", prompt_tokens=1000, cache_read_tokens=1000),
        _response(
            client_label="worker-3",
            model="worker-agent",
            prompt_tokens=2000,
            cache_read_tokens=0,
        ),
    ]
    cache._turns["s1"] = []

    dash = cache.get_dashboard(date(2026, 4, 11), date(2026, 4, 11))

    assert dash.read_cache_rate == 1900 / 4000
    assert dash.brain_read_cache_rate == 0.95
    assert dash.daily_costs[0]["brain_read_cache_rate"] == 0.95
    assert dash.daily_costs[0]["brain_prompt_tokens"] == 2000


def test_dashboard_brain_read_cache_rate_is_none_without_brain_rows():
    cache = MetricsCache(Path("/tmp"), {})
    cache._files["s1"] = SessionFiles(session_dir=Path("/tmp/s1"), meta=_meta("s1"))
    cache._responses["s1"] = [_response(client_label="worker-3", model="worker-agent")]
    cache._turns["s1"] = []

    dash = cache.get_dashboard(date(2026, 4, 11), date(2026, 4, 11))

    assert dash.brain_read_cache_rate is None
    assert dash.daily_costs[0]["brain_read_cache_rate"] is None


_OPUS = ModelPricing(
    input_cost_per_token=4e-06,
    output_cost_per_token=2e-05,
    cache_read_input_token_cost=2e-07,
    cache_creation_input_token_cost=5e-06,
    max_input_tokens=1_000_000,
)
_DEEPSEEK = ModelPricing(input_cost_per_token=1e-06, output_cost_per_token=2e-06)


def _response_line(seq: int, **overrides) -> str:
    record = {
        "seq": seq,
        "ts": "2026-11-12T16:07:04+08:00",
        "session_id": "20261112_030705_5012ba",
        "turn_id": "turn_000001",
        "request_id": f"req_{seq:06d}",
        "round": seq,
        "client_label": "brain",
        "provider": "kano_proxy",
        "model": "brain-agent",
        "call_type": "chat_with_tools",
        "latency_ms": 100,
        "response": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "usage_available": True,
            "cache_read_tokens": 600,
            "cache_write_tokens": 200,
            "served_upstream": "claude-code/claude-opus-5-5",
        },
    }
    record.update(overrides)
    return json.dumps(record)


def _ingest(tmp_path: Path, pricing: dict[str, ModelPricing], *lines: str) -> list[ResponseMetrics]:
    session_id = "20261112_030705_5012ba"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "responses.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cache = MetricsCache(tmp_path, pricing)
    cache.refresh_session(session_id)
    return cache._responses[session_id]


def test_gateway_alias_is_priced_at_the_upstream_it_named(tmp_path):
    [row] = _ingest(tmp_path, {"claude-opus-5-5": _OPUS}, _response_line(1))

    assert row.pricing_model_key == "claude-opus-5-5"
    assert row.cost == pytest.approx(200 * 4e-06 + 600 * 2e-07 + 200 * 5e-06 + 100 * 2e-05)


def test_plain_chat_record_with_reported_usage_is_priced(tmp_path):
    line = _response_line(
        1,
        client_label="compactor",
        model="compactor-agent",
        call_type="chat",
        response_text="summary",
    )
    [row] = _ingest(tmp_path, {"claude-opus-5-5": _OPUS}, line)

    assert row.cost is not None
    assert row.client_label == "compactor"


def test_fallback_candidate_prices_the_call_it_served(tmp_path):
    line = _response_line(
        1,
        served_provider="deepseek",
        served_model="deepseek-v4-pro",
        served_candidate_index=1,
        response={"prompt_tokens": 1000, "completion_tokens": 100, "usage_available": True},
    )
    [row] = _ingest(tmp_path, {"deepseek/deepseek-v4-pro": _DEEPSEEK}, line)

    assert row.pricing_model_key == "deepseek/deepseek-v4-pro"
    assert row.cost == pytest.approx(1000 * 1e-06 + 100 * 2e-06)


def test_live_hard_limit_follows_the_upstream_model(tmp_path):
    cache = MetricsCache(tmp_path, {"claude-opus-5-5": _OPUS})
    session_dir = tmp_path / "20261112_030705_5012ba"
    session_dir.mkdir()
    (session_dir / "responses.jsonl").write_text(_response_line(1) + "\n", encoding="utf-8")
    cache.refresh_session("20261112_030705_5012ba")
    cache._files["20261112_030705_5012ba"].meta = _meta("20261112_030705_5012ba")

    status = cache.get_live_status(400_000)

    assert status is not None
    assert status["hard_limit"] == 1_000_000


def test_turn_read_cache_rate_counts_plain_chat_rows_on_both_sides(tmp_path):
    session_id = "20261112_030705_5012ba"
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    brain = _response_line(1, response={
        "prompt_tokens": 150_000, "completion_tokens": 100, "usage_available": True,
        "cache_read_tokens": 140_000, "served_upstream": "claude-code/claude-opus-5-5",
    })
    compactor = _response_line(2, client_label="compactor", model="compactor-agent", call_type="chat",
        response_text="summary", response={
            "prompt_tokens": 150_000, "completion_tokens": 100, "usage_available": True,
            "cache_read_tokens": 120_000, "served_upstream": "claude-code/claude-opus-5-5",
        })
    (session_dir / "responses.jsonl").write_text(brain + "\n" + compactor + "\n", encoding="utf-8")
    # turns.jsonl only aggregates the brain call's cache reads.
    turn = {
        "turn_id": "turn_000001", "ts_started": "2026-11-12T16:06:00+08:00",
        "ts_finished": "2026-11-12T16:08:00+08:00", "session_id": session_id,
        "channel": "discord", "inbound_kind": "user_message", "input_text": "hi",
        "status": "completed", "llm_rounds": 1, "usage_available": True,
        "cache_read_tokens": 140_000,
    }
    (session_dir / "turns.jsonl").write_text(json.dumps(turn) + "\n", encoding="utf-8")
    cache = MetricsCache(tmp_path, {"claude-opus-5-5": _OPUS})

    cache.refresh_session(session_id)

    [tm] = cache._turns[session_id]
    assert tm.read_cache_rate == pytest.approx(260_000 / 300_000)
    # The turn row's cache column covers the same rows as its prompt total.
    assert tm.cache_read_tokens == 260_000


def test_session_peak_prompt_ignores_plain_chat_rows(tmp_path):
    session_id = "20261112_030705_5012ba"
    brain = _response_line(1, response={
        "prompt_tokens": 120_000, "completion_tokens": 100, "usage_available": True,
        "served_upstream": "claude-code/claude-opus-5-5",
    })
    compactor = _response_line(2, client_label="compactor", model="compactor-agent", call_type="chat",
        response_text="summary", response={
            "prompt_tokens": 180_000, "completion_tokens": 100, "usage_available": True,
            "served_upstream": "claude-code/claude-opus-5-5",
        })
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "responses.jsonl").write_text(brain + "\n" + compactor + "\n", encoding="utf-8")
    cache = MetricsCache(tmp_path, {"claude-opus-5-5": _OPUS})
    cache.refresh_session(session_id)
    cache._files[session_id].meta = _meta(session_id)

    summary = cache.get_session_summary(session_id)

    assert summary is not None
    assert summary.peak_prompt_tokens == 120_000
    assert summary.total_prompt_tokens == 300_000
