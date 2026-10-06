from __future__ import annotations

from datetime import date

import pytest

from lincy.web.pricing import (
    builtin_pricing_overrides,
    compute_request_cost,
    get_pricing_metadata,
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
