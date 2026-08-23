"""LLM provider config models.

One config class per provider, each owning its own reasoning/thinking field
shape, validation, and client construction -- see
docs/dev/provider-architecture.md and docs/dev/provider-api-spec.md.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from .schema_base import StrictConfigModel


# === Provider-specific reasoning/thinking configs ===
# Each provider has its own reasoning field type, matching its real API format.
# No shared ReasoningConfig -- see docs/dev/provider-api-spec.md for API facts.


class LLMProviderConfig(StrictConfigModel):
    """Shared provider config helpers."""

    vision: bool = False

    def supports_response_schema(self) -> bool:
        """Whether this adapter exposes native structured outputs."""
        return False

    def get_vision(self) -> bool:
        return self.vision

    @staticmethod
    def _clean_base_url(
        value: str,
        *,
        forbidden: list[tuple[tuple[str, ...], str]],
    ) -> str:
        """Trim a base_url and reject provider-specific endpoint suffixes."""
        trimmed = value.strip().rstrip("/")
        if not trimmed:
            raise ValueError("base_url must not be empty")
        for suffixes, message in forbidden:
            if trimmed.endswith(suffixes):
                raise ValueError(message)
        return trimmed

    def _validate_effort_reasoning(
        self,
        reasoning: Any,
        *,
        source_path: Path,
        supported_efforts: list[str] | None,
    ) -> Any:
        """Normalize and validate common enabled/effort reasoning fields."""
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        enabled = reasoning.enabled
        if reasoning.effort is not None and enabled is None:
            reasoning = reasoning.model_copy(update={"enabled": True})
            enabled = True
        if enabled is False and reasoning.effort is not None:
            raise ValueError(
                "reasoning.effort cannot be set when enabled is false " + ctx
            )
        if (
            reasoning.effort is not None
            and supported_efforts is not None
            and reasoning.effort not in supported_efforts
        ):
            allowed = ", ".join(supported_efforts) or "(none)"
            raise ValueError(
                f"reasoning.effort={reasoning.effort!r} is not supported "
                f"(supported_efforts={allowed}) {ctx}"
            )
        return reasoning


class OllamaNativeToggleThinkingConfig(StrictConfigModel):
    """Ollama native thinking toggle.

    Maps to think=true / think=false in the native /api/chat payload.
    """

    mode: Literal["toggle"]
    enabled: bool


class OllamaNativeEffortThinkingConfig(StrictConfigModel):
    """Ollama native effort mode.

    Maps to native /api/chat string `think` levels.
    """

    mode: Literal["effort"]
    effort: Literal["low", "medium", "high", "xhigh", "max"]


OllamaNativeThinkingConfig = Annotated[
    OllamaNativeToggleThinkingConfig | OllamaNativeEffortThinkingConfig,
    Field(discriminator="mode"),
]


class OllamaNativeConfig(LLMProviderConfig):
    """Ollama native provider configuration."""

    provider: Literal["ollama"] = "ollama"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "http://localhost:11434"
    max_tokens: int | None = Field(default=None, ge=1)
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = Field(default=None, ge=0.0)
    thinking: OllamaNativeThinkingConfig

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/chat/completions"),
                "Ollama native base_url must point to the host root or /api, not /v1",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "OllamaNativeConfig":
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        is_gpt_oss = self.model.startswith("gpt-oss:")
        if is_gpt_oss and self.thinking.mode != "effort":
            raise ValueError(
                "gpt-oss models require thinking.mode=effort in Ollama native profiles "
                + ctx
            )
        return self

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self) -> Any:
        from ..llm.providers.ollama_native import OllamaNativeClient

        return OllamaNativeClient(self)


class CopilotReasoningConfig(StrictConfigModel):
    """Copilot reasoning config.

    GitHub Copilot upstream /chat/completions uses top-level
    reasoning_effort (historical/empirical behavior).
    Endpoint and payload format remain reverse-engineered.
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool | None = None
    effort: str | None = None
    supported_efforts: list[str] = Field(default_factory=list)


class CopilotConfig(LLMProviderConfig):
    """Copilot proxy (native internal API, no auth)."""

    provider: Literal["copilot"] = "copilot"
    model: str
    base_url: str = "http://localhost:4141"
    max_tokens: int | None = None
    request_timeout: float | None = None
    temperature: float | None = None
    reasoning: CopilotReasoningConfig | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/chat/completions"),
                "Copilot base_url must point to the native proxy root, not /v1",
            ),
            (
                ("/chat",),
                "Copilot base_url must point to the proxy root; the client appends /chat",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "CopilotConfig":
        if self.reasoning is None:
            return self
        reasoning = self._validate_effort_reasoning(
            self.reasoning,
            source_path=source_path,
            supported_efforts=self.reasoning.supported_efforts,
        )
        return self.model_copy(update={"reasoning": reasoning})

    def supports_response_schema(self) -> bool:
        return True

    def create_client(
        self,
        *,
        dispatch_mode: str = "first_user_then_agent",
    ) -> Any:
        from ..llm.providers.copilot import CopilotClient

        return CopilotClient(
            self,
            dispatch_mode=dispatch_mode,
        )


class CodexReasoningConfig(StrictConfigModel):
    """Codex reasoning config.

    ChatGPT Codex backend accepts Responses-style reasoning payload:
    {"effort": "...", "summary": "auto"}.
    Endpoint and payload format remain reverse-engineered.
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool | None = None
    effort: str | None = None
    supported_efforts: list[str] = Field(default_factory=list)


class CodexConfig(LLMProviderConfig):
    """Codex proxy (native internal API via local OAuth proxy)."""

    provider: Literal["codex"] = "codex"
    model: str
    base_url: str = "http://localhost:4143"
    max_tokens: int | None = None
    request_timeout: float | None = None
    temperature: float | None = None
    reasoning: CodexReasoningConfig | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/responses"),
                "Codex base_url must point to the native proxy root, not /v1",
            ),
            (
                ("/chat",),
                "Codex base_url must point to the proxy root; the client appends /chat",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "CodexConfig":
        if self.reasoning is None:
            return self
        reasoning = self._validate_effort_reasoning(
            self.reasoning,
            source_path=source_path,
            supported_efforts=self.reasoning.supported_efforts,
        )
        return self.model_copy(update={"reasoning": reasoning})

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self, **kwargs: Any) -> Any:
        from ..llm.providers.codex import CodexClient

        return CodexClient(self, **kwargs)


class ClaudeCodeAdaptiveThinkingConfig(StrictConfigModel):
    """Claude Code adaptive thinking config.

    Maps to thinking: {"type": "adaptive"}.
    """

    type: Literal["adaptive"]


class ClaudeCodeEnabledThinkingConfig(StrictConfigModel):
    """Claude Code manual thinking config.

    Maps to thinking: {"type": "enabled", "budget_tokens": N}.
    budget_tokens is optional because Claude Code may let the API pick the
    default budget for non-adaptive models.
    """

    type: Literal["enabled"]
    budget_tokens: int | None = Field(default=None, gt=0)


class ClaudeCodeDisabledThinkingConfig(StrictConfigModel):
    """Claude Code disabled thinking config.

    Maps to thinking: {"type": "disabled"}.
    """

    type: Literal["disabled"]


ClaudeCodeThinkingConfig = Annotated[
    ClaudeCodeAdaptiveThinkingConfig
    | ClaudeCodeEnabledThinkingConfig
    | ClaudeCodeDisabledThinkingConfig,
    Field(discriminator="type"),
]


# Upstream effort support per Claude model family. Only families that reject
# part of the effort ladder are listed; anything unlisted passes through, so a
# new model id is never blocked at load time. See docs/dev/provider-api-spec.md.
_CLAUDE_MODEL_EFFORTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("haiku-4-5", frozenset()),
    ("sonnet-4-5", frozenset()),
    ("opus-4-5", frozenset({"low", "medium", "high"})),
    ("opus-4-6", frozenset({"low", "medium", "high", "max"})),
    ("sonnet-4-6", frozenset({"low", "medium", "high", "max"})),
)

# Families that only accept thinking.type=disabled at effort high or below.
_CLAUDE_CODE_DISABLED_THINKING_EFFORT_CAPPED = ("opus-5",)


class ClaudeCodeOutputConfig(StrictConfigModel):
    """Claude Code output_config block."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def validate_non_empty(self) -> "ClaudeCodeOutputConfig":
        if self.effort is None:
            raise ValueError("output_config must set at least one field")
        return self


class ClaudeCodeConfig(LLMProviderConfig):
    """Claude Code proxy (native Claude Messages API via local proxy)."""

    provider: Literal["claude_code"] = "claude_code"
    model: str
    base_url: str = "http://localhost:4142"
    max_tokens: int = 4096
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    thinking: ClaudeCodeThinkingConfig | None = None
    output_config: ClaudeCodeOutputConfig | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/messages"),
                "Claude Code base_url must point to the proxy root, not /v1 or /v1/messages",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "ClaudeCodeConfig":
        effort = self.output_config.effort if self.output_config else None
        if effort is None:
            return self

        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        model = self.model.lower()
        for family, allowed in _CLAUDE_MODEL_EFFORTS:
            if family not in model:
                continue
            if effort not in allowed:
                supported = ", ".join(sorted(allowed)) if allowed else "none"
                raise ValueError(
                    f"output_config.effort={effort} is not supported upstream by this "
                    f"model (supported: {supported}) " + ctx
                )
            break

        thinking_disabled = (
            self.thinking is not None and self.thinking.type == "disabled"
        )
        if (
            thinking_disabled
            and effort in ("xhigh", "max")
            and any(f in model for f in _CLAUDE_CODE_DISABLED_THINKING_EFFORT_CAPPED)
        ):
            raise ValueError(
                f"thinking.type=disabled only works at effort high or below on this "
                f"model, got effort={effort} " + ctx
            )
        return self

    def create_client(self) -> Any:
        from ..llm.providers.claude_code import ClaudeCodeClient

        return ClaudeCodeClient(self)


class GrokReasoningConfig(StrictConfigModel):
    """Grok Chat Completions reasoning config.

    xAI Chat Completions uses top-level reasoning_effort string.
    Responses API uses reasoning: {"effort": ...} object -- not used here.
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool | None = None
    effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None
    supported_efforts: list[str] = Field(default_factory=list)


class GrokConfig(LLMProviderConfig):
    """Grok via local SuperGrok OAuth proxy (OpenAI-compatible chat completions).

    Auth is handled by grok-proxy (device-code OAuth). The client only talks
    to the local proxy; no XAI_API_KEY is required on this path.
    See docs/dev/provider-api-spec.md.
    """

    provider: Literal["grok"] = "grok"
    model: str
    base_url: str = "http://localhost:4144/v1"
    max_tokens: int | None = Field(default=None, ge=1)
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = Field(default=None, ge=0.0)
    vision: bool = True
    reasoning: GrokReasoningConfig | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/chat/completions",),
                "Grok base_url must point to the proxy /v1 root; "
                "the client appends /chat/completions",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "GrokConfig":
        if self.reasoning is None:
            return self
        reasoning = self._validate_effort_reasoning(
            self.reasoning,
            source_path=source_path,
            supported_efforts=self.reasoning.supported_efforts or None,
        )
        return self.model_copy(update={"reasoning": reasoning})

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self, **kwargs: Any) -> Any:
        from ..llm.providers.grok import GrokClient

        return GrokClient(self, **kwargs)


class OpenAIReasoningConfig(StrictConfigModel):
    """OpenAI Chat Completions reasoning config.

    Chat Completions API uses reasoning_effort (top-level string field).
    Responses API uses reasoning: {"effort": ...} object -- NOT used here.
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # max_tokens not supported by OpenAI Chat Completions for reasoning


class OpenAICapabilities(StrictConfigModel):
    reasoning: "OpenAIReasoningCapabilities"


class OpenAIReasoningCapabilities(StrictConfigModel):
    supports_toggle: bool
    supported_efforts: list[str] = Field(default_factory=list)
    supports_max_tokens: bool


class OpenAIConfig(LLMProviderConfig):
    """OpenAI provider configuration."""

    provider: Literal["openai"] = "openai"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "https://api.openai.com/v1"
    max_tokens: int = 4096
    use_max_completion_tokens: bool = False
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    reasoning: OpenAIReasoningConfig | None = None
    capabilities: OpenAICapabilities | None = None
    provider_overrides: dict[str, Any] | None = None

    def validate_reasoning(self, *, source_path: Path) -> "OpenAIConfig":
        reasoning = self.reasoning
        if reasoning is None:
            return self
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        if self.capabilities is None:
            raise ValueError(
                "reasoning is configured but capabilities.reasoning is missing " + ctx
            )
        caps = self.capabilities.reasoning
        reasoning = self._validate_effort_reasoning(
            reasoning,
            source_path=source_path,
            supported_efforts=None,
        )
        if reasoning.enabled is not None and not caps.supports_toggle:
            raise ValueError(
                "reasoning.enabled is set, but supports_toggle=false " + ctx
            )
        # OpenAI adapter constraints
        overrides = self.provider_overrides or {}
        if (
            reasoning.enabled is False
            and overrides.get("openai_reasoning_effort") is None
        ):
            raise ValueError(
                "OpenAI Chat Completions does not support reasoning.enabled=false "
                "without provider_overrides.openai_reasoning_effort " + ctx
            )
        return self.model_copy(update={"reasoning": reasoning})

    def get_vision(self) -> bool:
        return bool(self.capabilities and self.capabilities.vision)

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self, **kwargs: Any) -> Any:
        from ..llm.providers.openai import OpenAIClient

        return OpenAIClient(self, **kwargs)


class DeepSeekThinkingConfig(StrictConfigModel):
    """DeepSeek OpenAI-format thinking config.

    Maps to thinking: {"type": "enabled"|"disabled"} plus optional
    reasoning_effort when thinking is enabled.
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool
    effort: Literal["high", "max"] | None = None


class DeepSeekConfig(LLMProviderConfig):
    """DeepSeek provider configuration.

    Uses DeepSeek's OpenAI-compatible /chat/completions endpoint with
    provider-specific thinking controls.
    See docs/dev/provider-api-spec.md.
    """

    provider: Literal["deepseek"] = "deepseek"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "https://api.deepseek.com"
    max_tokens: int | None = Field(default=None, gt=0)
    request_timeout: float = Field(default=600.0, gt=0)
    temperature: float | None = Field(default=None, ge=0.0)
    thinking: DeepSeekThinkingConfig

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/chat/completions"),
                "DeepSeek base_url must point to the API root, "
                "not /v1 or /chat/completions",
            ),
        ])

    def validate_reasoning(self, *, source_path: Path) -> "DeepSeekConfig":
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        if self.vision:
            raise ValueError("DeepSeek vision is not supported by this adapter " + ctx)
        if self.thinking.enabled:
            if self.thinking.effort is None:
                raise ValueError(
                    "thinking.effort is required when thinking is enabled " + ctx
                )
            if self.temperature is not None:
                raise ValueError(
                    "temperature is not supported when DeepSeek thinking is enabled "
                    + ctx
                )
        elif self.thinking.effort is not None:
            raise ValueError(
                "thinking.effort cannot be set when thinking is disabled " + ctx
            )
        return self

    def create_client(self) -> Any:
        from ..llm.providers.deepseek import DeepSeekClient

        return DeepSeekClient(self)


class AnthropicAdaptiveThinkingConfig(StrictConfigModel):
    """Anthropic adaptive thinking config."""

    type: Literal["adaptive"]


class AnthropicEnabledThinkingConfig(StrictConfigModel):
    """Anthropic manual extended thinking config."""

    type: Literal["enabled"]
    budget_tokens: int | None = Field(default=None, ge=1024)


class AnthropicDisabledThinkingConfig(StrictConfigModel):
    """Anthropic disabled thinking config."""

    type: Literal["disabled"]


AnthropicThinkingConfig = Annotated[
    AnthropicAdaptiveThinkingConfig
    | AnthropicEnabledThinkingConfig
    | AnthropicDisabledThinkingConfig,
    Field(discriminator="type"),
]


class AnthropicOutputConfig(StrictConfigModel):
    """Anthropic output_config block."""

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def validate_non_empty(self) -> "AnthropicOutputConfig":
        if self.effort is None:
            raise ValueError("output_config must set at least one field")
        return self


class AnthropicConfig(LLMProviderConfig):
    """Anthropic provider configuration using native Messages API fields."""

    provider: Literal["anthropic"] = "anthropic"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "https://api.anthropic.com"
    max_tokens: int = 4096
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    thinking: AnthropicThinkingConfig | None = None
    output_config: AnthropicOutputConfig | None = None

    def validate_reasoning(self, *, source_path: Path) -> "AnthropicConfig":
        effort = self.output_config.effort if self.output_config else None
        if effort is None:
            return self

        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        model = self.model.lower()
        for family, allowed in _CLAUDE_MODEL_EFFORTS:
            if family not in model:
                continue
            if effort not in allowed:
                supported = ", ".join(sorted(allowed)) if allowed else "none"
                raise ValueError(
                    f"output_config.effort={effort} is not supported upstream by this "
                    f"model (supported: {supported}) " + ctx
                )
            break

        thinking_disabled = (
            self.thinking is not None and self.thinking.type == "disabled"
        )
        if thinking_disabled and effort in ("xhigh", "max") and "opus-5" in model:
            raise ValueError(
                f"thinking.type=disabled only works at effort high or below on this "
                f"model, got effort={effort} " + ctx
            )
        return self

    def create_client(self) -> Any:
        from ..llm.providers.anthropic import AnthropicClient

        return AnthropicClient(self)


class HeyrouteConfig(AnthropicConfig):
    """Heyroute gateway using the assumed Anthropic-compatible Messages API."""

    provider: Literal["heyroute"] = "heyroute"
    api_key_env: str | None = "HEYROUTE_API_KEY"
    base_url: str = Field(
        default="https://heyroute.ai/",
        validate_default=True,
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/messages"),
                "Heyroute base_url must point to the gateway root, "
                "not /v1 or /v1/messages",
            ),
        ])

    def create_client(self) -> Any:
        from ..llm.providers.heyroute import HeyrouteClient

        return HeyrouteClient(self)


class KanoProxyConfig(AnthropicConfig):
    """Kano Proxy gateway using the assumed Anthropic-compatible Messages API."""

    provider: Literal["kano_proxy"] = "kano_proxy"
    api_key_env: str | None = "KANO_PROXY_API_KEY"
    base_url: str = Field(
        default="https://kano-proxy.yuufeng.com/anthropic",
        validate_default=True,
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return cls._clean_base_url(value, forbidden=[
            (
                ("/v1", "/v1/messages"),
                "Kano Proxy base_url must point to the gateway root, "
                "not /v1 or /v1/messages",
            ),
        ])

    def create_client(self) -> Any:
        from ..llm.providers.kano_proxy import KanoProxyClient

        return KanoProxyClient(self)


class GeminiThinkingConfig(StrictConfigModel):
    """Gemini thinking config.

    Gemini 3 uses thinkingLevel and Gemini 2.5 uses thinkingBudget. The
    adapter currently maps low, medium, and high effort levels only.
    """

    enabled: bool | None = None
    effort: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)


class GeminiCapabilities(StrictConfigModel):
    reasoning: "GeminiReasoningCapabilities"


class GeminiReasoningCapabilities(StrictConfigModel):
    supports_toggle: bool
    supported_efforts: list[str] = Field(default_factory=list)
    supports_max_tokens: bool


class GeminiConfig(LLMProviderConfig):
    """Gemini provider configuration.

    See GeminiThinkingConfig docstring and docs/dev/provider-api-spec.md.
    """

    provider: Literal["gemini"] = "gemini"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "https://generativelanguage.googleapis.com"
    max_tokens: int = 8192
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    reasoning: GeminiThinkingConfig | None = None
    capabilities: GeminiCapabilities | None = None
    provider_overrides: dict[str, Any] | None = None

    def validate_reasoning(self, *, source_path: Path) -> "GeminiConfig":
        reasoning = self.reasoning
        if reasoning is None:
            return self
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        if self.capabilities is None:
            raise ValueError(
                "reasoning is configured but capabilities.reasoning is missing " + ctx
            )
        caps = self.capabilities.reasoning
        reasoning = self._validate_effort_reasoning(
            reasoning,
            source_path=source_path,
            supported_efforts=caps.supported_efforts,
        )
        if reasoning.enabled is False and reasoning.max_tokens is not None:
            raise ValueError(
                "reasoning.max_tokens cannot be set when enabled is false " + ctx
            )
        if reasoning.enabled is not None and not caps.supports_toggle:
            raise ValueError(
                "reasoning.enabled is set, but supports_toggle=false " + ctx
            )
        if reasoning.max_tokens is not None and not caps.supports_max_tokens:
            raise ValueError(
                "reasoning.max_tokens is set, but supports_max_tokens=false " + ctx
            )
        if reasoning.effort == "minimal":
            raise ValueError(
                "reasoning.effort=minimal is not supported by the Gemini adapter " + ctx
            )
        if reasoning.enabled is False and "gemini-3-pro" in self.model.lower():
            raise ValueError(
                "Gemini 3 Pro does not support reasoning.enabled=false " + ctx
            )
        return self.model_copy(update={"reasoning": reasoning})

    def get_vision(self) -> bool:
        return bool(self.capabilities and self.capabilities.vision)

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self) -> Any:
        from ..llm.providers.gemini import GeminiClient

        return GeminiClient(self)


class LiteLLMConfig(LLMProviderConfig):
    """LiteLLM proxy provider configuration (OpenAI-compatible).

    LiteLLM is a proxy that translates to various backend models.
    Reasoning is pass-through (no capabilities validation needed).
    """

    provider: Literal["litellm"] = "litellm"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "http://localhost:4000/v1"
    max_tokens: int | None = None
    request_timeout: float = Field(default=120.0, gt=0)
    temperature: float | None = None
    reasoning_effort: str | None = None

    def validate_reasoning(self, *, source_path: Path) -> "LiteLLMConfig":
        # Pass-through: LiteLLM handles backend-specific translation.
        return self

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self) -> Any:
        from ..llm.providers.litellm import LiteLLMClient

        return LiteLLMClient(self)


class OpenRouterReasoningConfig(StrictConfigModel):
    """OpenRouter reasoning config.

    Uses reasoning: {"effort": ...} object format.
    Effort and max_tokens are mutually exclusive (validated at config level).
    See docs/dev/provider-api-spec.md.
    """

    enabled: bool | None = None
    effort: str | None = None
    # OpenRouter requires minimum 1024 for reasoning max_tokens
    max_tokens: int | None = Field(default=None, ge=1024)
    supported_efforts: list[str] = Field(default_factory=list)


class OpenRouterProviderRoutingConfig(StrictConfigModel):
    """OpenRouter provider routing preferences.

    Maps to OpenRouter request payload:
    provider: {"order": [...], "ignore": [...], "require_parameters": bool,
               "allow_fallbacks": bool}
    """

    order: list[str] | None = None
    ignore: list[str] | None = None
    require_parameters: bool | None = None
    allow_fallbacks: bool | None = None

    @field_validator("order")
    @classmethod
    def validate_order(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("provider_routing.order must not be empty")
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("provider_routing.order entries must not be empty")
        return normalized

    @field_validator("ignore")
    @classmethod
    def validate_ignore(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("provider_routing.ignore must not be empty")
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("provider_routing.ignore entries must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_non_empty(self) -> "OpenRouterProviderRoutingConfig":
        if (
            self.order is None
            and self.allow_fallbacks is None
            and self.ignore is None
            and self.require_parameters is None
        ):
            raise ValueError("provider_routing must set at least one routing field")
        return self


class OpenRouterConfig(LLMProviderConfig):
    """OpenRouter provider configuration.

    See OpenRouterReasoningConfig docstring and docs/dev/provider-api-spec.md.
    """

    provider: Literal["openrouter"] = "openrouter"
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    max_tokens: int | None = Field(default=None, gt=0)
    request_timeout: float | None = Field(default=None, gt=0)
    temperature: float | None = None
    # Optional headers for OpenRouter leaderboard identification
    site_url: str | None = None  # HTTP-Referer header
    site_name: str | None = None  # X-OpenRouter-Title / X-Title headers
    reasoning: OpenRouterReasoningConfig | None = None
    verbosity: Literal["low", "medium", "high", "max"] | None = None
    provider_routing: OpenRouterProviderRoutingConfig | None = None

    def validate_reasoning(self, *, source_path: Path) -> "OpenRouterConfig":
        reasoning = self.reasoning
        if reasoning is None:
            return self
        ctx = f"(provider={self.provider}, model={self.model}, path={source_path})"
        reasoning = self._validate_effort_reasoning(
            reasoning,
            source_path=source_path,
            supported_efforts=reasoning.supported_efforts,
        )
        if reasoning.enabled is False and reasoning.max_tokens is not None:
            raise ValueError(
                "reasoning.max_tokens cannot be set when enabled is false " + ctx
            )
        # Mutual exclusivity: effort and max_tokens cannot both be set
        if reasoning.effort is not None and reasoning.max_tokens is not None:
            raise ValueError(
                "reasoning.effort and reasoning.max_tokens are mutually exclusive "
                + ctx
            )
        return self.model_copy(update={"reasoning": reasoning})

    def supports_response_schema(self) -> bool:
        return True

    def create_client(self) -> Any:
        from ..llm.providers.openrouter import OpenRouterClient

        return OpenRouterClient(self)


LLMConfig = Annotated[
    OllamaNativeConfig
    | CopilotConfig
    | CodexConfig
    | ClaudeCodeConfig
    | GrokConfig
    | OpenAIConfig
    | DeepSeekConfig
    | AnthropicConfig
    | HeyrouteConfig
    | KanoProxyConfig
    | GeminiConfig
    | OpenRouterConfig
    | LiteLLMConfig,
    Field(discriminator="provider"),
]
