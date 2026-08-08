"""Anthropic provider client.

Thinking and output_config are passed through in the native Messages API shape.
See docs/dev/provider-api-spec.md.
"""

from typing import Any

import httpx

from ...core.schema import (
    AnthropicConfig,
    AnthropicOutputConfig,
    AnthropicThinkingConfig,
)
from ..schema import (
    AnthropicContent,
    AnthropicMessagePayload,
    AnthropicResponse,
    AnthropicTextContent,
    AnthropicTool,
    AnthropicToolInputSchema,
    AnthropicToolResultContent,
    AnthropicToolUseContent,
    ContentPart,
    LLMResponse,
    Message,
    ToolCall,
    ToolDefinition,
)


def _map_thinking(
    thinking: AnthropicThinkingConfig | None,
) -> dict[str, Any] | None:
    if thinking is None:
        return None
    payload: dict[str, Any] = {"type": thinking.type}
    budget_tokens = getattr(thinking, "budget_tokens", None)
    if budget_tokens is not None:
        payload["budget_tokens"] = budget_tokens
    return payload


def _map_output_config(
    output_config: AnthropicOutputConfig | None,
) -> dict[str, Any] | None:
    if output_config is None or output_config.effort is None:
        return None
    return {"effort": output_config.effort}


def _has_active_thinking(thinking: dict[str, Any] | None) -> bool:
    return thinking is not None and thinking.get("type") != "disabled"


class AnthropicClient:
    def __init__(self, config: AnthropicConfig):
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.thinking = _map_thinking(config.thinking)
        self.output_config = _map_output_config(config.output_config)
        self.has_active_thinking = _has_active_thinking(self.thinking)

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[AnthropicTool]:
        """Convert ToolDefinition list to Anthropic tools format."""
        result = []
        for tool in tools:
            schema = tool.to_json_schema()
            result.append(
                AnthropicTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=AnthropicToolInputSchema(
                        properties=schema["properties"],
                        required=schema["required"],
                    ),
                )
            )
        return result

    @staticmethod
    def _convert_content_parts_to_blocks(
        parts: list[ContentPart],
    ) -> list[dict[str, Any]]:
        """Convert ContentPart list to Anthropic content blocks."""
        blocks: list[dict[str, Any]] = []
        for part in parts:
            if part.type == "text" and part.text is not None:
                block: dict[str, Any] = {"type": "text", "text": part.text}
                if part.cache_control is not None:
                    block["cache_control"] = part.cache_control
                blocks.append(block)
            elif part.type == "image" and part.data and part.media_type:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.media_type,
                            "data": part.data,
                        },
                    }
                )
        return blocks

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str | list[dict[str, Any]] | None, list[AnthropicMessagePayload]]:
        """Convert Message list to Anthropic format.

        Returns (system, messages) where system can be a plain string
        or a list of content blocks with cache_control annotations.
        """
        system_blocks: list[dict[str, Any]] = []
        result: list[AnthropicMessagePayload] = []
        # Index of the user message currently collecting tool_result blocks.
        pending_tool_result_index: int | None = None

        for m in messages:
            if m.role == "system":
                if isinstance(m.content, list):
                    for part in m.content:
                        if part.type == "text" and part.text:
                            block: dict[str, Any] = {
                                "type": "text",
                                "text": part.text,
                            }
                            if part.cache_control is not None:
                                block["cache_control"] = part.cache_control
                            system_blocks.append(block)
                elif isinstance(m.content, str):
                    block = {"type": "text", "text": m.content}
                    if m.cache_control is not None:
                        block["cache_control"] = m.cache_control
                    system_blocks.append(block)
                continue
            elif m.role == "tool":
                block_or_result: AnthropicContent | dict[str, Any]
                if isinstance(m.content, list):
                    # Multimodal tool result: wrap content blocks in tool_result
                    block_or_result = {
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": self._convert_content_parts_to_blocks(m.content),
                    }
                else:
                    block_or_result = AnthropicToolResultContent(
                        tool_use_id=m.tool_call_id or "",
                        content=m.content or "",
                    )
                # All results of one parallel tool_use turn belong in a single
                # user message. Splitting them works on api.anthropic.com only
                # because it merges consecutive user turns; the heyroute gateway
                # rejects the split form. See docs/dev/provider-api-spec.md.
                if pending_tool_result_index is not None:
                    pending = result[pending_tool_result_index]
                    if isinstance(pending.content, list):
                        pending.content.append(block_or_result)
                        continue
                result.append(
                    AnthropicMessagePayload(role="user", content=[block_or_result])
                )
                pending_tool_result_index = len(result) - 1
                continue
            elif m.role == "assistant" and m.tool_calls:
                content_blocks: list[AnthropicContent] = []
                if isinstance(m.content, str) and m.content:
                    content_blocks.append(AnthropicTextContent(text=m.content))
                for tc in m.tool_calls:
                    content_blocks.append(
                        AnthropicToolUseContent(
                            id=tc.id,
                            name=tc.name,
                            input=tc.arguments,
                        )
                    )
                result.append(
                    AnthropicMessagePayload(role="assistant", content=content_blocks)
                )
                pending_tool_result_index = None
            else:
                # Always use content-block array format for stable prefix
                # serialization (Anthropic prompt cache is byte-level).
                if isinstance(m.content, list):
                    blocks = self._convert_content_parts_to_blocks(m.content)
                else:
                    block = {"type": "text", "text": m.content or ""}
                    if m.cache_control is not None:
                        block["cache_control"] = m.cache_control
                    blocks = [block]
                result.append(AnthropicMessagePayload(role=m.role, content=blocks))
                pending_tool_result_index = None

        system: str | list[dict[str, Any]] | None = None
        if system_blocks:
            # Use array format to preserve cache_control annotations.
            system = system_blocks
        return system, result

    def _parse_response(self, response: AnthropicResponse) -> LLMResponse:
        """Parse Anthropic response into unified LLMResponse."""
        text_blocks: list[str] = []
        tool_calls = []

        for block in response.content:
            if block.type == "text" and block.text:
                text_blocks.append(block.text)
            elif block.type == "tool_use" and block.id and block.name:
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input or {},
                    )
                )

        content = "".join(text_blocks) if text_blocks else None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        total_tokens: int | None = None
        cache_read = 0
        cache_write = 0
        usage_available = response.usage is not None
        if response.usage is not None:
            base_input = response.usage.input_tokens
            cache_read = response.usage.cache_read_input_tokens or 0
            cache_write = response.usage.cache_creation_input_tokens or 0
            prompt_tokens = (base_input or 0) + cache_read + cache_write
            completion_tokens = response.usage.output_tokens
            total_tokens = prompt_tokens + (completion_tokens or 0)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_available=usage_available,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    def _serialize_messages(
        self, messages: list[AnthropicMessagePayload]
    ) -> list[dict[str, Any]]:
        """Serialize messages to JSON-compatible format."""
        result = []
        for m in messages:
            if isinstance(m.content, str):
                result.append({"role": m.role, "content": m.content})
            else:
                # Content is a list of content blocks (Pydantic models or dicts)
                content_list = []
                for block in m.content:
                    if isinstance(block, dict):
                        content_list.append(block)
                    else:
                        content_list.append(block.model_dump(exclude_none=True))
                result.append({"role": m.role, "content": content_list})
        return result

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if self.output_config:
            headers["anthropic-beta"] = "effort-2025-11-24"

        system, chat_messages = self._convert_messages(messages)

        request_data: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(chat_messages),
            "max_tokens": self.max_tokens,
        }
        if system:
            request_data["system"] = system
        if self.thinking:
            request_data["thinking"] = self.thinking
        if self.output_config:
            request_data["output_config"] = self.output_config
        effective_temp = temperature if temperature is not None else self.temperature
        if effective_temp is not None and not self.has_active_thinking:
            request_data["temperature"] = effective_temp

        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(url, headers=headers, json=request_data)
            response.raise_for_status()
            data = response.json()

        result = AnthropicResponse.model_validate(data)
        # Concatenate all text blocks in-order.
        text_blocks: list[str] = []
        for block in result.content:
            if block.type == "text" and block.text:
                text_blocks.append(block.text)
        return "".join(text_blocks)

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send messages with tool definitions and return response."""
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if self.output_config:
            headers["anthropic-beta"] = "effort-2025-11-24"

        system, chat_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools) if tools else None

        request_data: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(chat_messages),
            "max_tokens": self.max_tokens,
        }
        if system:
            request_data["system"] = system
        if anthropic_tools:
            request_data["tools"] = [t.model_dump() for t in anthropic_tools]
        if self.thinking:
            request_data["thinking"] = self.thinking
        if self.output_config:
            request_data["output_config"] = self.output_config
        effective_temp = temperature if temperature is not None else self.temperature
        if effective_temp is not None and not self.has_active_thinking:
            request_data["temperature"] = effective_temp

        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(url, headers=headers, json=request_data)
            response.raise_for_status()
            data = response.json()

        result = AnthropicResponse.model_validate(data)
        return self._parse_response(result)
