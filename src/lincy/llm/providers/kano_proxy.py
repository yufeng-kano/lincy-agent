"""Kano Proxy provider client.

Kano Proxy is modeled as an Anthropic-compatible gateway. The concrete client
keeps a distinct provider entry point while reusing the native Anthropic
Messages adapter implementation.
"""

from ...core.schema import KanoProxyConfig
from ..schema import Message, ToolDefinition
from ..session import current_llm_session_key
from .anthropic import AnthropicClient


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
