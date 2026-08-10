"""DeepSeek provider client.

DeepSeek uses an OpenAI-compatible chat completions endpoint with
provider-specific thinking controls and usage fields.
See docs/dev/provider-api-spec.md.
"""

from typing import Any

from pydantic import BaseModel

from ...core.schema import DeepSeekConfig
from ..schema import (
    ContentPart,
    LLMResponse,
    Message,
    OpenAIMessagePayload,
    OpenAIRequest,
    OpenAIResponse,
    OpenAIUsage,
    ToolDefinition,
)
from .openai_compat import OpenAICompatibleClient, merge_leading_system_messages


class DeepSeekUsage(OpenAIUsage):
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None


class DeepSeekResponse(OpenAIResponse):
    usage: DeepSeekUsage | None = None


class DeepSeekThinkingPayload(BaseModel):
    type: str


class DeepSeekRequest(OpenAIRequest):
    thinking: DeepSeekThinkingPayload | None = None


def _map_thinking(config: DeepSeekConfig) -> tuple[dict[str, str], str | None]:
    if not config.thinking.enabled:
        return {"type": "disabled"}, None
    return {"type": "enabled"}, config.thinking.effort


class DeepSeekClient(OpenAICompatibleClient):
    response_type = DeepSeekResponse

    def __init__(self, config: DeepSeekConfig):
        if config.thinking.enabled and config.temperature is not None:
            raise ValueError(
                "temperature is not supported when DeepSeek thinking is enabled"
            )
        self.api_key = config.api_key
        self.thinking_payload, reasoning_effort = _map_thinking(config)
        super().__init__(
            model=config.model,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            request_timeout=config.request_timeout,
            reasoning_effort=reasoning_effort,
            temperature=config.temperature,
        )

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _convert_content_parts(parts: list[ContentPart]) -> list[dict[str, Any]]:
        if any(part.type == "image" for part in parts):
            raise ValueError("DeepSeek provider does not support image content")
        return OpenAICompatibleClient._convert_content_parts(parts)

    def _convert_messages(self, messages: list[Message]) -> list[OpenAIMessagePayload]:
        converted = super()._convert_messages(messages)
        rewritten: list[OpenAIMessagePayload] = []
        for msg in converted:
            updates: dict[str, Any] = {}
            reasoning_content = msg.reasoning_content
            if msg.reasoning is not None:
                reasoning_content = msg.reasoning
                updates["reasoning_content"] = reasoning_content
                updates["reasoning"] = None
            if msg.reasoning_details is not None:
                updates["reasoning_details"] = None
            if (
                self.thinking_payload["type"] == "enabled"
                and msg.role == "assistant"
                and msg.tool_calls
                and reasoning_content is None
            ):
                # DeepSeek requires the field when continuing from a tool result.
                updates["reasoning_content"] = ""
            rewritten.append(msg.model_copy(update=updates) if updates else msg)

        return merge_leading_system_messages(rewritten)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> DeepSeekRequest:
        if response_schema is not None:
            raise ValueError(
                "DeepSeek provider does not support response_schema; "
                "DeepSeek JSON Output only accepts response_format={type: json_object}"
            )
        if self.thinking_payload["type"] == "enabled" and temperature is not None:
            raise ValueError(
                "temperature is not supported when DeepSeek thinking is enabled"
            )
        request = super()._build_request(
            messages,
            tools=tools,
            response_schema=None,
            temperature=temperature,
        )
        payload = request.model_dump()
        payload["thinking"] = self.thinking_payload
        return DeepSeekRequest.model_validate(payload)

    def _parse_response(self, response: OpenAIResponse) -> LLMResponse:
        parsed = super()._parse_response(response)
        usage = response.usage
        if usage is None or usage.prompt_cache_hit_tokens is None:
            return parsed
        return parsed.model_copy(update={
            "cache_read_tokens": usage.prompt_cache_hit_tokens,
            "cache_write_tokens": 0,
        })
