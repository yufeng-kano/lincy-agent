"""Model pricing: fetch from LiteLLM, cache locally, compute request costs."""

from __future__ import annotations

import json
import logging
import re
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

# Upstream matching mirrors Kano Proxy's resolve_model_price
# (apps/api/crates/kano-proxy-core/src/pricing/litellm.rs), so a gateway
# request is billed at the rate the gateway itself logs.
_VENDOR_PREFIXES = ("anthropic", "openai", "xai", "gemini", "vertex_ai", "openrouter")
_BRACKET_VARIANT_RE = re.compile(r"\[[^\]]*\]$")
_EFFORT_SUFFIX_RE = re.compile(r"-(?:thinking-)?(?:high|medium|low|tiered)$")
_THINKING_SUFFIX_RE = re.compile(r"-(?:thinking|thought)$")


def _normalize_id(value: str) -> str:
    return _BRACKET_VARIANT_RE.sub("", value.strip().lower())


def _is_gemini_id(value: str) -> bool:
    return (
        value.startswith(("gemini-", "gemini/"))
        or value == "gemini"
        or "/gemini-" in value
        or value.endswith("/gemini")
    )


def _resolve_upstream_key(
    served_upstream: str,
    pricing: dict[str, ModelPricing],
) -> str | None:
    """Pricing key for a gateway-reported `provider/model`; None when unpriced."""
    if "/" in served_upstream:
        provider, model = served_upstream.split("/", 1)
    else:
        provider, model = "", served_upstream
    provider = _normalize_id(provider)
    model = _normalize_id(model)
    if not model:
        return None
    if provider == "openrouter":
        key = f"openrouter/{model}"
        return key if key in pricing else None

    # The id itself, then with its own leading path segments stripped one by one.
    parts = model.split("/")
    bases = ["/".join(parts[i:]) for i in range(len(parts))]
    bases = [base for base in bases if base]
    variants: list[str] = []
    # Reasoning-effort and preview variants are only folded for Gemini, so an
    # arbitrary `-high` model is never priced as its base.
    for base in bases:
        if provider != "antigravity" and not _is_gemini_id(base):
            continue
        stripped = _THINKING_SUFFIX_RE.sub("", _EFFORT_SUFFIX_RE.sub("", base))
        if stripped != base:
            variants.append(stripped)
        if base.endswith("-preview"):
            variants.append(base.removesuffix("-preview"))
        else:
            variants.append(f"{base}-preview")
            if stripped != base:
                variants.append(f"{stripped}-preview")

    candidates = [
        *bases,
        *(f"{prefix}/{base}" for base in bases for prefix in _VENDOR_PREFIXES),
        *variants,
        *(f"{prefix}/{variant}" for variant in variants for prefix in _VENDOR_PREFIXES),
    ]
    return next((key for key in candidates if key in pricing), None)


def resolve_model_key(
    provider: str | None,
    model: str | None,
    pricing: dict[str, ModelPricing],
    *,
    served_upstream: str | None = None,
) -> str | None:
    """Resolve one response to a pricing key present in ``pricing``.

    A gateway-reported upstream decides on its own: the profile model behind a
    gateway is an alias, and pricing it instead would be a guess.
    """
    if served_upstream:
        return _resolve_upstream_key(served_upstream, pricing)
    if not model:
        return None
    key = _MODEL_KEY_MAP.get((provider or "", model), model)
    return key if key in pricing else None


def get_pricing_metadata(
    provider: str | None,
    model: str | None,
    pricing: dict[str, ModelPricing],
    *,
    served_upstream: str | None = None,
    today: date | None = None,
) -> PricingMetadata | None:
    model_key = resolve_model_key(provider, model, pricing, served_upstream=served_upstream)
    if model_key is None:
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
    *,
    served_upstream: str | None = None,
) -> float | None:
    """Compute cost for a single LLM request.

    prompt_tokens from Anthropic = base_input + cache_read + cache_write,
    so base_input = prompt_tokens - cache_read - cache_write.
    """
    model_key = resolve_model_key(provider, model, pricing, served_upstream=served_upstream)
    if model_key is None:
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
