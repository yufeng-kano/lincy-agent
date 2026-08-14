"""Agent-level LLM client composition."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core.schema import AgentConfig, LLMConfig
from .base import LLMClient
from .factory import create_client
from .failover import FailoverCandidate, llm_failover_key, with_llm_failover

ProviderKwargsFactory = Callable[[LLMConfig], dict[str, Any]]


def create_agent_client(
    agent_config: AgentConfig,
    *,
    retry_label: str | None = None,
    provider_kwargs_factory: ProviderKwargsFactory | None = None,
) -> LLMClient:
    """Create one agent client, optionally with fallback LLMs."""

    candidates: list[FailoverCandidate] = []
    llm_configs = [agent_config.llm, *agent_config.llm_fallbacks]
    has_fallbacks = len(llm_configs) > 1
    for index, llm_config in enumerate(llm_configs):
        provider_kwargs = (
            provider_kwargs_factory(llm_config)
            if provider_kwargs_factory is not None
            else {}
        )
        candidate_retry_label = retry_label
        if retry_label and index > 0:
            candidate_retry_label = f"{retry_label}.fallback{index}"
        # Earlier candidates should fail over immediately on 429 instead of
        # burning the full rate-limit retry budget before the next provider.
        rate_limit_retries = agent_config.llm_rate_limit_retries
        if has_fallbacks and index < len(llm_configs) - 1:
            rate_limit_retries = 0
        client = create_client(
            llm_config,
            transient_retries=agent_config.llm_transient_retries,
            request_timeout=agent_config.llm_request_timeout,
            rate_limit_retries=rate_limit_retries,
            retry_label=candidate_retry_label,
            **provider_kwargs,
        )
        candidates.append(
            FailoverCandidate(
                key=llm_failover_key(llm_config),
                label=_candidate_label(llm_config),
                client=client,
                supports_vision=llm_config.get_vision(),
                provider=llm_config.provider,
                model=getattr(llm_config, "model", None),
            )
        )

    return with_llm_failover(
        candidates,
        cooldown_seconds=agent_config.llm_fallback_cooldown_seconds,
        label=retry_label,
    )


def _candidate_label(config: LLMConfig) -> str:
    model = getattr(config, "model", "")
    if isinstance(model, str) and model:
        return f"{config.provider}:{model}"
    return config.provider
