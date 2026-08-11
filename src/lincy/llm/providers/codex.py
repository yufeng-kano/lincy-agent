"""Client for the project-native Codex proxy API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from ...core.schema import CodexConfig
from ..schema import (
    CodexCompactRequest,
    CodexCompactResponse,
    CodexNativeRequest,
    LLMResponse,
    Message,
    ToolDefinition,
)
from .native_proxy import NativeProxyClient

_CONTEXT_LENGTH_PATTERNS = ("context_length_exceeded",)


class CodexClient(NativeProxyClient):
    """Client for the local native Codex proxy."""

    httpx = httpx

    def __init__(
        self,
        config: CodexConfig,
        *,
        cache_key_provider: Callable[[], str | None] | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
        turn_id_provider: Callable[[], str | None] | None = None,
    ):
        self.model = config.model
        self.base_url = config.base_url.rstrip("/")
        self.max_output_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.reasoning_effort = config.reasoning.effort if config.reasoning else None
        self._cache_key_provider = cache_key_provider
        self._session_id_provider = session_id_provider
        self._turn_id_provider = turn_id_provider

    def _build_request(self, messages: list[Message], *, tools: list[ToolDefinition] | None = None, response_schema: dict[str, Any] | None = None, temperature: float | None = None) -> CodexNativeRequest:
        return CodexNativeRequest(
            model=self.model,
            messages=messages,
            max_output_tokens=self.max_output_tokens,
            prompt_cache_key=self._cache_key_provider() if self._cache_key_provider else None,
            session_id=self._session_id_provider() if self._session_id_provider else None,
            turn_id=self._turn_id_provider() if self._turn_id_provider else None,
            tools=tools,
            response_schema=response_schema,
            reasoning_effort=self.reasoning_effort,
            temperature=temperature if temperature is not None else self.temperature,
        )

    def _do_post(self, request: CodexNativeRequest) -> LLMResponse:
        return self._post("chat", request, LLMResponse, context_error_patterns=_CONTEXT_LENGTH_PATTERNS)

    def compact_messages(self, messages: list[Message], tools: list[ToolDefinition] | None = None) -> list[Message]:
        request = CodexCompactRequest(
            model=self.model,
            messages=messages,
            session_id=self._session_id_provider() if self._session_id_provider else None,
            turn_id=self._turn_id_provider() if self._turn_id_provider else None,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
        )
        return self._post("compact", request, CodexCompactResponse, context_error_patterns=_CONTEXT_LENGTH_PATTERNS).messages

    def chat(self, messages: list[Message], response_schema: dict[str, Any] | None = None, temperature: float | None = None) -> str:
        return self._do_post(self._build_request(messages, response_schema=response_schema, temperature=temperature)).content or ""

    def chat_with_tools(self, messages: list[Message], tools: list[ToolDefinition], temperature: float | None = None) -> LLMResponse:
        return self._do_post(self._build_request(messages, tools=tools, temperature=temperature))
