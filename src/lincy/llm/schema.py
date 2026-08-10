"""Pydantic models for LLM request/response schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# === Exceptions ===
class MalformedFunctionCallError(RuntimeError):
    """LLM returned a malformed function call; retryable."""


class ContextLengthExceededError(RuntimeError):
    """Prompt token count exceeds the model's context length limit; not retryable."""


def raise_if_context_length_error(
    exc: Any,
    *,
    patterns: tuple[str, ...],
) -> None:
    """Translate provider-specific context-limit 400 responses."""
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 400:
        return
    body = response.text
    if any(pattern.lower() in body.lower() for pattern in patterns):
        raise ContextLengthExceededError(body) from None


# === Multimodal Content ===
class ContentPart(BaseModel):
    """A single part of multimodal message content."""

    type: Literal["text", "image"]
    text: str | None = None
    media_type: str | None = None  # e.g. "image/png"
    data: str | None = None  # base64-encoded image data
    width: int | None = None
    height: int | None = None
    cache_control: dict[str, str] | None = (
        None  # e.g. {"type": "ephemeral", "ttl": "1h"}
    )


# === Tool Definitions ===
class ToolParameter(BaseModel):
    """A parameter definition for a tool."""

    type: Literal["string", "number", "integer", "boolean", "object", "array"]
    description: str
    enum: list[str] | None = None
    items: dict[str, Any] | None = None  # JSON Schema items for array type
    json_schema: dict[str, Any] | None = None


class ToolDefinition(BaseModel):
    """A tool definition that can be passed to LLM."""

    name: str
    description: str
    parameters: dict[str, ToolParameter]
    required: list[str] = []

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema format for OpenAI/Anthropic."""
        properties: dict[str, Any] = {}
        for name, param in self.parameters.items():
            if param.json_schema:
                prop = dict(param.json_schema)
                prop.setdefault("type", param.type)
                prop.setdefault("description", param.description)
                if param.enum and "enum" not in prop:
                    prop["enum"] = param.enum
            else:
                prop = {"type": param.type, "description": param.description}
                if param.enum:
                    prop["enum"] = param.enum
                if param.items:
                    prop["items"] = param.items
            properties[name] = prop

        return {
            "type": "object",
            "properties": properties,
            "required": self.required,
        }


class ToolCall(BaseModel):
    """A tool call made by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    provider_call_index: int | None = None
    provider_roundtrip: dict[str, Any] | None = None


class LLMResponse(BaseModel):
    """Unified response from LLM that may contain tool calls."""

    content: str | None = None
    reasoning_content: str | None = None
    reasoning_details: list[dict[str, Any]] | None = None  # Structured round-trip
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    usage_available: bool = False
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


# === Shared ===
class Message(BaseModel):
    """A message in a conversation."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[ContentPart] | None = None
    codex_compaction_encrypted_content: str | None = None
    reasoning_content: str | None = None  # Plain text for display
    reasoning_details: list[dict[str, Any]] | None = None  # Structured round-trip
    tool_calls: list[ToolCall] | None = None  # For assistant messages with tool calls
    tool_call_id: str | None = None  # For tool result messages
    name: str | None = None  # Tool name for tool result messages
    timestamp: datetime | None = None  # UTC timestamp when message was created
    # Provider-agnostic cache annotation.  The provider adapter reads this
    # during serialization (e.g. Anthropic wraps content in a content-block
    # with cache_control).  ContextBuilder sets it; content type stays str.
    cache_control: dict[str, str] | None = None


class CopilotNativeRequest(BaseModel):
    """Native internal request sent to the local Copilot proxy."""

    model: str
    messages: list[Message]
    max_tokens: int | None = None
    tools: list[ToolDefinition] | None = None
    response_schema: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    initiator: Literal["user", "agent"]
    interaction_id: str | None = None
    interaction_type: Literal["conversation-agent", "conversation-subagent"] | None = (
        None
    )
    request_id: str | None = None


class CodexNativeRequest(BaseModel):
    """Native internal request sent to the local Codex proxy."""

    model: str
    messages: list[Message]
    max_output_tokens: int | None = None
    prompt_cache_key: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    tools: list[ToolDefinition] | None = None
    response_schema: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None


class CodexCompactRequest(BaseModel):
    """Native internal request sent to the local Codex compact proxy."""

    model: str
    messages: list[Message]
    session_id: str | None = None
    turn_id: str | None = None
    tools: list[ToolDefinition] | None = None
    reasoning_effort: str | None = None


class CodexCompactResponse(BaseModel):
    """Compacted message history returned by the local Codex proxy."""

    messages: list[Message]


def make_tool_result_message(
    *,
    tool_call_id: str,
    name: str,
    content: str | list[ContentPart] | None,
    timestamp: datetime | None = None,
) -> Message:
    """Build a tool-result message with the required linkage fields."""
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        name=name,
        timestamp=timestamp,
    )


# === OpenAI ===
class OpenAIFunctionDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class OpenAITool(BaseModel):
    type: Literal["function"] = "function"
    function: OpenAIFunctionDef


class OpenAIToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: "OpenAIFunctionCall"


class OpenAIFunctionCall(BaseModel):
    name: str
    arguments: str  # JSON string


class OpenAIMessagePayload(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None
    reasoning: str | None = None  # Plain string fallback
    reasoning_content: str | None = None  # DeepSeek thinking round-trip
    reasoning_details: list[dict[str, Any]] | None = None  # Structured round-trip
    tool_calls: list[OpenAIToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class OpenAIRequest(BaseModel):
    model: str
    messages: list[OpenAIMessagePayload]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    tools: list[OpenAITool] | None = None
    reasoning_effort: str | None = None
    response_format: dict[str, Any] | None = None
    temperature: float | None = None


class OpenAIResponseMessage(BaseModel):
    content: str | None = None
    reasoning_content: str | None = Field(
        default=None,
        # OpenRouter Gemini returns "reasoning", DeepSeek/Qwen use "reasoning_content",
        # some proxies use "reasoning_text".
        validation_alias=AliasChoices(
            "reasoning_content", "reasoning", "reasoning_text"
        ),
        serialization_alias="reasoning_content",
    )
    reasoning_details: list[dict[str, Any]] | None = None
    tool_calls: list[OpenAIToolCall] | None = None


class OpenAIChoice(BaseModel):
    message: OpenAIResponseMessage
    finish_reason: str | None = None


class OpenAIPromptTokensDetails(BaseModel):
    """Cache metrics from OpenRouter/OpenAI usage response."""

    cached_tokens: int = 0
    cache_write_tokens: int = 0

    model_config = ConfigDict(extra="ignore")


class OpenAIUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: OpenAIPromptTokensDetails | None = None

    model_config = ConfigDict(extra="ignore")


class OpenAIResponse(BaseModel):
    choices: list[OpenAIChoice]
    usage: OpenAIUsage | None = None


# === Ollama Native ===
class OllamaNativeFunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class OllamaNativeTool(BaseModel):
    type: Literal["function"] = "function"
    function: OllamaNativeFunctionDef

    model_config = ConfigDict(extra="ignore")


class OllamaNativeFunctionCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    index: int | None = None

    model_config = ConfigDict(extra="allow")


class OllamaNativeToolCall(BaseModel):
    id: str | None = None
    thought_signature: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thought_signature", "thoughtSignature"),
        serialization_alias="thoughtSignature",
    )
    function: OllamaNativeFunctionCall

    model_config = ConfigDict(extra="allow")


class OllamaNativeMessagePayload(BaseModel):
    role: str
    content: str | None = None
    thinking: str | None = None
    images: list[str] | None = None
    tool_calls: list[OllamaNativeToolCall] | None = None
    tool_name: str | None = None

    model_config = ConfigDict(extra="ignore")


class OllamaNativeRequest(BaseModel):
    model: str
    messages: list[OllamaNativeMessagePayload]
    stream: bool = False
    tools: list[OllamaNativeTool] | None = None
    format: dict[str, Any] | Literal["json"] | None = None
    think: bool | Literal["low", "medium", "high", "xhigh", "max"] | None = None
    options: dict[str, Any] | None = None


class OllamaNativeResponse(BaseModel):
    message: OllamaNativeMessagePayload
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None

    model_config = ConfigDict(extra="ignore")


# === Anthropic ===
class AnthropicToolInputSchema(BaseModel):
    type: Literal["object"] = "object"
    properties: dict[str, Any]
    required: list[str] = []


class AnthropicTool(BaseModel):
    name: str
    description: str
    input_schema: AnthropicToolInputSchema


class AnthropicTextContent(BaseModel):
    type: Literal["text"] = "text"
    text: str
    cache_control: dict[str, str] | None = None


class AnthropicToolUseContent(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class AnthropicToolResultContent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str


AnthropicContent = (
    AnthropicTextContent | AnthropicToolUseContent | AnthropicToolResultContent
)


class AnthropicMessagePayload(BaseModel):
    role: str
    content: str | list[AnthropicContent | dict[str, Any]]


class AnthropicRequest(BaseModel):
    model: str
    messages: list[AnthropicMessagePayload]
    max_tokens: int
    system: str | None = None
    tools: list[AnthropicTool] | None = None


class ClaudeCodeMessagePayload(BaseModel):
    role: str
    content: str | list[dict[str, Any]]

    model_config = ConfigDict(extra="allow")


class ClaudeCodeRequest(BaseModel):
    model: str
    messages: list[ClaudeCodeMessagePayload]
    max_tokens: int
    system: str | list[str | dict[str, Any]] | None = None
    # Kept as raw dicts: the proxy must forward tools verbatim. Server tools
    # (e.g. advisor_20260301) have no description/input_schema, and typed
    # validation would also strip unknown JSON-schema fields upstream accepts.
    tools: list[dict[str, Any]] | None = None
    thinking: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop_sequences: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("stop_sequences", "stopSequences"),
        serialization_alias="stop_sequences",
    )

    model_config = ConfigDict(extra="allow")


class AnthropicContentBlock(BaseModel):
    type: str = "text"
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


class AnthropicUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    model_config = ConfigDict(extra="ignore")


class AnthropicResponse(BaseModel):
    content: list[AnthropicContentBlock]
    stop_reason: str | None = None
    usage: AnthropicUsage | None = None


# === Gemini ===
class GeminiFunctionDeclaration(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class GeminiToolConfig(BaseModel):
    function_declarations: list[GeminiFunctionDeclaration] = Field(
        validation_alias=AliasChoices(
            "function_declarations",
            "functionDeclarations",
        ),
        serialization_alias="functionDeclarations",
    )


class GeminiFunctionCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class GeminiFunctionResponse(BaseModel):
    name: str
    response: dict[str, Any]


class GeminiInlineData(BaseModel):
    """Inline binary data for Gemini multimodal requests."""

    mime_type: str = Field(
        validation_alias=AliasChoices("mime_type", "mimeType"),
        serialization_alias="mimeType",
    )
    data: str  # base64-encoded


class GeminiPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str | None = None
    inline_data: GeminiInlineData | None = Field(
        default=None,
        validation_alias=AliasChoices("inline_data", "inlineData"),
        serialization_alias="inlineData",
    )
    function_call: GeminiFunctionCall | None = Field(
        default=None,
        validation_alias=AliasChoices("function_call", "functionCall"),
        serialization_alias="functionCall",
    )
    function_response: GeminiFunctionResponse | None = Field(
        default=None,
        validation_alias=AliasChoices("function_response", "functionResponse"),
        serialization_alias="functionResponse",
    )
    thought_signature: str | None = Field(
        default=None,
        validation_alias=AliasChoices("thought_signature", "thoughtSignature"),
        serialization_alias="thoughtSignature",
    )


class GeminiContent(BaseModel):
    role: str | None = None
    parts: list[GeminiPart] = Field(default_factory=list)


class GeminiSystemInstruction(BaseModel):
    parts: list[GeminiPart] = Field(default_factory=list)


class GeminiRequest(BaseModel):
    contents: list[GeminiContent]
    system_instruction: GeminiSystemInstruction | None = None
    tools: list[GeminiToolConfig] | None = None


class GeminiCandidate(BaseModel):
    content: GeminiContent = Field(default_factory=GeminiContent)
    finish_reason: str | None = Field(
        default=None,
        validation_alias=AliasChoices("finish_reason", "finishReason"),
        serialization_alias="finishReason",
    )
    finish_message: str | None = Field(
        default=None,
        validation_alias=AliasChoices("finish_message", "finishMessage"),
        serialization_alias="finishMessage",
    )


class GeminiUsageMetadata(BaseModel):
    prompt_token_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("prompt_token_count", "promptTokenCount"),
        serialization_alias="promptTokenCount",
    )
    candidates_token_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("candidates_token_count", "candidatesTokenCount"),
        serialization_alias="candidatesTokenCount",
    )
    total_token_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("total_token_count", "totalTokenCount"),
        serialization_alias="totalTokenCount",
    )

    model_config = ConfigDict(extra="ignore")


class GeminiResponse(BaseModel):
    candidates: list[GeminiCandidate]
    usage_metadata: GeminiUsageMetadata | None = Field(
        default=None,
        validation_alias=AliasChoices("usage_metadata", "usageMetadata"),
        serialization_alias="usageMetadata",
    )
