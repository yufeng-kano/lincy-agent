"""Client for the project-native Claude Code proxy API."""

from __future__ import annotations

from typing import Any

import httpx

from ...core.schema import ClaudeCodeConfig
from ..schema import (
    AnthropicResponse,
    AnthropicTool,
    AnthropicToolInputSchema,
    ClaudeCodeMessagePayload,
    ClaudeCodeRequest,
    LLMResponse,
    Message,
    ToolDefinition,
)
from .anthropic_messages import (
    convert_messages,
    has_active_thinking,
    map_output_config,
    map_thinking,
    parse_response,
)


class ClaudeCodeClient:
    """Client for the local Claude Code proxy."""

    def __init__(self, config: ClaudeCodeConfig):
        self.model = config.model
        self.base_url = config.base_url.rstrip("/")
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.thinking = map_thinking(config.thinking)
        self.output_config = map_output_config(config.output_config)
        self.has_thinking = has_active_thinking(self.thinking)

    @staticmethod
    def _get_headers() -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[list[dict[str, Any]], list[ClaudeCodeMessagePayload]]:
        return convert_messages(messages, ClaudeCodeMessagePayload)

    @staticmethod
    def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            schema = tool.to_json_schema()
            converted.append(AnthropicTool(
                name=tool.name,
                description=tool.description,
                input_schema=AnthropicToolInputSchema(
                    properties=schema["properties"], required=schema["required"]
                ),
            ).model_dump())
        return converted

    @staticmethod
    def _parse_response(response: AnthropicResponse) -> LLMResponse:
        return parse_response(response)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
    ) -> ClaudeCodeRequest:
        system_blocks, chat_messages = self._convert_messages(messages)
        effective_temperature = temperature if temperature is not None else self.temperature
        return ClaudeCodeRequest(
            model=self.model,
            system=system_blocks or None,
            messages=chat_messages,
            max_tokens=self.max_tokens,
            tools=self._convert_tools(tools) if tools else None,
            thinking=self.thinking,
            output_config=self.output_config,
            temperature=effective_temperature if not self.has_thinking else None,
        )

    def _post(self, request: ClaudeCodeRequest) -> AnthropicResponse:
        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/messages",
                headers=self._get_headers(),
                json=request.model_dump(exclude_none=True, by_alias=True),
            )
            response.raise_for_status()
        return AnthropicResponse.model_validate(response.json())

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        if response_schema is not None:
            raise ValueError(
                "Claude Code provider does not support response_schema; "
                "use a provider with native structured outputs."
            )
        return self._parse_response(self._post(self._build_request(messages, temperature=temperature))).content or ""

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        return self._parse_response(self._post(self._build_request(messages, tools=tools, temperature=temperature)))
