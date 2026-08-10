"""Brain token and prompt-cache telemetry."""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LLMResponse

_READ_CACHE_MEASURABLE_PROVIDERS = frozenset({
    "anthropic", "claude_code", "codex", "copilot", "deepseek", "grok",
    "heyroute", "openai", "openrouter",
})


def is_read_cache_measurable(provider: str | None) -> bool:
    return provider in _READ_CACHE_MEASURABLE_PROVIDERS


@dataclass
class TurnTokenUsage:
    usage_available: bool = False
    max_prompt_tokens: int | None = None
    completion_tokens_for_max_prompt: int | None = None
    total_tokens_for_max_prompt: int | None = None
    cache_prompt_tokens_for_display: int | None = None
    cache_read_tokens_for_display: int = 0
    cache_write_tokens_for_display: int = 0
    saw_missing_usage: bool = False

    def record(self, response: LLMResponse) -> None:
        if not response.usage_available:
            self.saw_missing_usage = True
            return
        self.usage_available = True
        if response.prompt_tokens is None:
            return
        if self.max_prompt_tokens is None or response.prompt_tokens >= self.max_prompt_tokens:
            self.max_prompt_tokens = response.prompt_tokens
            self.completion_tokens_for_max_prompt = response.completion_tokens
            self.total_tokens_for_max_prompt = response.total_tokens
        if (
            self.cache_prompt_tokens_for_display is None
            or response.cache_read_tokens > self.cache_read_tokens_for_display
            or (
                response.cache_read_tokens == self.cache_read_tokens_for_display
                and response.prompt_tokens >= self.cache_prompt_tokens_for_display
            )
        ):
            self.cache_prompt_tokens_for_display = response.prompt_tokens
            self.cache_read_tokens_for_display = response.cache_read_tokens
            self.cache_write_tokens_for_display = response.cache_write_tokens


@dataclass
class LatestTokenStatus:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_prompt_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usage_available: bool = False
    missing_usage: bool = False


class TokenTelemetry:
    """Owns turn usage aggregation and toolbar status presentation."""

    def __init__(self, core) -> None:
        self._core = core
        self._low_cache_streak = 0

    def reset(self) -> None:
        self._core._turn_token_usage = TurnTokenUsage()

    def record(self, response: LLMResponse) -> None:
        self._core._turn_token_usage.record(response)

    def finalize(self) -> None:
        aggregate = self._core._turn_token_usage
        if aggregate.usage_available:
            self._core._latest_token_status = LatestTokenStatus(
                prompt_tokens=aggregate.max_prompt_tokens,
                completion_tokens=aggregate.completion_tokens_for_max_prompt,
                total_tokens=aggregate.total_tokens_for_max_prompt,
                cache_prompt_tokens=aggregate.cache_prompt_tokens_for_display,
                cache_read_tokens=aggregate.cache_read_tokens_for_display,
                cache_write_tokens=aggregate.cache_write_tokens_for_display,
                usage_available=True,
            )
            self._warn_low_cache_rate(aggregate)
        elif self._core._brain_provider == "copilot" and aggregate.saw_missing_usage:
            self._core._latest_token_status = LatestTokenStatus(missing_usage=True)

    def _warn_low_cache_rate(self, aggregate: TurnTokenUsage) -> None:
        prompt = aggregate.max_prompt_tokens
        if prompt is None or prompt < 10000:
            return
        if not is_read_cache_measurable(self._core._brain_provider):
            self._low_cache_streak = 0
            return
        brain_cfg = self._core.config.agents.get("brain")
        cache_cfg = getattr(brain_cfg, "cache", None) if brain_cfg else None
        if cache_cfg is None or not cache_cfg.enabled:
            return
        rate = aggregate.cache_read_tokens_for_display / prompt
        self._low_cache_streak = self._low_cache_streak + 1 if rate < 0.3 else 0
        if self._low_cache_streak >= 2:
            self._core.console.print_warning(
                f"Low cache hit rate for {self._low_cache_streak} consecutive turns: "
                f"{rate:.0%} (read={aggregate.cache_read_tokens_for_display:,} prompt={prompt:,})"
            )

    def status_text(self) -> str:
        limit = self._core._soft_max_prompt_tokens
        state = self._core._latest_token_status
        if state.usage_available and state.prompt_tokens is not None:
            percentage = state.prompt_tokens / limit * 100 if limit else 0
            suffix = " soft-over" if state.prompt_tokens > limit else ""
            if not is_read_cache_measurable(self._core._brain_provider):
                return f"tok {state.prompt_tokens:,}/{limit:,} ({percentage:.1f}%) cache unavailable{suffix}"
            cache_prompt = state.cache_prompt_tokens or state.prompt_tokens
            read_rate = state.cache_read_tokens / cache_prompt * 100 if cache_prompt else 0.0
            cache = f" cache r{state.cache_read_tokens:,}/{cache_prompt:,} ({read_rate:.1f}%)"
            if state.cache_write_tokens:
                cache += f" w{state.cache_write_tokens:,}"
            return f"tok {state.prompt_tokens:,}/{limit:,} ({percentage:.1f}%){cache}{suffix}"
        if state.missing_usage:
            return f"tok unavailable/{limit:,} (copilot no usage)"
        return f"tok --/{limit:,} (--.-%)"
