from __future__ import annotations

from datetime import date

import pytest

from lincy.web.pricing import (
    ModelPricing,
    builtin_pricing_overrides,
    compute_request_cost,
    get_pricing_metadata,
    resolve_model_key,
)


def test_deepseek_v4_pro_uses_local_original_pricing_override():
    pricing = builtin_pricing_overrides()

    cost = compute_request_cost(
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt_tokens=1000,
        completion_tokens=100,
        cache_read_tokens=200,
        cache_write_tokens=0,
        pricing=pricing,
    )

    assert cost == pytest.approx(
        800 * (1.74 / 1_000_000)
        + 200 * (0.0145 / 1_000_000)
        + 100 * (3.48 / 1_000_000)
    )


def test_deepseek_v4_pro_pricing_metadata_reports_local_override():
    pricing = builtin_pricing_overrides()

    meta = get_pricing_metadata(
        "deepseek",
        "deepseek-v4-pro",
        pricing,
        today=date(2026, 5, 12),
    )

    assert meta is not None
    assert meta.model_key == "deepseek/deepseek-v4-pro"
    assert meta.source == "local_override"
    assert meta.stale is False


def test_deepseek_v4_pro_pricing_metadata_becomes_stale_after_review_date():
    pricing = builtin_pricing_overrides()

    meta = get_pricing_metadata(
        "deepseek",
        "deepseek-v4-pro",
        pricing,
        today=date(2026, 6, 13),
    )

    assert meta is not None
    assert meta.stale is True


_RATE = ModelPricing(input_cost_per_token=1e-06, output_cost_per_token=2e-06)


@pytest.mark.parametrize(
    ("served_upstream", "table_keys", "expected"),
    [
        ("claude-code/claude-opus-5-5", ["claude-opus-5-5"], "claude-opus-5-5"),
        ("ant-official/claude-haiku-5-5", ["claude-haiku-5-5"], "claude-haiku-5-5"),
        ("claude-code/claude-opus-5-5[1m]", ["claude-opus-5-5"], "claude-opus-5-5"),
        ("antigravity/gemini-3.8-flash-high", ["gemini-3.8-flash"], "gemini-3.8-flash"),
        ("antigravity/gemini-4-argon-medium", ["gemini/gemini-4-argon"], "gemini/gemini-4-argon"),
        ("antigravity/gemini-3.8-flash-thinking-low", ["gemini-3.8-flash"], "gemini-3.8-flash"),
        ("antigravity/gemini-4-argon", ["gemini-4-argon-preview"], "gemini-4-argon-preview"),
        ("codex/gpt-6-sol", ["openai/gpt-6-sol"], "openai/gpt-6-sol"),
        ("mygw/vendor/claude-sonnet-5-5", ["claude-sonnet-5-5"], "claude-sonnet-5-5"),
        # The effort fold is Gemini-only: a non-Gemini `-high` is never priced as its base.
        ("codex/gpt-6-sol-high", ["gpt-6-sol"], None),
        ("openrouter/x-ai/grok-4.5", ["openrouter/x-ai/grok-4.5"], "openrouter/x-ai/grok-4.5"),
        ("openrouter/x-ai/grok-4.5", ["grok-4.5"], None),
    ],
)
def test_served_upstream_resolves_like_kano_proxy(served_upstream, table_keys, expected):
    pricing = {key: _RATE for key in table_keys}

    assert resolve_model_key("kano_proxy", "brain-agent", pricing, served_upstream=served_upstream) == expected


def test_unpriced_upstream_does_not_fall_back_to_the_profile_model():
    pricing = {"brain-agent": _RATE}

    assert resolve_model_key("kano_proxy", "brain-agent", pricing, served_upstream="claude-code/claude-opus-9") is None


def test_profile_model_prices_records_without_an_upstream():
    pricing = builtin_pricing_overrides()

    assert resolve_model_key("deepseek", "deepseek-v4-pro", pricing) == "deepseek/deepseek-v4-pro"
    assert resolve_model_key("kano_proxy", "brain-agent", pricing) is None


def test_request_cost_uses_the_upstream_rate():
    cost = compute_request_cost(
        provider="kano_proxy",
        model="brain-agent",
        prompt_tokens=1000,
        completion_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        pricing={"claude-opus-5-5": _RATE},
        served_upstream="claude-code/claude-opus-5-5",
    )

    assert cost == pytest.approx(1000 * 1e-06 + 100 * 2e-06)
