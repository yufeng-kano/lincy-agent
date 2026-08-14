"""Session-aware debug logging wrapper for normalized LLM clients."""

from __future__ import annotations

import time
from typing import Any, Protocol

from ..llm import LLMResponse, Message, ServedCandidate, ToolDefinition
from ..llm.base import LLMClient
from ..llm.failover import observe_served_candidate
from .debug_store import PendingLLMRequest


class _SessionDebugSink(Protocol):
    """Subset of session debug methods needed by the client wrapper."""

    def begin_llm_request(
        self,
        *,
        client_label: str,
        provider: str | None,
        model: str | None,
        call_type: str,
        messages: list[Message],
        temperature: float | None,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> PendingLLMRequest | None:
        ...

    def complete_llm_response(
        self,
        pending: PendingLLMRequest | None,
        *,
        response: LLMResponse,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        ...

    def complete_llm_text_response(
        self,
        pending: PendingLLMRequest | None,
        *,
        response_text: str,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        ...

    def fail_llm_request(
        self,
        pending: PendingLLMRequest | None,
        *,
        error: Exception,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        ...


class DebugLoggingLLMClient:
    """Decorate one LLM client with session request/response logging."""

    def __init__(
        self,
        client: LLMClient,
        *,
        sink: _SessionDebugSink,
        client_label: str,
        provider: str | None,
        model: str | None,
    ) -> None:
        self._client = client
        self._sink = sink
        self._client_label = client_label
        self._provider = provider
        self._model = model

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        pending = self._sink.begin_llm_request(
            client_label=self._client_label,
            provider=self._provider,
            model=self._model,
            call_type="chat",
            messages=messages,
            response_schema=response_schema,
            temperature=temperature,
        )
        started = time.perf_counter()
        # The failover chain lives below this wrapper, so the candidate that
        # actually answered is reported out of band.
        with observe_served_candidate() as probe:
            try:
                response_text = self._client.chat(
                    messages,
                    response_schema=response_schema,
                    temperature=temperature,
                )
            except Exception as error:
                self._sink.fail_llm_request(
                    pending,
                    error=error,
                    latency_ms=_elapsed_ms(started),
                    served=probe.get(),
                )
                raise
            self._sink.complete_llm_text_response(
                pending,
                response_text=response_text,
                latency_ms=_elapsed_ms(started),
                served=probe.get(),
            )
        return response_text

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        pending = self._sink.begin_llm_request(
            client_label=self._client_label,
            provider=self._provider,
            model=self._model,
            call_type="chat_with_tools",
            messages=messages,
            tools=tools,
            temperature=temperature,
        )
        started = time.perf_counter()
        with observe_served_candidate() as probe:
            try:
                response = self._client.chat_with_tools(
                    messages,
                    tools,
                    temperature=temperature,
                )
            except Exception as error:
                self._sink.fail_llm_request(
                    pending,
                    error=error,
                    latency_ms=_elapsed_ms(started),
                    served=probe.get(),
                )
                raise
            self._sink.complete_llm_response(
                pending,
                response=response,
                latency_ms=_elapsed_ms(started),
                served=probe.get(),
            )
        return response

    def compact_messages(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> list[Message]:
        compact_client = getattr(self._client, "compact_messages", None)
        if compact_client is None:
            raise AttributeError("compact_messages")
        return compact_client(messages, tools=tools)


def wrap_llm_client_with_session_debug(
    client: LLMClient,
    *,
    sink: _SessionDebugSink | None,
    client_label: str,
    provider: str | None,
    model: str | None,
) -> LLMClient:
    """Return a session-logging wrapper when a debug sink is available."""
    if sink is None:
        return client
    return DebugLoggingLLMClient(
        client,
        sink=sink,
        client_label=client_label,
        provider=provider,
        model=model,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
