"""Client for the project-native Copilot proxy API."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

import httpx

from ...core.schema import CopilotConfig
from ..schema import CopilotNativeRequest, LLMResponse, Message, ToolDefinition
from .native_proxy import NativeProxyClient

CopilotDispatchMode = Literal["first_user_then_agent", "always_agent"]

_CONTEXT_LENGTH_PATTERNS = (
    "max_prompt_tokens_exceeded",
    "context_length_exceeded",
)


class CopilotClient(NativeProxyClient):
    """Client for the local native Copilot proxy."""

    httpx = httpx

    def __init__(
        self,
        config: CopilotConfig,
        *,
        dispatch_mode: CopilotDispatchMode = "first_user_then_agent",
    ):
        self.model = config.model
        self.base_url = config.base_url.rstrip("/")
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.reasoning_effort = config.reasoning.effort if config.reasoning else None
        self._dispatch_mode = dispatch_mode

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> CopilotNativeRequest:
        agent_dispatch = self._dispatch_mode == "always_agent"
        return CopilotNativeRequest(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=self.reasoning_effort,
            temperature=temperature if temperature is not None else self.temperature,
            initiator="agent" if agent_dispatch else "user",
            interaction_id=uuid4().hex,
            interaction_type=(
                "conversation-subagent" if agent_dispatch else "conversation-agent"
            ),
            request_id=uuid4().hex,
        )

    def _do_post(self, request: CopilotNativeRequest) -> LLMResponse:
        return self._post(
            "chat", request, LLMResponse,
            context_error_patterns=_CONTEXT_LENGTH_PATTERNS,
        )

    def chat(self, messages: list[Message], response_schema: dict[str, Any] | None = None, temperature: float | None = None) -> str:
        return self._do_post(self._build_request(messages, response_schema=response_schema, temperature=temperature)).content or ""

    def chat_with_tools(self, messages: list[Message], tools: list[ToolDefinition], temperature: float | None = None) -> LLMResponse:
        return self._do_post(self._build_request(messages, tools=tools, temperature=temperature))
