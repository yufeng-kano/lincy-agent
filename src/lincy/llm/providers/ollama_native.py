"""Ollama native provider client.

Uses the native /api/chat endpoint with official think/tools/format/options
payloads. See docs/dev/provider-api-spec.md.
"""

from copy import deepcopy
import logging
from typing import Any

import httpx

from ...core.schema import OllamaNativeConfig
from ..schema import (
    ContentPart,
    LLMResponse,
    MalformedFunctionCallError,
    Message,
    OllamaNativeFunctionDef,
    OllamaNativeMessagePayload,
    OllamaNativeRequest,
    OllamaNativeResponse,
    OllamaNativeTool,
    OllamaNativeToolCall,
    ToolCall,
    ToolDefinition,
)
from ..schema import raise_if_context_length_error
from .openai_compat import repair_missing_tool_results

_CONTEXT_LENGTH_PATTERNS = ("context length", "context window", "max prompt tokens")

logger = logging.getLogger(__name__)


def _set_provider_field(
    payload: dict[str, Any],
    *,
    field_name: str,
    alias_name: str,
    value: Any,
) -> None:
    """Preserve the provider's original key spelling when possible."""
    if value is None:
        payload.pop(field_name, None)
        payload.pop(alias_name, None)
        return
    if field_name in payload and alias_name not in payload:
        payload[field_name] = value
        return
    payload[alias_name] = value


def _build_chat_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/api/chat"):
        return trimmed
    if trimmed.endswith("/api"):
        return f"{trimmed}/chat"
    return f"{trimmed}/api/chat"


def _map_thinking(config: OllamaNativeConfig) -> bool | str:
    thinking = config.thinking
    if thinking.mode == "toggle":
        return thinking.enabled
    return thinking.effort


_SYNTHETIC_TOOL_CONTEXT_NAMES = frozenset(
    {
        "read_startup_context",
        "_stage1_gather",
        "_load_skill_prerequisite",
        "_load_common_ground_at_message_time",
    }
)


def _can_roundtrip_assistant_thinking(tool_calls: list[ToolCall] | None) -> bool:
    """Return True when assistant tool history can safely keep thinking text.

    Gemini-backed Ollama rejects assistant history that replays `thinking`
    alongside function calls missing `thoughtSignature`. Some upstream responses
    omit the signature entirely, so the adapter must drop thinking on replay
    for those legacy/incomplete tool-call messages instead of re-sending an
    invalid combination.
    """
    if not tool_calls:
        return True
    return all(bool(tool_call.thought_signature) for tool_call in tool_calls)


def _is_synthetic_tool_history(tool_calls: list[ToolCall] | None) -> bool:
    if not tool_calls:
        return False
    return all(tool_call.name in _SYNTHETIC_TOOL_CONTEXT_NAMES for tool_call in tool_calls)


class OllamaNativeClient:
    def __init__(self, config: OllamaNativeConfig):
        self.model = config.model
        self.base_url = config.base_url
        self.api_key = config.api_key
        self.chat_url = _build_chat_url(config.base_url)
        self.max_tokens = config.max_tokens
        self.request_timeout = config.request_timeout
        self.temperature = config.temperature
        self.think = _map_thinking(config)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _convert_tools(self, tools: list[ToolDefinition]) -> list[OllamaNativeTool]:
        return [
            OllamaNativeTool(
                function=OllamaNativeFunctionDef(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.to_json_schema(),
                )
            )
            for tool in tools
        ]

    @staticmethod
    def _repair_missing_tool_results(messages: list[Message]) -> list[Message]:
        # Native Ollama history may include provider-generated orphan results;
        # preserve them because dropping them changes the upstream replay.
        return repair_missing_tool_results(
            messages,
            repair_names=True,
            drop_orphans=False,
        )

    @staticmethod
    def _split_content_parts(parts: list[ContentPart]) -> tuple[str | None, list[str]]:
        text_parts = [part.text for part in parts if part.type == "text" and part.text]
        images = [part.data for part in parts if part.type == "image" and part.data]
        text = "\n".join(text_parts) if text_parts else None
        return text, images

    def _convert_synthetic_tool_history(
        self,
        messages: list[Message],
        start_idx: int,
    ) -> tuple[list[OllamaNativeMessagePayload], int, list[str]]:
        """Rewrite synthetic tool context pairs into plain system text.

        These messages are runtime-authored context injections, not real provider
        tool history. Gemini-backed Ollama rejects them as native function-call
        replay, so flatten them at the adapter boundary while keeping the
        internal Conversation structure unchanged.
        """
        assistant_msg = messages[start_idx]
        payloads: list[OllamaNativeMessagePayload] = []
        pending_images: list[str] = []

        assistant_text: str | None
        assistant_images: list[str]
        if isinstance(assistant_msg.content, list):
            assistant_text, assistant_images = self._split_content_parts(
                assistant_msg.content
            )
        else:
            assistant_text = assistant_msg.content
            assistant_images = []
        if assistant_text:
            payloads.append(
                OllamaNativeMessagePayload(role="system", content=assistant_text)
            )
        pending_images.extend(assistant_images)

        idx = start_idx + 1
        while idx < len(messages):
            tool_msg = messages[idx]
            if tool_msg.role != "tool" or tool_msg.name not in _SYNTHETIC_TOOL_CONTEXT_NAMES:
                break

            tool_text: str | None
            tool_images: list[str]
            if isinstance(tool_msg.content, list):
                tool_text, tool_images = self._split_content_parts(tool_msg.content)
            else:
                tool_text = tool_msg.content
                tool_images = []
            if tool_text:
                payloads.append(
                    OllamaNativeMessagePayload(
                        role="system",
                        content=f"[Synthetic context: {tool_msg.name}]\n{tool_text}",
                    )
                )
            pending_images.extend(tool_images)
            idx += 1

        return payloads, idx, pending_images

    def _convert_messages(self, messages: list[Message]) -> list[OllamaNativeMessagePayload]:
        messages = self._repair_missing_tool_results(messages)
        result: list[OllamaNativeMessagePayload] = []
        pending_tool_images: list[str] = []

        idx = 0
        while idx < len(messages):
            message = messages[idx]
            if message.role != "tool" and pending_tool_images:
                result.append(
                    OllamaNativeMessagePayload(
                        role="user",
                        images=pending_tool_images,
                    )
                )
                pending_tool_images = []

            if (
                message.role == "assistant"
                and _is_synthetic_tool_history(message.tool_calls)
            ):
                synthetic_payloads, idx, synthetic_images = self._convert_synthetic_tool_history(
                    messages,
                    idx,
                )
                result.extend(synthetic_payloads)
                pending_tool_images.extend(synthetic_images)
                continue

            content: str | None
            images: list[str]
            if isinstance(message.content, list):
                content, images = self._split_content_parts(message.content)
            else:
                content = message.content
                images = []

            if message.role == "tool":
                if not message.name:
                    raise ValueError("Ollama native tool messages require Message.name")
                result.append(
                    OllamaNativeMessagePayload(
                        role="tool",
                        content=content or "",
                        tool_name=message.name,
                    )
                )
                pending_tool_images.extend(images)
                idx += 1
                continue

            payload = OllamaNativeMessagePayload(
                role=message.role,
                content=content,
                images=images or None,
            )
            if message.role == "assistant":
                if message.reasoning_content and _can_roundtrip_assistant_thinking(
                    message.tool_calls
                ):
                    payload.thinking = message.reasoning_content
                if message.tool_calls:
                    payload.tool_calls = [
                        self._build_roundtrip_tool_call_payload(tool_call)
                        for tool_call in message.tool_calls
                    ]
            result.append(payload)
            idx += 1

        if pending_tool_images:
            result.append(
                OllamaNativeMessagePayload(
                    role="user",
                    images=pending_tool_images,
                )
            )

        return result

    @staticmethod
    def _build_roundtrip_tool_call_payload(tool_call: ToolCall) -> OllamaNativeToolCall:
        raw = deepcopy(tool_call.provider_roundtrip) if tool_call.provider_roundtrip else {}
        if not isinstance(raw, dict):
            raw = {}

        raw["id"] = tool_call.id
        _set_provider_field(
            raw,
            field_name="thought_signature",
            alias_name="thoughtSignature",
            value=tool_call.thought_signature,
        )

        function = raw.get("function")
        if not isinstance(function, dict):
            function = {}
            raw["function"] = function
        function["name"] = tool_call.name
        function["arguments"] = tool_call.arguments
        if tool_call.provider_call_index is None:
            function.pop("index", None)
        else:
            function["index"] = tool_call.provider_call_index

        return OllamaNativeToolCall.model_validate(raw)

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> OllamaNativeRequest:
        options: dict[str, Any] = {}
        if self.max_tokens is not None:
            options["num_predict"] = self.max_tokens
        effective_temperature = temperature if temperature is not None else self.temperature
        if effective_temperature is not None:
            options["temperature"] = effective_temperature

        return OllamaNativeRequest(
            model=self.model,
            messages=self._convert_messages(messages),
            stream=False,
            tools=self._convert_tools(tools) if tools else None,
            format=response_schema,
            think=self.think,
            options=options or None,
        )

    def _do_post(self, request: OllamaNativeRequest) -> dict[str, Any]:
        with httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(
                self.chat_url,
                headers=self._get_headers(),
                json=request.model_dump(exclude_none=True, by_alias=True),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_if_context_length_error(exc, patterns=_CONTEXT_LENGTH_PATTERNS)
                raise
            return response.json()

    def _parse_response(self, response: OllamaNativeResponse) -> LLMResponse:
        tool_calls: list[ToolCall] = []
        for idx, tool_call in enumerate(response.message.tool_calls or []):
            name = tool_call.function.name.strip()
            if not name:
                raise MalformedFunctionCallError(
                    "MALFORMED_FUNCTION_CALL: Ollama returned a tool call with an empty name."
                )
            tool_calls.append(
                ToolCall(
                    id=tool_call.id or f"ollama-tool-{idx + 1}",
                    name=name,
                    arguments=tool_call.function.arguments,
                    thought_signature=tool_call.thought_signature,
                    provider_call_index=tool_call.function.index,
                    provider_roundtrip=tool_call.model_dump(exclude_none=True, by_alias=True),
                )
            )
        prompt_tokens = response.prompt_eval_count
        completion_tokens = response.eval_count
        usage_available = prompt_tokens is not None or completion_tokens is not None
        total_tokens = None
        if usage_available:
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

        return LLMResponse(
            content=response.message.content or None,
            reasoning_content=response.message.thinking or None,
            tool_calls=tool_calls,
            finish_reason=response.done_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_available=usage_available,
        )

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        request = self._build_request(
            messages,
            response_schema=response_schema,
            temperature=temperature,
        )
        data = self._do_post(request)
        response = OllamaNativeResponse.model_validate(data)
        return self._parse_response(response).content or ""

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        request = self._build_request(
            messages,
            tools=tools,
            temperature=temperature,
        )
        data = self._do_post(request)
        response = OllamaNativeResponse.model_validate(data)
        return self._parse_response(response)
