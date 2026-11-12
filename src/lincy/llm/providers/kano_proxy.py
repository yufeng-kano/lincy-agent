"""Kano Proxy provider client.

Kano Proxy is modeled as an Anthropic-compatible gateway. The concrete client
keeps a distinct provider entry point while reusing the native Anthropic
Messages adapter implementation. The OpenAI-compatible surface, used only for
audio, reuses the OpenAI adapter the same way.
"""

import httpx

from ...core.schema import KanoProxyConfig
from ..schema import Message, ToolDefinition
from ..session import current_llm_session_key
from .anthropic import AnthropicClient
from .anthropic_messages import served_upstream
from .openai import OpenAIClient


class KanoProxyClient(AnthropicClient):
    """Anthropic Messages API client pointed at the Kano Proxy gateway."""

    def __init__(self, config: KanoProxyConfig):
        super().__init__(config)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
    ) -> dict:
        request = super()._build_request(messages, tools=tools, temperature=temperature)
        session_key = current_llm_session_key()
        if session_key is not None:
            request["metadata"] = {"user_id": session_key}
        return request


class KanoProxyOpenAIClient(OpenAIClient):
    """OpenAI Chat Completions client pointed at the Kano Proxy gateway."""

    def _served_upstream(self, headers: httpx.Headers) -> str | None:
        # The gateway names the target behind a group alias on both surfaces.
        return served_upstream(headers)
