"""OpenRouter provider client.

Reasoning: uses reasoning: {"effort": ...} object format.
Effort and max_tokens are mutually exclusive (validated at config level).
See docs/dev/provider-api-spec.md.
"""

from typing import Any

from ..schema import Message, OpenAIMessagePayload, OpenAIRequest, ToolDefinition
from ...core.schema import (
    OpenRouterConfig,
    OpenRouterProviderRoutingConfig,
    OpenRouterReasoningConfig,
)
from .openai_compat import OpenAICompatibleClient, merge_leading_system_messages


def _routes_to_anthropic(config: OpenRouterConfig) -> bool:
    """Return True when the profile is pinned to Anthropic provider."""
    routing = config.provider_routing
    if routing is None or routing.order is None:
        # Check model ID prefix as fallback
        return config.model.startswith("anthropic/")
    return any(slug.startswith("anthropic") for slug in routing.order)


class OpenRouterRequest(OpenAIRequest):
    reasoning: dict[str, Any] | None = None
    provider: dict[str, Any] | None = None
    verbosity: str | None = None


def _map_reasoning(
    reasoning: OpenRouterReasoningConfig | None,
) -> dict[str, Any] | None:
    """Map reasoning config to OpenRouter reasoning object."""
    if reasoning is None:
        return None
    if reasoning.enabled is False:
        return {"effort": "none"}

    payload: dict[str, Any] = {}
    has_explicit_controls = (
        reasoning.effort is not None or reasoning.max_tokens is not None
    )
    if reasoning.enabled is True and not has_explicit_controls:
        payload["enabled"] = True
    # Mutual exclusivity guaranteed by config validation.
    if reasoning.effort is not None:
        payload["effort"] = reasoning.effort
    if reasoning.max_tokens is not None:
        payload["max_tokens"] = reasoning.max_tokens
    return payload or None


def _map_provider_routing(
    provider_routing: OpenRouterProviderRoutingConfig | None,
) -> dict[str, Any] | None:
    """Map OpenRouter provider_routing config to request provider object."""
    if provider_routing is None:
        return None
    payload: dict[str, Any] = {}
    if provider_routing.order is not None:
        payload["order"] = provider_routing.order
    if provider_routing.ignore is not None:
        payload["ignore"] = provider_routing.ignore
    if provider_routing.require_parameters is not None:
        payload["require_parameters"] = provider_routing.require_parameters
    if provider_routing.allow_fallbacks is not None:
        payload["allow_fallbacks"] = provider_routing.allow_fallbacks
    return payload or None


class OpenRouterClient(OpenAICompatibleClient):
    def __init__(self, config: OpenRouterConfig):
        self.api_key = config.api_key
        self.site_url = config.site_url
        self.site_name = config.site_name
        self.verbosity = config.verbosity
        self._pins_anthropic = _routes_to_anthropic(config)
        super().__init__(
            model=config.model,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            request_timeout=config.request_timeout,
            temperature=config.temperature,
        )
        self.reasoning_payload = _map_reasoning(config.reasoning)
        self.provider_payload = _map_provider_routing(config.provider_routing)

    def _convert_messages(self, messages: list[Message]) -> list[OpenAIMessagePayload]:
        """Merge consecutive leading system messages into one.

        Many OpenRouter downstream providers (Together, Parasail, Nebius)
        reject multiple system messages.  Anthropic supports them natively
        and needs the original structure for cache_control breakpoints.
        """
        converted = super()._convert_messages(messages)
        if self._pins_anthropic:
            return converted
        return merge_leading_system_messages(converted)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> OpenRouterRequest:
        request = super()._build_request(
            messages,
            tools=tools,
            response_schema=response_schema,
            temperature=temperature,
        )
        payload = request.model_dump()
        payload["reasoning"] = self.reasoning_payload
        payload["provider"] = self.provider_payload
        if self.verbosity is not None:
            payload["verbosity"] = self.verbosity
        return OpenRouterRequest.model_validate(payload)

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.site_name:
            headers["X-OpenRouter-Title"] = self.site_name
            headers["X-Title"] = self.site_name
        return headers
