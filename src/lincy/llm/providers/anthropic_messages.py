"""Shared Anthropic Messages payload and response mapping."""

from __future__ import annotations

from typing import Any, TypeVar

from ..schema import AnthropicResponse, ContentPart, LLMResponse, Message, ToolCall

PayloadT = TypeVar("PayloadT")


def map_thinking(thinking: Any | None) -> dict[str, Any] | None:
    if thinking is None:
        return None
    payload: dict[str, Any] = {"type": thinking.type}
    budget_tokens = getattr(thinking, "budget_tokens", None)
    if budget_tokens is not None:
        payload["budget_tokens"] = budget_tokens
    return payload


def map_output_config(output_config: Any | None) -> dict[str, Any] | None:
    if output_config is None or output_config.effort is None:
        return None
    return {"effort": output_config.effort}


def has_active_thinking(thinking: dict[str, Any] | None) -> bool:
    return thinking is not None and thinking.get("type") != "disabled"


def content_parts_to_blocks(parts: list[ContentPart]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "text" and part.text is not None:
            block: dict[str, Any] = {"type": "text", "text": part.text}
            if part.cache_control is not None:
                block["cache_control"] = part.cache_control
            blocks.append(block)
        elif part.type == "image" and part.data and part.media_type:
            block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.data,
                },
            }
            if part.cache_control is not None:
                block["cache_control"] = part.cache_control
            blocks.append(block)
    return blocks


def convert_messages(
    messages: list[Message], payload_type: type[PayloadT]
) -> tuple[list[dict[str, Any]], list[PayloadT]]:
    system_blocks: list[dict[str, Any]] = []
    result: list[PayloadT] = []
    pending_tool_result_index: int | None = None
    for message in messages:
        if message.role == "system":
            if isinstance(message.content, list):
                system_blocks.extend(content_parts_to_blocks(message.content))
            elif isinstance(message.content, str) and message.content:
                block = {"type": "text", "text": message.content}
                if message.cache_control is not None:
                    block["cache_control"] = message.cache_control
                system_blocks.append(block)
            continue
        if message.role == "tool":
            content: str | list[dict[str, Any]]
            if isinstance(message.content, list):
                content = [{"type": "tool_result", "tool_use_id": message.tool_call_id or "", "content": content_parts_to_blocks(message.content)}]
            else:
                content = [{"type": "tool_result", "tool_use_id": message.tool_call_id or "", "content": message.content or ""}]
            # Messages API requires all results from a parallel tool-use turn in one user message.
            if pending_tool_result_index is not None:
                pending = result[pending_tool_result_index]
                if isinstance(pending.content, list):
                    pending.content.extend(content)
                    continue
            result.append(payload_type(role="user", content=content))
            pending_tool_result_index = len(result) - 1
            continue
        if message.role == "assistant" and message.tool_calls:
            blocks = content_parts_to_blocks(message.content) if isinstance(message.content, list) else []
            if isinstance(message.content, str) and message.content:
                block = {"type": "text", "text": message.content}
                if message.cache_control is not None:
                    block["cache_control"] = message.cache_control
                blocks.append(block)
            blocks.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls)
            result.append(payload_type(role="assistant", content=blocks))
        else:
            if isinstance(message.content, list):
                content = content_parts_to_blocks(message.content)
            else:
                block = {"type": "text", "text": message.content or ""}
                if message.cache_control is not None:
                    block["cache_control"] = message.cache_control
                content = [block]
            result.append(payload_type(role=message.role, content=content))
        pending_tool_result_index = None
    return system_blocks, result


def parse_response(response: AnthropicResponse) -> LLMResponse:
    text_blocks: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text" and block.text:
            text_blocks.append(block.text)
        elif block.type == "tool_use" and block.id and block.name:
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input or {}))
    prompt_tokens = completion_tokens = total_tokens = None
    cache_read = cache_write = 0
    usage_available = response.usage is not None
    if response.usage is not None:
        cache_read = response.usage.cache_read_input_tokens or 0
        cache_write = response.usage.cache_creation_input_tokens or 0
        prompt_tokens = (response.usage.input_tokens or 0) + cache_read + cache_write
        completion_tokens = response.usage.output_tokens
        total_tokens = prompt_tokens + (completion_tokens or 0)
    return LLMResponse(content="".join(text_blocks) or None, tool_calls=tool_calls, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens, usage_available=usage_available, cache_read_tokens=cache_read, cache_write_tokens=cache_write)
