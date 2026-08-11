"""Base client for OpenAI-compatible chat completions APIs."""

import json
import logging
import re
from typing import Any

import httpx

from ..schema import (
    ContentPart, LLMResponse, Message, OpenAIFunctionCall, OpenAIFunctionDef,
    raise_if_context_length_error,
    OpenAIMessagePayload, OpenAIRequest, OpenAIResponse, OpenAITool,
    OpenAIToolCall, ToolCall, ToolDefinition, make_tool_result_message,
)
logger = logging.getLogger(__name__)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_CONTEXT_LENGTH_PATTERNS = ("max_prompt_tokens_exceeded", "context_length_exceeded")


def merge_leading_system_messages(messages: list[OpenAIMessagePayload]) -> list[OpenAIMessagePayload]:
    """Merge leading systems for OpenAI-compatible endpoints that reject multiples."""
    end = 0
    while end < len(messages) and messages[end].role == "system":
        end += 1
    if end <= 1:
        return messages
    text: list[str] = []
    for message in messages[:end]:
        if isinstance(message.content, str):
            text.append(message.content)
        elif isinstance(message.content, list):
            text.extend(part["text"] for part in message.content if isinstance(part, dict) and part.get("type") == "text")
    return [OpenAIMessagePayload(role="system", content="\n\n".join(text))] + messages[end:]


def repair_missing_tool_results(messages: list[Message], *, repair_names: bool, drop_orphans: bool) -> list[Message]:
    """Restore immediate tool results required by tool-call history protocols."""
    repaired: list[Message] = []
    idx = 0
    while idx < len(messages):
        message = messages[idx]
        repaired.append(message)
        if message.role != "assistant" or not message.tool_calls:
            idx += 1
            continue
        expected = {call.id: call.name for call in message.tool_calls if call.id}
        idx += 1
        while idx < len(messages) and messages[idx].role == "tool":
            tool_message = messages[idx]
            if repair_names and not tool_message.name and tool_message.tool_call_id in expected:
                tool_message = make_tool_result_message(
                    tool_call_id=tool_message.tool_call_id,
                    name=expected[tool_message.tool_call_id],
                    content=tool_message.content,
                    timestamp=tool_message.timestamp,
                )
            if tool_message.tool_call_id in expected:
                repaired.append(tool_message)
                expected.pop(tool_message.tool_call_id, None)
            elif drop_orphans:
                logger.warning("Dropping orphan or duplicate tool result: %s", tool_message.tool_call_id)
            else:
                repaired.append(tool_message)
            idx += 1
        repaired.extend(make_tool_result_message(tool_call_id=call_id, name=name, content="[Recovered missing tool result]") for call_id, name in expected.items())
    return repaired


def _repair_json_arguments(raw: str) -> dict[str, Any]:
    fixed = _TRAILING_COMMA_RE.sub(r"\1", raw)
    for candidate in (fixed, raw.replace("'", '"')):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    logger.warning("Could not repair tool call arguments: %s", raw[:200])
    return {"_raw_arguments": raw}


def _filter_empty_thinking(details: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not details:
        return details
    filtered = [detail for detail in details if not (detail.get("type", "").startswith("thinking") or detail.get("type") == "reasoning.text") or (detail.get("text") or "").strip()]
    return filtered or None


class OpenAICompatibleClient:
    """Base class for providers using OpenAI-compatible /chat/completions."""

    response_type = OpenAIResponse

    def __init__(self, *, model: str, base_url: str, max_tokens: int | None = None, max_completion_tokens: int | None = None, request_timeout: float, reasoning_effort: str | None = None, temperature: float | None = None):
        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.max_completion_tokens = max_completion_tokens
        self.request_timeout = request_timeout
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature

    def _get_headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[OpenAITool]:
        return [OpenAITool(function=OpenAIFunctionDef(name=tool.name, description=tool.description, parameters=tool.to_json_schema())) for tool in tools]

    @staticmethod
    def _convert_content_parts(parts: list[ContentPart]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for part in parts:
            if part.type == "text" and part.text is not None:
                item: dict[str, Any] = {"type": "text", "text": part.text}
                if part.cache_control is not None:
                    item["cache_control"] = part.cache_control
                result.append(item)
            elif part.type == "image" and part.data and part.media_type:
                result.append({"type": "image_url", "image_url": {"url": f"data:{part.media_type};base64,{part.data}"}})
        return result

    def _convert_messages(self, messages: list[Message]) -> list[OpenAIMessagePayload]:
        messages = repair_missing_tool_results(messages, repair_names=False, drop_orphans=True)
        result: list[OpenAIMessagePayload] = []
        pending_images: list[dict[str, Any]] = []
        for message in messages:
            if message.role != "tool" and pending_images:
                result.append(OpenAIMessagePayload(role="user", content=pending_images))
                pending_images = []
            if message.role == "tool":
                if isinstance(message.content, list):
                    result.append(OpenAIMessagePayload(role="tool", content="\n".join(part.text for part in message.content if part.type == "text" and part.text), tool_call_id=message.tool_call_id, name=message.name))
                    pending_images.extend(block for block in self._convert_content_parts(message.content) if block.get("type") == "image_url")
                else:
                    result.append(OpenAIMessagePayload(role="tool", content=message.content, tool_call_id=message.tool_call_id, name=message.name))
            elif message.role == "assistant" and message.tool_calls:
                calls = [OpenAIToolCall(id=call.id, function=OpenAIFunctionCall(name=call.name, arguments=json.dumps(call.arguments))) for call in message.tool_calls]
                result.append(OpenAIMessagePayload(role="assistant", content=message.content if isinstance(message.content, str) else "", reasoning=message.reasoning_content if not message.reasoning_details else None, reasoning_details=_filter_empty_thinking(message.reasoning_details), tool_calls=calls))
            elif isinstance(message.content, list):
                content = self._convert_content_parts(message.content)
                if message.cache_control and content:
                    content[-1]["cache_control"] = message.cache_control
                result.append(OpenAIMessagePayload(role=message.role, content=content))
            elif message.cache_control:
                result.append(OpenAIMessagePayload(role=message.role, content=[{"type": "text", "text": message.content or "", "cache_control": message.cache_control}]))
            else:
                result.append(OpenAIMessagePayload(role=message.role, content=message.content))
        if pending_images:
            result.append(OpenAIMessagePayload(role="user", content=pending_images))
        return result

    def _parse_response(self, response: OpenAIResponse) -> LLMResponse:
        content = None
        reasoning_parts: list[str] = []
        seen_reasoning: set[str] = set()
        reasoning_details = None
        tool_calls: list[ToolCall] = []
        finish_reason = None
        for choice in response.choices:
            message = choice.message
            if message.content and content is None:
                content = message.content
            if message.reasoning_content:
                chunk = message.reasoning_content.strip()
                if chunk and chunk not in seen_reasoning:
                    seen_reasoning.add(chunk)
                    reasoning_parts.append(chunk)
            if message.reasoning_details and reasoning_details is None:
                reasoning_details = message.reasoning_details
            if finish_reason is None:
                finish_reason = choice.finish_reason
            for call in message.tool_calls or []:
                try:
                    arguments = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    arguments = _repair_json_arguments(call.function.arguments)
                tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
        usage = response.usage
        details = usage.prompt_tokens_details if usage else None
        return LLMResponse(content=content, reasoning_content="\n\n".join(reasoning_parts) or None, reasoning_details=reasoning_details, tool_calls=tool_calls, finish_reason=finish_reason, prompt_tokens=usage.prompt_tokens if usage else None, completion_tokens=usage.completion_tokens if usage else None, total_tokens=usage.total_tokens if usage else None, usage_available=usage is not None, cache_read_tokens=details.cached_tokens if details else 0, cache_write_tokens=details.cache_write_tokens if details else 0)

    def _build_request(self, messages: list[Message], *, tools: list[ToolDefinition] | None = None, response_schema: dict[str, Any] | None = None, temperature: float | None = None) -> OpenAIRequest:
        request = OpenAIRequest(model=self.model, messages=self._convert_messages(messages), max_tokens=self.max_tokens, max_completion_tokens=self.max_completion_tokens, tools=self._convert_tools(tools) if tools else None, reasoning_effort=self.reasoning_effort, temperature=temperature if temperature is not None else self.temperature)
        if response_schema is not None:
            request.response_format = {"type": "json_schema", "json_schema": {"name": "response", "strict": False, "schema": response_schema}}
        return request

    def _do_post(self, request: OpenAIRequest) -> dict[str, Any]:
        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(f"{self.base_url}/chat/completions", headers=self._get_headers(), json=request.model_dump(exclude_none=True))
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_if_context_length_error(exc, patterns=_CONTEXT_LENGTH_PATTERNS)
                raise
            return response.json()

    def chat(self, messages: list[Message], response_schema: dict[str, Any] | None = None, temperature: float | None = None) -> str:
        return self._parse_response(self.response_type.model_validate(self._do_post(self._build_request(messages, response_schema=response_schema, temperature=temperature)))).content or ""

    def chat_with_tools(self, messages: list[Message], tools: list[ToolDefinition], temperature: float | None = None) -> LLMResponse:
        return self._parse_response(self.response_type.model_validate(self._do_post(self._build_request(messages, tools=tools, temperature=temperature))))
