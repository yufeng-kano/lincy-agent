"""Client for the project-native Copilot proxy API."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from ...core.schema import CopilotConfig
from ..schema import CopilotNativeRequest, LLMResponse, Message, ToolDefinition
from .copilot_runtime import CopilotDispatchMode, CopilotRequestRouting, CopilotRuntime
from .native_proxy import NativeProxyClient

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
        runtime: CopilotRuntime | None = None,
        dispatch_mode: CopilotDispatchMode = "first_user_then_agent",
    ):
        self.model = config.model
        self.base_url = config.base_url.rstrip("/")
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.reasoning_effort = config.reasoning.effort if config.reasoning else None
        self._runtime = runtime
        self._dispatch_mode = dispatch_mode

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> CopilotNativeRequest:
        routing = self._resolve_routing()
        return CopilotNativeRequest(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=self.reasoning_effort,
            temperature=temperature if temperature is not None else self.temperature,
            initiator=routing.initiator,
            interaction_id=routing.interaction_id,
            interaction_type=routing.interaction_type,
            request_id=routing.request_id,
        )

    def _resolve_routing(self) -> CopilotRequestRouting:
        if self._runtime is not None:
            return self._runtime.resolve_request(self._dispatch_mode)
        interaction_type = (
            "conversation-agent"
            if self._dispatch_mode == "first_user_then_agent"
            else "conversation-subagent"
        )
        return CopilotRequestRouting(
            initiator="user" if self._dispatch_mode == "first_user_then_agent" else "agent",
            interaction_id=uuid4().hex,
            interaction_type=interaction_type,
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
