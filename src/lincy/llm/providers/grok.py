"""Grok provider client (local SuperGrok OAuth proxy).

Talks OpenAI-compatible Chat Completions against grok-proxy. Auth is owned
by the proxy (device-code OAuth); this client sends a sentinel bearer only.
Reasoning uses top-level reasoning_effort, matching xAI Chat Completions.

Prompt cache: xAI auto-caches shared prefixes, but hit rate is maximized by
sticky routing via the ``x-grok-conv-id`` HTTP header (Chat Completions).
The assembly layer supplies a session-scoped id provider for that header.
See docs/dev/provider-api-spec.md.
"""

from __future__ import annotations

from collections.abc import Callable

from ...core.schema import GrokConfig, GrokReasoningConfig
from ..schema import Message, OpenAIMessagePayload
from .openai_compat import OpenAICompatibleClient, merge_leading_system_messages

# Proxy injects the real SuperGrok OAuth token; header must still be present.
_LOCAL_PROXY_BEARER = "local-proxy"

# Official sticky-routing header for Chat Completions prompt cache.
# https://docs.x.ai/developers/advanced-api-usage/prompt-caching/maximizing-cache-hits
X_GROK_CONV_ID_HEADER = "x-grok-conv-id"


def _map_reasoning_effort(reasoning: GrokReasoningConfig | None) -> str | None:
    """Map reasoning config to Chat Completions reasoning_effort string."""

    if reasoning is None:
        return None
    if reasoning.enabled is False:
        # Best-effort "off". Models that disallow disabling reasoning (e.g.
        # grok-4.5) will reject this upstream — profile supported_efforts
        # should not offer enabled=false for those models.
        return "none"
    if reasoning.effort is not None:
        return reasoning.effort
    return None


class GrokClient(OpenAICompatibleClient):
    def __init__(
        self,
        config: GrokConfig,
        *,
        conv_id_provider: Callable[[], str | None] | None = None,
    ):
        super().__init__(
            model=config.model,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            request_timeout=config.request_timeout,
            reasoning_effort=_map_reasoning_effort(config.reasoning),
            temperature=config.temperature,
        )
        self._conv_id_provider = conv_id_provider

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {_LOCAL_PROXY_BEARER}",
            "Content-Type": "application/json",
        }
        if self._conv_id_provider is not None:
            conv_id = self._conv_id_provider()
            if conv_id:
                headers[X_GROK_CONV_ID_HEADER] = conv_id
        return headers

    def _convert_messages(self, messages: list[Message]) -> list[OpenAIMessagePayload]:
        """Merge consecutive leading system messages into one.

        xAI Chat Completions is OpenAI-compatible; multi-system messages are
        not a documented guarantee, so merge them like the OpenAI adapter.
        Stable leading system prefix also keeps automatic prompt cache hits.
        """
        return merge_leading_system_messages(super()._convert_messages(messages))
