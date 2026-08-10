"""Anthropic provider client.

Thinking and output_config are passed through in the native Messages API shape.
See docs/dev/provider-api-spec.md.
"""

from typing import Any

import httpx

from ...core.schema import AnthropicConfig
from ..schema import (
    AnthropicMessagePayload,
    AnthropicResponse,
    AnthropicTool,
    AnthropicToolInputSchema,
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


class AnthropicClient:
    def __init__(self, config: AnthropicConfig):
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.thinking = map_thinking(config.thinking)
        self.output_config = map_output_config(config.output_config)
        self.has_active_thinking = has_active_thinking(self.thinking)

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[AnthropicTool]:
        return [
            AnthropicTool(
                name=tool.name,
                description=tool.description,
                input_schema=AnthropicToolInputSchema(
                    properties=tool.to_json_schema()["properties"],
                    required=tool.to_json_schema()["required"],
                ),
            )
            for tool in tools
        ]

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str | list[dict[str, Any]] | None, list[AnthropicMessagePayload]]:
        system_blocks, converted = convert_messages(messages, AnthropicMessagePayload)
        return system_blocks or None, converted

    @staticmethod
    def _serialize_messages(messages: list[AnthropicMessagePayload]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message.content, str):
                result.append({"role": message.role, "content": message.content})
            else:
                result.append({
                    "role": message.role,
                    "content": [
                        block if isinstance(block, dict) else block.model_dump(exclude_none=True)
                        for block in message.content
                    ],
                })
        return result

    @staticmethod
    def _parse_response(response: AnthropicResponse) -> LLMResponse:
        return parse_response(response)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        system, chat_messages = self._convert_messages(messages)
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(chat_messages),
            "max_tokens": self.max_tokens,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [tool.model_dump() for tool in self._convert_tools(tools)]
        if self.thinking:
            request["thinking"] = self.thinking
        if self.output_config:
            request["output_config"] = self.output_config
        effective_temperature = temperature if temperature is not None else self.temperature
        if effective_temperature is not None and not self.has_active_thinking:
            request["temperature"] = effective_temperature
        return request

    def _post(self, request: dict[str, Any]) -> AnthropicResponse:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if self.output_config:
            headers["anthropic-beta"] = "effort-2025-11-24"
        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(f"{self.base_url}/v1/messages", headers=headers, json=request)
            response.raise_for_status()
        return AnthropicResponse.model_validate(response.json())

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        if response_schema is not None:
            raise ValueError("Anthropic provider does not support response_schema; use a provider with native structured outputs.")
        return self._parse_response(self._post(self._build_request(messages, temperature=temperature))).content or ""

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        return self._parse_response(self._post(self._build_request(messages, tools=tools, temperature=temperature)))
