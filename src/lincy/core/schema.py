import re
from datetime import datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ..timezone_utils import validate_timezone_spec
from .schema_base import StrictConfigModel
from .provider_schema import (
    LLMProviderConfig as LLMProviderConfig,
    OllamaNativeToggleThinkingConfig as OllamaNativeToggleThinkingConfig,
    OllamaNativeEffortThinkingConfig as OllamaNativeEffortThinkingConfig,
    OllamaNativeThinkingConfig as OllamaNativeThinkingConfig,
    OllamaNativeConfig as OllamaNativeConfig,
    CopilotReasoningConfig as CopilotReasoningConfig,
    CopilotConfig as CopilotConfig,
    CodexReasoningConfig as CodexReasoningConfig,
    CodexConfig as CodexConfig,
    ClaudeCodeAdaptiveThinkingConfig as ClaudeCodeAdaptiveThinkingConfig,
    ClaudeCodeEnabledThinkingConfig as ClaudeCodeEnabledThinkingConfig,
    ClaudeCodeDisabledThinkingConfig as ClaudeCodeDisabledThinkingConfig,
    ClaudeCodeThinkingConfig as ClaudeCodeThinkingConfig,
    ClaudeCodeOutputConfig as ClaudeCodeOutputConfig,
    ClaudeCodeConfig as ClaudeCodeConfig,
    GrokReasoningConfig as GrokReasoningConfig,
    GrokConfig as GrokConfig,
    OpenAIReasoningConfig as OpenAIReasoningConfig,
    OpenAICapabilities as OpenAICapabilities,
    OpenAIReasoningCapabilities as OpenAIReasoningCapabilities,
    OpenAIConfig as OpenAIConfig,
    DeepSeekThinkingConfig as DeepSeekThinkingConfig,
    DeepSeekConfig as DeepSeekConfig,
    AnthropicAdaptiveThinkingConfig as AnthropicAdaptiveThinkingConfig,
    AnthropicEnabledThinkingConfig as AnthropicEnabledThinkingConfig,
    AnthropicDisabledThinkingConfig as AnthropicDisabledThinkingConfig,
    AnthropicThinkingConfig as AnthropicThinkingConfig,
    AnthropicOutputConfig as AnthropicOutputConfig,
    AnthropicConfig as AnthropicConfig,
    HeyrouteConfig as HeyrouteConfig,
    KanoProxyConfig as KanoProxyConfig,
    GeminiThinkingConfig as GeminiThinkingConfig,
    GeminiCapabilities as GeminiCapabilities,
    GeminiReasoningCapabilities as GeminiReasoningCapabilities,
    GeminiConfig as GeminiConfig,
    LiteLLMConfig as LiteLLMConfig,
    OpenRouterReasoningConfig as OpenRouterReasoningConfig,
    OpenRouterProviderRoutingConfig as OpenRouterProviderRoutingConfig,
    OpenRouterConfig as OpenRouterConfig,
    LLMConfig as LLMConfig,
)


class ShellConfig(StrictConfigModel):
    """Shell execution configuration."""

    blacklist: list[str] = []
    timeout: int = 30
    task_max_concurrency: int = Field(default=2, ge=1)
    export_env: list[str] = Field(default_factory=list)
    handoff: "ShellHandoffConfig" = Field(default_factory=lambda: ShellHandoffConfig())


class ShellHandoffRuleConfig(StrictConfigModel):
    """Deterministic shell handoff detection rule."""

    id: str
    outcome: Literal["waiting_external_action", "waiting_user_input"]
    any_text: list[str] = Field(default_factory=list)
    all_text: list[str] = Field(default_factory=list)
    require_url: bool = False
    prompt_suffix: list[str] = Field(default_factory=list)
    process_alive: bool | None = None
    idle_seconds_ge: float | None = Field(default=None, ge=0)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("id must not be empty")
        return cleaned

    @field_validator("any_text", "all_text")
    @classmethod
    def validate_regex_list(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not pattern.strip():
                raise ValueError("regex pattern must not be empty")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern '{pattern}': {exc}") from exc
        return value

    @field_validator("prompt_suffix")
    @classmethod
    def validate_prompt_suffix(cls, value: list[str]) -> list[str]:
        for suffix in value:
            if not suffix.strip():
                raise ValueError("prompt_suffix entries must not be empty")
        return value

    @model_validator(mode="after")
    def validate_matchers_present(self) -> "ShellHandoffRuleConfig":
        if (
            not self.any_text
            and not self.all_text
            and not self.require_url
            and not self.prompt_suffix
            and self.process_alive is None
            and self.idle_seconds_ge is None
        ):
            raise ValueError("handoff rule must define at least one matcher condition")
        return self


class ShellHandoffConfig(StrictConfigModel):
    """Shell handoff detection configuration."""

    enabled: bool = False
    tail_lines: int = Field(default=8, ge=1)
    grace_seconds: float = Field(default=1.5, ge=0)
    rules: list[ShellHandoffRuleConfig] = Field(default_factory=list)


class MemoryEditWarningBudgetConfig(StrictConfigModel):
    """Per-pattern character budget override for memory_edit warnings."""

    pattern: str
    max_chars: int = Field(ge=1000)


class MemoryEditWarningsConfig(StrictConfigModel):
    """File health warning configuration for memory_edit."""

    max_chars: int = Field(default=10000, ge=1000)
    budgets: list[MemoryEditWarningBudgetConfig] = Field(default_factory=list)
    ignore: list[str] = Field(
        default_factory=lambda: [
            "temp-memory.md",
            "index.md",
            "archive/",
        ]
    )


class MemoryEditToolConfig(StrictConfigModel):
    """Configuration for memory_edit tool."""

    allow_failure: bool = False
    turn_retry_limit: int = Field(default=3, ge=1)
    warnings: MemoryEditWarningsConfig = Field(default_factory=MemoryEditWarningsConfig)


class BM25SearchConfig(StrictConfigModel):
    """BM25 deterministic search configuration."""

    top_k: int = Field(default=8, ge=1)
    snippet_lines: int = Field(default=3, ge=0)
    max_snippets_per_file: int = Field(default=3, ge=1)
    max_response_chars: int = Field(default=2000, ge=100)
    date_normalization: bool = True
    exclude: list[str] = Field(default_factory=list)


class MemorySearchToolConfig(StrictConfigModel):
    """Configuration for memory_search tool."""

    bm25: BM25SearchConfig = Field(default_factory=BM25SearchConfig)


class WebSearchConfig(StrictConfigModel):
    """Configuration for external web_search tool."""

    enabled: bool = False
    timeout: float = Field(default=10.0, gt=0)
    api_key_env: str = "TAVILY_API_KEY"
    default_max_results: int = Field(default=5, ge=1)
    max_results_limit: int = Field(default=5, ge=1)
    include_raw_content: bool = False


class WebFetchConfig(StrictConfigModel):
    """Configuration for direct public URL fetching."""

    enabled: bool = False
    timeout: float = Field(default=60.0, gt=0)
    default_max_chars: int = Field(default=100_000, ge=200)
    max_response_chars: int = Field(default=100_000, ge=200)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    user_agent: str = "chat-agent-web-fetch/1.0"
    allow_private_hosts: bool = False
    summarize_with_llm: bool = True


class ScrollConfig(StrictConfigModel):
    """Scroll behavior configuration."""

    invert: bool = False
    max_amount: int = Field(default=5, ge=1)


class MemorySyncConfig(StrictConfigModel):
    """Side-channel memory sync frequency.

    Tracks consecutive turns without natural memory target updates.
    When the count reaches every_n_turns, forces a sync call.
    null = disabled (never force sync).
    """

    every_n_turns: int | None = Field(default=1, ge=1)
    max_retries: int = Field(default=1, ge=0)


_GovernanceScalar = str | int | float | bool


class GovernanceRule(StrictConfigModel):
    """One tool-governance rule declared in agent config."""

    skill: str
    tool: str
    when: dict[str, _GovernanceScalar] = Field(default_factory=dict)
    enforcement: Literal["advisory", "require_context"] = "require_context"


class SkillGovernanceConfig(StrictConfigModel):
    """Skill governance configuration."""

    external_skills_dir: str | None = "~/.agents/skills"
    rules: list[GovernanceRule] = Field(default_factory=list)


class AppleAppsContextSyncConfig(StrictConfigModel):
    """Deprecated compatibility shim for removed auto-sync behavior."""

    enabled: bool = False
    cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    calendar_window_hours: int = Field(default=36, ge=1, le=168)
    calendar_max_events: int = Field(default=5, ge=1, le=20)
    reminders_window_days: int = Field(default=7, ge=1, le=90)
    reminders_max_items: int = Field(default=6, ge=1, le=20)


class AppleAppsToolConfig(StrictConfigModel):
    """Configuration for macOS personal-app tools."""

    enabled: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_search_results: int = Field(default=25, ge=1, le=200)
    photos_export_dir: str = "tmp/photos-exports"
    mail_export_dir: str = "tmp/mail-attachments"
    context_sync: AppleAppsContextSyncConfig = Field(
        default_factory=AppleAppsContextSyncConfig
    )


class AgentNoteToolConfig(StrictConfigModel):
    """Configuration for agent_note tool guardrails.

    Limits apply to new writes only (create / batch_update with a new
    value); existing oversized values already in state/notes.json keep
    working and are surfaced via a warning instead of being rejected.
    """

    max_value_chars: int = Field(default=80, ge=1)
    max_notes: int = Field(default=12, ge=1)


class ToolsConfig(StrictConfigModel):
    """Tools configuration for agent capabilities."""

    max_tool_iterations: int = Field(default=10, ge=1)
    allowed_paths: list[str] = []
    shell: ShellConfig = Field(default_factory=ShellConfig)
    memory_edit: MemoryEditToolConfig = Field(default_factory=MemoryEditToolConfig)
    memory_search: MemorySearchToolConfig = Field(
        default_factory=MemorySearchToolConfig
    )
    apple_apps: AppleAppsToolConfig = Field(default_factory=AppleAppsToolConfig)
    web_fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    scroll: ScrollConfig = Field(default_factory=ScrollConfig)
    memory_sync: MemorySyncConfig = Field(default_factory=MemorySyncConfig)
    skill_governance: SkillGovernanceConfig = Field(
        default_factory=SkillGovernanceConfig
    )
    agent_note: AgentNoteToolConfig = Field(default_factory=AgentNoteToolConfig)


class CacheFingerprintConfig(StrictConfigModel):
    """Controls which content is included in the render-cache fingerprint.

    system_prompt is always included (no toggle).
    """

    boot_files: bool = False
    boot_files_as_tool: bool = False


class CacheConfig(StrictConfigModel):
    """Prompt caching for cost optimization."""

    enabled: bool = False
    # Supported project-wide TTL tokens. Provider adapters may clamp further
    # (e.g. Anthropic-style breakpoints max out at 1h).
    ttl: Literal["ephemeral", "1h", "24h"] = "ephemeral"
    fingerprint: CacheFingerprintConfig = Field(default_factory=CacheFingerprintConfig)


class AXServerConfig(StrictConfigModel):
    """OpenComputerUse MCP server settings for the AX-first GUI loop.

    repo/commit/binary_path default to the pinned values in
    lincy.gui.ax_runtime; set them only to override the vendored build.
    """

    repo: str | None = None
    commit: str | None = None
    binary_path: str | None = None
    keep_full_states: int = Field(default=2, ge=1)
    stale_text_max_chars: int = Field(default=2000, ge=200)
    max_tree_nodes: int | None = Field(default=None, ge=10)
    max_tree_depth: int | None = Field(default=None, ge=2)
    tool_timeout: float = Field(default=90.0, gt=0)


class AgentConfig(StrictConfigModel):
    """Agent configuration with LLM settings."""

    enabled: bool = True
    llm: LLMConfig
    llm_fallbacks: list[LLMConfig] = Field(default_factory=list)
    llm_fallback_cooldown_seconds: int = Field(default=1800, ge=0)
    llm_request_timeout: float | None = Field(default=None, gt=0)
    llm_transient_retries: int = Field(default=1, ge=0)
    llm_rate_limit_retries: int = Field(default=5, ge=0)
    # Memory searcher / editor specific
    pre_parse_retries: int = Field(default=1, ge=0)
    post_parse_retries: int = Field(default=1, ge=0)
    context_bytes_limit: int | None = Field(default=None, gt=0)
    max_results: int | None = Field(default=None, gt=0)
    enforce_memory_path_constraints: bool = True
    warn_on_failure: bool = True
    # GUI manager specific
    max_steps: int = Field(default=20, ge=1)
    allow_wait_tool: bool = True
    step_delay_min: float = Field(default=0.0, ge=0.0, le=10.0)
    step_delay_max: float = Field(default=0.0, ge=0.0, le=10.0)
    # GUI screenshot optimization
    screenshot_max_width: int | None = Field(default=1280, ge=256)
    screenshot_quality: int = Field(default=80, ge=10, le=100)
    # AX-first GUI backend (gui_manager only)
    ax: AXServerConfig = Field(default_factory=AXServerConfig)
    # Vision delegation: when True, vision-capable LLMs in the failover chain
    # read images themselves; non-vision candidates fall back to the vision
    # sub-agent. When False, always delegate image reading to the sub-agent.
    use_own_vision_ability: bool = False
    # Worker subagent specific
    max_turns: int = Field(default=30, ge=1)
    max_context_tokens: int = Field(default=96000, ge=1024)
    task_max_concurrency: int = Field(default=2, ge=1)
    # Tools hidden from this agent's tool loop (schema + execution);
    # validated against the registry at startup.
    excluded_tools: list[str] = Field(default_factory=list)
    # Prompt caching for cost optimization
    cache: CacheConfig = Field(default_factory=CacheConfig)


class MemoryArchiveConfig(StrictConfigModel):
    """Auto-archive rolling buffers older than retain_days."""

    retain_days: int = Field(default=3, ge=0)


class MaintenanceCurateConfig(StrictConfigModel):
    """Memory distillation settings during daily maintenance."""

    enabled: bool = True
    digest_retain_days: int = Field(default=14, ge=0)
    digest_max_chars: int = Field(default=1200, ge=1)


class MemoryBackupConfig(StrictConfigModel):
    """Periodic memory backup configuration."""

    enabled: bool = True
    interval_minutes: int = Field(default=30, ge=1)
    retention_minutes: int = Field(default=1440, ge=1)


class SessionFileCleanupConfig(StrictConfigModel):
    """Auto-cleanup expired session JSONL files from disk."""

    enabled: bool = True
    retention_days: int = Field(default=30, ge=1)


class MaintenanceContextRefreshConfig(StrictConfigModel):
    """Context refresh settings during maintenance."""

    preserve_turns: int = Field(default=2, ge=0)


class MaintenanceConfig(StrictConfigModel):
    """Consolidated daily maintenance window.

    Steps run in fixed order:
    archive -> context_refresh -> backup -> session_file_cleanup.
    """

    enabled: bool = True
    daily_hour: int = Field(default=3, ge=0, le=23)
    latest_hour: int = Field(default=6, ge=0, le=23)
    retry_interval_minutes: int = Field(default=10, ge=1)
    # Steps in execution order:
    archive: MemoryArchiveConfig = Field(default_factory=MemoryArchiveConfig)
    curate: MaintenanceCurateConfig = Field(default_factory=MaintenanceCurateConfig)
    context_refresh: MaintenanceContextRefreshConfig = Field(
        default_factory=MaintenanceContextRefreshConfig,
    )
    backup: MemoryBackupConfig = Field(default_factory=MemoryBackupConfig)
    session_file_cleanup: SessionFileCleanupConfig = Field(
        default_factory=SessionFileCleanupConfig,
    )

    @model_validator(mode="after")
    def _validate_time_window(self) -> "MaintenanceConfig":
        if self.latest_hour <= self.daily_hour:
            raise ValueError(
                f"latest_hour ({self.latest_hour}) must be greater than "
                f"daily_hour ({self.daily_hour})"
            )
        return self


class ContextConfig(StrictConfigModel):
    """Context window management."""

    class CommonGroundConfig(StrictConfigModel):
        """Time-anchored common-ground injection settings."""

        enabled: bool = True
        max_entries: int = Field(default=8, ge=1)
        max_chars: int = Field(default=1200, ge=100)
        max_entry_chars: int = Field(default=160, ge=20)
        persist_cache: bool = True

    soft_max_prompt_tokens: int = Field(default=128_000, ge=1_024)
    preserve_turns: int = Field(default=6, ge=1)
    boot_files: list[str] = Field(
        default_factory=lambda: [
            "memory/agent/persona.md",
            "memory/agent/long-term.md",
            "kernel/builtin-skills/index.md",
            "personal-skills/index.md",
        ]
    )
    boot_files_as_tool: list[str] = Field(
        default_factory=lambda: [
            "memory/agent/index.md",
            "memory/agent/temp-memory.md",
        ]
    )
    skill_rescan: bool = False
    common_ground: CommonGroundConfig = Field(default_factory=CommonGroundConfig)


class TuiConfig(StrictConfigModel):
    """CLI/TUI display settings."""

    debug: bool = False
    show_tool_use: bool = False
    replay_turns: int | None = Field(default=5, ge=1)
    show_tool_calls: bool = True


class SendMessageBatchGuidanceConfig(StrictConfigModel):
    """Global switch for prompt/tool guidance that encourages batched sends."""

    enabled: bool = False


class ICloudSyncAwarenessConfig(StrictConfigModel):
    """Prompt-only flag for iCloud-synced user workspace awareness."""

    enabled: bool = False


class CodexRemoteCompactionConfig(StrictConfigModel):
    """Runtime flag for Codex Responses compact routing."""

    enabled: bool = False


class FeaturesConfig(StrictConfigModel):
    """Feature flags."""

    codex_remote_compaction: CodexRemoteCompactionConfig = Field(
        default_factory=CodexRemoteCompactionConfig,
    )
    icloud_sync_awareness: ICloudSyncAwarenessConfig = Field(
        default_factory=ICloudSyncAwarenessConfig,
    )
    send_message_batch_guidance: SendMessageBatchGuidanceConfig = Field(
        default_factory=SendMessageBatchGuidanceConfig,
    )


class GmailChannelConfig(StrictConfigModel):
    """Gmail channel adapter settings."""

    enabled: bool = True
    poll_interval: int = Field(default=45, ge=1)
    max_age_minutes: int | None = Field(default=None, ge=1)
    ignore_senders: list[str] = Field(default_factory=list)
    thread_max_age_days: int = Field(default=7, ge=1)


class DiscordListenChannel(StrictConfigModel):
    """Bootstrap/hard allowlist entry for a Discord guild channel."""

    channel_id: str
    filter: str = Field(
        default="mention_only",
        pattern=r"^(mention_only|all|from_contacts)$",
    )


class DiscordChannelConfig(StrictConfigModel):
    """Discord self-bot adapter settings."""

    enabled: bool = False
    debounce_seconds: int = Field(default=5, ge=1, le=30)
    max_wait_seconds: int = Field(default=30, ge=5, le=120)
    dm_debounce_seconds: int = Field(default=12, ge=1, le=300)
    dm_max_wait_seconds: int = Field(default=180, ge=5, le=600)
    dm_typing_quiet_seconds: int = Field(default=15, ge=2, le=120)
    send_delay_min: float = Field(default=1.0, ge=0)
    send_delay_max: float = Field(default=3.0, ge=0)
    listen_dms: bool = True
    listen_channels: list[DiscordListenChannel] = Field(default_factory=list)
    ignore_users: list[str] = Field(default_factory=list)
    guild_review_interval_seconds: int = Field(default=60, ge=5, le=3600)
    send_typing_refresh_seconds: int = Field(default=7, ge=2, le=30)
    send_typing_cps_min: float = Field(default=14.0, gt=0)
    send_typing_cps_max: float = Field(default=26.0, gt=0)
    send_delay_char_max: float = Field(default=8.0, ge=0)
    send_delay_total_max: float = Field(default=20.0, ge=0)
    presence_mode: str = Field(default="auto", pattern=r"^(off|auto|keep_online)$")
    presence_refresh_seconds: int = Field(default=90, ge=10, le=600)
    presence_idle_after_seconds: int = Field(default=300, ge=30, le=3600)
    auto_download_attachment_max_mb: int = Field(default=25, ge=1, le=200)
    auto_read_images: bool = True
    auto_read_images_in_dm: bool = True
    auto_read_images_in_guild: bool = True
    auto_read_image_max_per_batch: int = Field(default=3, ge=0, le=20)
    auto_read_image_max_mb: int = Field(default=10, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Renamed: thinking_typing_refresh_seconds -> send_typing_refresh_seconds
            if (
                "thinking_typing_refresh_seconds" in data
                and "send_typing_refresh_seconds" not in data
            ):
                data["send_typing_refresh_seconds"] = data.pop(
                    "thinking_typing_refresh_seconds"
                )
            else:
                data.pop("thinking_typing_refresh_seconds", None)
            # Removed: thinking_typing (no-op, silently drop)
            data.pop("thinking_typing", None)
        return data

    @model_validator(mode="after")
    def _validate_ranges(self) -> "DiscordChannelConfig":
        if self.send_delay_min > self.send_delay_max:
            raise ValueError("send_delay_min must be <= send_delay_max")
        if self.debounce_seconds > self.max_wait_seconds:
            raise ValueError("debounce_seconds must be <= max_wait_seconds")
        if self.dm_debounce_seconds > self.dm_max_wait_seconds:
            raise ValueError("dm_debounce_seconds must be <= dm_max_wait_seconds")
        if self.send_typing_cps_min > self.send_typing_cps_max:
            raise ValueError("send_typing_cps_min must be <= send_typing_cps_max")
        return self


class WebChannelConfig(StrictConfigModel):
    """Local Web Chat adapter settings."""

    enabled: bool = False
    history_limit: int = Field(default=200, ge=1, le=1000)


class ChannelsConfig(StrictConfigModel):
    """Channel adapter configuration."""

    gmail: GmailChannelConfig = Field(default_factory=GmailChannelConfig)
    discord: DiscordChannelConfig = Field(default_factory=DiscordChannelConfig)
    web: WebChannelConfig = Field(default_factory=WebChannelConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_channels(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("line_crack", None)
        return data


_QUIET_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def _parse_quiet_window(spec: str) -> tuple[time, time]:
    """Parse 'HH:MM-HH:MM' into (start_time, end_time)."""
    m = _QUIET_WINDOW_RE.fullmatch(spec.strip())
    if not m:
        raise ValueError(f"invalid quiet_hours format {spec!r}, expected 'HH:MM-HH:MM'")
    start = time(int(m.group(1)), int(m.group(2)))
    end = time(int(m.group(3)), int(m.group(4)))
    if start == end:
        raise ValueError(f"quiet_hours window {spec!r} has zero duration")
    return start, end


def _time_in_window(t: time, start: time, end: time) -> bool:
    """Check if time-of-day falls within a window (handles cross-midnight)."""
    if start < end:
        return start <= t < end
    # Cross-midnight: e.g. 23:00-07:00
    return t >= start or t < end


def is_in_quiet_hours(
    dt: datetime,
    windows: list[tuple[time, time]],
    tz: tzinfo,
) -> bool:
    """Check if *dt* falls within any quiet window in the given timezone."""
    local_time = dt.astimezone(tz).time()
    return any(_time_in_window(local_time, s, e) for s, e in windows)


def next_quiet_end(
    dt: datetime,
    windows: list[tuple[time, time]],
    tz: tzinfo,
) -> datetime:
    """Return the earliest quiet-window end time at or after *dt*.

    Assumes ``is_in_quiet_hours(dt, ...)`` is True.
    """
    local_dt = dt.astimezone(tz)
    local_time = local_dt.time()
    candidates: list[datetime] = []
    for start, end in windows:
        if not _time_in_window(local_time, start, end):
            continue
        # Build the end datetime in local timezone
        end_dt = local_dt.replace(
            hour=end.hour, minute=end.minute, second=0, microsecond=0
        )
        if end <= start:
            # Cross-midnight: end is on the next day (or today if we're before midnight)
            if local_time >= start:
                end_dt += timedelta(days=1)
        if end_dt <= dt.astimezone(tz):
            end_dt += timedelta(days=1)
        candidates.append(end_dt)
    if not candidates:
        return dt
    return min(candidates).astimezone(tz)


class HeartbeatConfig(StrictConfigModel):
    """Autonomous heartbeat configuration."""

    enabled: bool = False
    # Whether to enqueue an immediate [STARTUP] system wake-up on process start.
    enqueue_startup: bool = False
    # Whether to enqueue kernel upgrade summaries as one-shot system notices.
    enqueue_upgrade_notice: bool = True
    # Supports hours (h) or minutes (m), e.g. "2h-5h", "30m-90m"
    interval: str = Field(default="2h-5h", pattern=r"^\d+[hm]-\d+[hm]$")
    # Time windows where heartbeat is suppressed, e.g. ["00:00-06:00"]
    quiet_hours: list[str] = Field(default_factory=list)

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, value: list[str]) -> list[str]:
        for spec in value:
            _parse_quiet_window(spec)
        return value

    def parsed_quiet_windows(self) -> list[tuple[time, time]]:
        """Return parsed (start, end) time pairs."""
        return [_parse_quiet_window(s) for s in self.quiet_hours]


class ControlConfig(StrictConfigModel):
    """Control API server configuration for external process management."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=9001, ge=1, le=65535)


class AppSectionConfig(StrictConfigModel):
    """App-level runtime settings."""

    agent_os_dir: str = "~/.agent"
    timezone: str = "UTC+8"
    warn_on_failure: bool = True
    turn_failure_requeue_limit: int = Field(default=1, ge=0)
    turn_failure_requeue_delay_seconds: int = Field(default=60, ge=0)
    requeue_non_retryable_turn_failures: bool = False
    openrouter_site_name: str | None = None
    control: ControlConfig = Field(default_factory=ControlConfig)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        return validate_timezone_spec(value)


class AppConfig(StrictConfigModel):
    """Application configuration."""

    app: AppSectionConfig = Field(default_factory=AppSectionConfig)
    tui: TuiConfig = Field(default_factory=TuiConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    agents: dict[str, AgentConfig]

    def get_agent_os_dir(self) -> Path:
        """Get resolved agent OS directory path."""
        return Path(self.app.agent_os_dir).expanduser().resolve()
