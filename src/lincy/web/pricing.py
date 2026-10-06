"""Model pricing: fetch from LiteLLM, cache locally, compute request costs."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_input_token_cost: float | None = None
    cache_creation_input_token_cost: float | None = None
    max_input_tokens: int | None = None
    pricing_source: str = "litellm"
    pricing_source_url: str | None = None
    pricing_stale: bool = False
    verified_at: date | None = None
    stale_after: date | None = None

    def is_stale(self, today: date | None = None) -> bool:
        if self.pricing_stale:
            return True
        if self.stale_after is None:
            return False
        return (today or date.today()) > self.stale_after


@dataclass(frozen=True)
class PricingMetadata:
    model_key: str
    source: str
    source_url: str | None
    stale: bool


_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
_DEEPSEEK_VERIFIED_AT = date(2026, 5, 12)
_DEEPSEEK_STALE_AFTER = date(2026, 6, 12)


def _per_token(per_million_tokens: float) -> float:
    return per_million_tokens / 1_000_000


def builtin_pricing_overrides() -> dict[str, ModelPricing]:
    """Return project-maintained pricing entries not yet reliable upstream."""
    return {
        "deepseek/deepseek-v4-flash": ModelPricing(
            input_cost_per_token=_per_token(0.14),
            output_cost_per_token=_per_token(0.28),
            cache_read_input_token_cost=_per_token(0.0028),
            max_input_tokens=1_000_000,
            pricing_source="local_override",
            pricing_source_url=_DEEPSEEK_PRICING_URL,
            verified_at=_DEEPSEEK_VERIFIED_AT,
            stale_after=_DEEPSEEK_STALE_AFTER,
        ),
        "deepseek/deepseek-v4-pro": ModelPricing(
            input_cost_per_token=_per_token(1.74),
            output_cost_per_token=_per_token(3.48),
            cache_read_input_token_cost=_per_token(0.0145),
            max_input_tokens=1_000_000,
            pricing_source="local_override",
            pricing_source_url=_DEEPSEEK_PRICING_URL,
            verified_at=_DEEPSEEK_VERIFIED_AT,
            stale_after=_DEEPSEEK_STALE_AFTER,
        ),
    }


def _apply_builtin_overrides(pricing: dict[str, ModelPricing]) -> dict[str, ModelPricing]:
    merged = dict(pricing)
    for key, override in builtin_pricing_overrides().items():
        if key not in merged:
            merged[key] = override
    return merged


def _parse_litellm_json(
    raw: dict,
    *,
    source: str = "litellm",
    source_url: str | None = _LITELLM_PRICING_URL,
    stale: bool = False,
) -> dict[str, ModelPricing]:
    """Parse LiteLLM model_prices JSON into a lookup dict."""
    result: dict[str, ModelPricing] = {}
    for key, info in raw.items():
        if not isinstance(info, dict):
            continue
        inp = info.get("input_cost_per_token")
        out = info.get("output_cost_per_token")
        if inp is None or out is None:
            continue
        result[key] = ModelPricing(
            input_cost_per_token=float(inp),
            output_cost_per_token=float(out),
            cache_read_input_token_cost=_opt_float(info.get("cache_read_input_token_cost")),
            cache_creation_input_token_cost=_opt_float(
                info.get("cache_creation_input_token_cost")
            ),
            max_input_tokens=_opt_int(info.get("max_input_tokens")),
            pricing_source=source,
            pricing_source_url=source_url,
            pricing_stale=stale,
        )
    return result


def _opt_float(v: float | str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _opt_int(v: int | str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


async def fetch_pricing(
    url: str,
    cache_path: Path,
    ttl_hours: int = 24,
) -> dict[str, ModelPricing]:
    """Load pricing from local cache or fetch from remote."""
    # Try local cache first
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            fetched_at = cached.get("_fetched_at", 0)
            if time.time() - fetched_at < ttl_hours * 3600:
                logger.info("Pricing loaded from cache (%s)", cache_path)
                return _apply_builtin_overrides(
                    _parse_litellm_json(
                        cached.get("data", {}),
                        source="litellm_cache",
                        source_url=url,
                    )
                )
        except Exception:
            logger.warning("Failed to read pricing cache, will fetch fresh")

    # Fetch from remote
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.json()
    except Exception:
        logger.warning("Failed to fetch pricing from %s", url, exc_info=True)
        # Fall back to stale cache if available
        if cache_path.exists():
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            return _apply_builtin_overrides(
                _parse_litellm_json(
                    cached.get("data", {}),
                    source="litellm_cache",
                    source_url=url,
                    stale=True,
                )
            )
        return _apply_builtin_overrides({})

    # Persist to cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump({"_fetched_at": time.time(), "data": raw}, fh)
    logger.info("Pricing fetched and cached to %s", cache_path)
    return _apply_builtin_overrides(_parse_litellm_json(raw, source_url=url))


# Common model name aliases: map (provider, model) to LiteLLM key
_MODEL_KEY_MAP: dict[tuple[str, str], str] = {
    ("deepseek", "deepseek-v4-flash"): "deepseek/deepseek-v4-flash",
    ("deepseek", "deepseek-v4-pro"): "deepseek/deepseek-v4-pro",
}


def resolve_model_key(provider: str | None, model: str | None) -> str | None:
    """Resolve (provider, model) to a LiteLLM pricing key."""
    if not model:
        return None
    key = (provider or "", model)
    if key in _MODEL_KEY_MAP:
        return _MODEL_KEY_MAP[key]
    # Try exact model name
    return model


def get_pricing_metadata(
    provider: str | None,
    model: str | None,
    pricing: dict[str, ModelPricing],
    *,
    today: date | None = None,
) -> PricingMetadata | None:
    model_key = resolve_model_key(provider, model)
    if not model_key or model_key not in pricing:
        return None
    p = pricing[model_key]
    return PricingMetadata(
        model_key=model_key,
        source=p.pricing_source,
        source_url=p.pricing_source_url,
        stale=p.is_stale(today),
    )


def compute_request_cost(
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    pricing: dict[str, ModelPricing],
) -> float | None:
    """Compute cost for a single LLM request.

    prompt_tokens from Anthropic = base_input + cache_read + cache_write,
    so base_input = prompt_tokens - cache_read - cache_write.
    """
    model_key = resolve_model_key(provider, model)
    if not model_key or model_key not in pricing:
        return None
    p = pricing[model_key]
    base_input = prompt_tokens - cache_read_tokens - cache_write_tokens
    if base_input < 0:
        base_input = 0

    cost = base_input * p.input_cost_per_token
    cost += completion_tokens * p.output_cost_per_token

    if cache_read_tokens > 0 and p.cache_read_input_token_cost is not None:
        cost += cache_read_tokens * p.cache_read_input_token_cost
    elif cache_read_tokens > 0:
        cost += cache_read_tokens * p.input_cost_per_token

    if cache_write_tokens > 0 and p.cache_creation_input_token_cost is not None:
        cost += cache_write_tokens * p.cache_creation_input_token_cost
    elif cache_write_tokens > 0:
        cost += cache_write_tokens * p.input_cost_per_token

    return cost
