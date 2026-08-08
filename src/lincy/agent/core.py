"""Agent core logic: responder + memory sync.

Extracted from cli/app.py to decouple agent logic from CLI adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import ValidationError

if TYPE_CHECKING:
    from .adapters.protocol import ChannelAdapter
    from .conscience import ConscienceAgent
    from .skill_check import SkillCheckAgent
    from .scope import ScopeResolver
    from .shared_state import SharedStateStore
    from ..brain_prompt_policy import BrainPromptPolicy
    from ..llm.providers.copilot_runtime import CopilotRuntime

from ..context import ContextBuilder, Conversation
from ..core.schema import AppConfig, MaintenanceConfig
from ..llm.http_error import classify_http_status_error
from ..llm import LLMResponse
from ..llm.base import ConversationCompactionClient, LLMClient
from ..llm.schema import (
    ContextLengthExceededError,
    MalformedFunctionCallError,
    Message,
    ToolDefinition,
)
from ..memory import (
    ARTIFACT_REGISTRY_TARGET,
    find_missing_artifact_registry_paths,
    find_missing_memory_sync_targets,
)
from ..memory.backup import MemoryBackupManager
from ..session import SessionManager
from ..session.schema import SessionEntry
from ..skills import rebuild_personal_skills_index
from ..timezone_utils import get_tz, now as tz_now
from ..tools import FilteredToolRegistry, ToolRegistry
from ..tui.sink import UiSink
from ..workspace import WorkspaceManager
from . import responder as _responder
from .queue import PersistentPriorityQueue
from .responder import (
    _CommonGroundTurnDebug,
    _build_common_ground_overlay,
    _build_dynamic_turn_overlay,
    _compose_message_overlays,
)
from .run_helpers import (
    _latest_intermediate_text,  # noqa: F401
    _latest_nonempty_assistant_content,  # noqa: F401
    _resolve_final_content,
    _surface_error_message,
    _strip_timestamp_prefix,
)
from .schema import (
    InboundMessage,
    MaintenanceSentinel,
    NewSessionSentinel,
    ReloadSentinel,
    ReloadSystemPromptSentinel,
    ShutdownSentinel,
)
from .skill_governance import SkillGovernanceRegistry
from .scope import DEFAULT_SCOPE_RESOLVER
from .staged_planning import run_stage1_information_gathering, run_stage2_brain_planning
from .tool_setup import setup_tools  # noqa: F401
from .turn_context import ProactiveTurnYield, TurnContext
from .turn_effects import analyze_turn_effects
from ..turn_timing import TURN_PROCESSING_STARTED_AT_KEY, build_turn_timing_metadata

# Re-exported for backward compatibility with tests importing from
# lincy.agent.core. AgentCore itself does not use every symbol here.
from .turn_runtime import (
    _EMPTY_RESPONSE_NUDGE,  # noqa: F401
    _LatestTokenStatus,
    _TurnMemorySnapshot,
    _TurnTokenUsage,
    _build_artifact_registry_sync_reminder,
    _build_memory_sync_reminder,  # noqa: F401
    _inject_brain_failure_record,
    _patch_interrupted_tool_calls,
    _rollback_turn_memory_changes,
    _run_empty_response_fallback,  # noqa: F401
    _run_memory_archive,
    _run_memory_sync_side_channel,
)
from .ui_event_console import AgentUiPort, UiEventConsole

logger = logging.getLogger(__name__)
_RENDERED_STATIC_METADATA_KEY = "rendered_static"
_READ_CACHE_MEASURABLE_PROVIDERS = frozenset(
    {
        "anthropic",
        "claude_code",
        "codex",
        "copilot",
        "deepseek",
        "grok",
        "heyroute",
        "openai",
        "openrouter",
    }
)

TurnRunStatus = Literal["completed", "failed", "interrupted"]

_HEARTBEAT_RELIABILITY_NOTICE = (
    "[Heartbeat Reliability Notice]\n"
    "Heartbeat is opportunistic background scanning, not a reliable follow-up "
    "or wake-up guarantee.\n"
    "agent_note, temp-memory.md, and a future heartbeat will not wake you up.\n"
    "If medication, health, safety, travel, promises, or any open-loop "
    "user-care state must be checked later, create schedule_action now unless "
    "you explicitly decide not to follow up and persist the reason."
)

_HEARTBEAT_QUIET_HOURS_NOTICE = (
    "[Heartbeat Quiet-Hours Warning]\n"
    "The earliest next heartbeat would be deferred by quiet hours. This may be "
    "the last heartbeat before quiet hours.\n"
    "Do not leave user-care goals to heartbeat. Create schedule_action for "
    "every required later check now."
)
TurnFailureCategory = Literal[
    "request-format",
    "provider-api",
    "transport",
    "provider-response",
    "context-length",
    "other",
]
CompactionSource = Literal["codex_remote", "local", "local_fallback"]

_TURN_FAILURE_REQUEUE_COUNT_KEY = "turn_failure_requeue_count"
_TURN_FAILURE_FIRST_FAILED_AT_KEY = "turn_failure_first_failed_at"
_PROACTIVE_YIELD_REEVALUATE_DELAY = timedelta(minutes=2)


def _brain_read_cache_measurable(provider: str | None) -> bool:
    return provider in _READ_CACHE_MEASURABLE_PROVIDERS


@dataclass(frozen=True)
class ContextCompactionResult:
    """One compaction attempt outcome."""

    changed: bool
    removed_messages: int = 0
    source: CompactionSource | None = None
    trigger: str | None = None
    fallback: bool = False

    @property
    def source_label(self) -> str | None:
        if self.source == "codex_remote":
            return "codex remote"
        if self.source == "local_fallback":
            return "local fallback"
        if self.source == "local":
            return "local"
        return None


def _ensure_turn_runtime_metadata(
    *,
    channel: str,
    timestamp: datetime | None,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Freeze per-turn timing metadata once so repeated builds stay cache-stable."""
    prepared = dict(metadata or {})
    if TURN_PROCESSING_STARTED_AT_KEY in prepared:
        return prepared

    processing_started_at = tz_now()
    event_timestamp = processing_started_at
    if (
        timestamp is not None
        and timestamp.tzinfo is not None
        and timestamp.utcoffset() is not None
    ):
        event_timestamp = timestamp
    return build_turn_timing_metadata(
        channel=channel,
        metadata=prepared,
        event_timestamp=event_timestamp,
        processing_started_at=processing_started_at,
    )


def _run_responder(*args, **kwargs) -> LLMResponse:
    """Compatibility wrapper for the responder loop implementation."""
    return _responder._run_responder(*args, **kwargs)


def _run_brain_responder(**kwargs) -> LLMResponse:
    """Compatibility wrapper for staged planning plus responder execution."""
    return _responder._run_brain_responder(
        **kwargs,
        run_responder_fn=_run_responder,
        stage1_gather_fn=run_stage1_information_gathering,
        stage2_plan_fn=run_stage2_brain_planning,
    )


def _classify_turn_failure(error: Exception) -> TurnFailureCategory:
    """Classify a failed turn for queue-level retry decisions."""
    if isinstance(error, ContextLengthExceededError):
        return "context-length"
    if isinstance(
        error,
        (
            httpx.TimeoutException,
            TimeoutError,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ),
    ):
        return "transport"
    if isinstance(error, (MalformedFunctionCallError, ValidationError)):
        return "provider-response"
    if isinstance(error, httpx.HTTPStatusError):
        category = classify_http_status_error(error)
        if category == "request-format":
            return "request-format"
        if category == "provider-api":
            return "provider-api"
        status = error.response.status_code if error.response is not None else None
        if status in {429, 500, 502, 503, 504, 529}:
            return "transport"
        return "provider-api"
    return "other"


def _should_requeue_failed_turn(
    category: TurnFailureCategory | None,
    *,
    requeue_non_retryable: bool = False,
) -> bool:
    """Return True when a failed inbound should be retried through the queue."""
    if category in {None, "transport", "provider-response"}:
        return True
    if not requeue_non_retryable:
        return False
    return category in {"request-format", "provider-api", "context-length", "other"}


def _classify_inbound_kind(
    *,
    channel: str,
    metadata: dict[str, object] | None,
) -> str:
    """Classify the inbound source for turn-level debug logs."""
    meta = metadata or {}
    if channel == "system":
        if bool(meta.get("task_due")):
            return "task_due"
        if isinstance(meta.get("scheduled_reason"), str):
            return "scheduled"
        if bool(meta.get("system")):
            return "heartbeat"
        return "system"
    return "user_message"


class _MaintenanceScheduler:
    """Background timer that enqueues MaintenanceSentinel at daily_hour.

    Retries every retry_interval_minutes until latest_hour.
    Skips the day if latest_hour is passed without a successful run.
    """

    def __init__(
        self,
        queue: PersistentPriorityQueue,
        config: MaintenanceConfig,
    ):
        self._queue = queue
        self._config = config
        self._tz = get_tz()
        self._ran_today = False
        self._last_date: date | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def mark_done(self) -> None:
        """Called after successful maintenance to prevent re-trigger today."""
        self._ran_today = True

    def _loop_once(self) -> bool:
        """Check if maintenance is due. Returns True if sentinel enqueued."""
        now = datetime.now(self._tz)
        today = now.date()

        # Reset flag on new day
        if self._last_date != today:
            self._ran_today = False
            self._last_date = today

        if self._ran_today:
            return False

        hour = now.hour
        if hour < self._config.daily_hour:
            return False
        if hour >= self._config.latest_hour:
            # Past window; skip today
            self._ran_today = True
            logger.info(
                "Maintenance window passed (%02d:00-%02d:00), skipping today",
                self._config.daily_hour,
                self._config.latest_hour,
            )
            return False

        self._queue.put(MaintenanceSentinel())
        return True

    def _loop(self) -> None:
        while not self._stop.wait(timeout=60):
            if self._loop_once():
                # Wait retry interval before next attempt
                self._stop.wait(timeout=self._config.retry_interval_minutes * 60)


@dataclass
class _PreparedTurn:
    """Prepared state for one run_turn attempt."""

    debug: bool
    pre_turn_anchor: int
    turn_metadata: dict[str, object] | None
    messages: list[Message]
    # Composed overlay applied to the latest user message of every outgoing
    # request this turn: per-turn dynamic blocks ([Runtime Context],
    # [Timing Notice], [Decision Reminder], [Agent Notes]) followed by the
    # common-ground block, in that order. Captured once per turn attempt
    # (see _prepare_turn_attempt) and reused verbatim across tool-loop
    # rounds and the single overflow retry so BP4 still hits within the turn.
    message_overlay: Callable[[list[Message]], list[Message]] | None
    turn_memory_snapshot: _TurnMemorySnapshot
    turn_anchor: int


class AgentCore:
    """Core agent logic: responder + memory sync."""

    def __init__(
        self,
        *,
        client: LLMClient,
        conversation: Conversation,
        builder: ContextBuilder,
        registry: ToolRegistry,
        excluded_tools: frozenset[str] = frozenset(),
        ui_sink: UiSink,
        workspace: WorkspaceManager,
        config: AppConfig,
        agent_os_dir: Path,
        user_id: str,
        session_mgr: SessionManager | None = None,
        display_name: str = "",
        # Memory
        memory_edit_allow_failure: bool = False,
        memory_backup_mgr: MemoryBackupManager | None = None,
        # Queue
        queue: PersistentPriorityQueue | None = None,
        # Turn context for send_message tool
        turn_context: TurnContext | None = None,
        turn_cancel: object | None = None,
        shared_state_store: SharedStateStore | None = None,
        scope_resolver: ScopeResolver | None = None,
        memory_sync_client: LLMClient | None = None,
        conversation_compaction_client: ConversationCompactionClient | None = None,
        brain_prompt_policy: "BrainPromptPolicy | None" = None,
        copilot_runtime: "CopilotRuntime | None" = None,
        ui_debug: bool = False,
        ui_show_tool_use: bool = False,
        ui_timezone: str | None = None,
        ui_gui_intent_max_chars: int | None = None,
        task_store: object | None = None,
        note_store: object | None = None,
        skill_check_agent: "SkillCheckAgent | None" = None,
        conscience_agent: "ConscienceAgent | None" = None,
    ):
        self.client = client
        self.memory_sync_client = memory_sync_client
        self.conversation = conversation
        self.builder = builder
        # Excluded tools stay hidden from both the schema and execution, so a
        # model that hallucinates the name gets a normal unknown-tool error.
        self.registry: ToolRegistry | FilteredToolRegistry = (
            FilteredToolRegistry(registry, excluded_tools) if excluded_tools else registry
        )
        self.ui_sink = ui_sink
        self.console: AgentUiPort = UiEventConsole(
            ui_sink,
            debug=ui_debug,
            show_tool_use=ui_show_tool_use,
        )
        if ui_timezone:
            self.console.set_timezone(ui_timezone)
        self.console.gui_intent_max_chars = ui_gui_intent_max_chars
        self.workspace = workspace
        self.config = config
        self.agent_os_dir = agent_os_dir
        self.user_id = user_id
        self.session_mgr = session_mgr
        self.display_name = display_name
        self.memory_edit_allow_failure = memory_edit_allow_failure
        self.memory_backup_mgr = memory_backup_mgr
        self._queue = queue
        self.turn_context = turn_context
        self.turn_cancel = turn_cancel
        self.shared_state_store = shared_state_store
        self.scope_resolver = scope_resolver or DEFAULT_SCOPE_RESOLVER
        self.conversation_compaction_client = conversation_compaction_client
        self.copilot_runtime = copilot_runtime
        self.brain_prompt_policy = brain_prompt_policy
        self.skill_registry = SkillGovernanceRegistry.load(
            agent_os_dir,
            governance_config=self.config.tools.skill_governance,
        )
        self._maintenance_scheduler: _MaintenanceScheduler | None = None
        self._turns_since_memory_sync: int = 0
        self.adapters: dict[str, ChannelAdapter] = {}
        brain_cfg = self.config.agents.get("brain")
        self._brain_provider = brain_cfg.llm.provider if brain_cfg is not None else ""
        self._soft_max_prompt_tokens = self.config.context.soft_max_prompt_tokens
        self._latest_token_status = _LatestTokenStatus()
        self._turn_token_usage = _TurnTokenUsage()
        self._last_proactive_yield: ProactiveTurnYield | None = None
        self._last_turn_failure_category: TurnFailureCategory | None = None
        self.task_store = task_store
        self.note_store = note_store
        self.skill_check_agent = skill_check_agent
        self.conscience_agent = conscience_agent

    def _maybe_rescan_skills(self) -> None:
        """Rescan skill roots if directory mtimes have changed."""
        ctx = getattr(self.config, "context", None)
        if ctx is None or not getattr(ctx, "skill_rescan", False):
            return
        skill_registry = getattr(self, "skill_registry", None)
        if skill_registry is None:
            return
        if skill_registry.needs_rescan():
            logger.info("Skill root changed; rescanning skills")
            self.skill_registry = SkillGovernanceRegistry.load(
                self.agent_os_dir,
                governance_config=self.config.tools.skill_governance,
            )
            rebuild_personal_skills_index(self.agent_os_dir)
            self.builder.reload_boot_files()

    def _reset_turn_token_usage(self) -> None:
        """Reset per-turn token aggregation state."""
        self._turn_token_usage = _TurnTokenUsage()

    def _record_brain_response_usage(self, response: LLMResponse) -> None:
        """Record usage from each brain model response in the current turn."""
        self._turn_token_usage.record(response)

    def _finalize_turn_token_status(self) -> None:
        """Publish per-turn aggregated usage to the status model."""
        agg = self._turn_token_usage
        if agg.usage_available:
            self._latest_token_status = _LatestTokenStatus(
                prompt_tokens=agg.max_prompt_tokens,
                completion_tokens=agg.completion_tokens_for_max_prompt,
                total_tokens=agg.total_tokens_for_max_prompt,
                cache_prompt_tokens=agg.cache_prompt_tokens_for_display,
                cache_read_tokens=agg.cache_read_tokens_for_display,
                cache_write_tokens=agg.cache_write_tokens_for_display,
                usage_available=True,
                missing_usage=False,
            )
            self._warn_low_cache_rate(agg)
            return

        if self._brain_provider == "copilot" and agg.saw_missing_usage:
            self._latest_token_status = _LatestTokenStatus(
                usage_available=False,
                missing_usage=True,
            )

    _low_cache_streak: int = 0

    def _warn_low_cache_rate(self, agg: "_TurnTokenUsage") -> None:
        """Emit a warning when cache hit rate is low for consecutive turns."""
        prompt = agg.max_prompt_tokens
        if prompt is None or prompt < 10000:
            return
        if not _brain_read_cache_measurable(self._brain_provider):
            self._low_cache_streak = 0
            return
        brain_cfg = self.config.agents.get("brain")
        if brain_cfg is None:
            return
        cache_cfg = getattr(brain_cfg, "cache", None)
        if cache_cfg is None or not cache_cfg.enabled:
            return
        cache_read = agg.cache_read_tokens_for_display
        rate = cache_read / prompt if prompt > 0 else 0
        if rate < 0.3:
            self._low_cache_streak += 1
        else:
            self._low_cache_streak = 0
            return
        if self._low_cache_streak >= 2:
            self.console.print_warning(
                f"Low cache hit rate for {self._low_cache_streak} consecutive turns: "
                f"{rate:.0%} (read={cache_read:,} prompt={prompt:,})"
            )

    def get_token_status_text(self) -> str:
        """Return token status text for toolbar and processing headers."""
        limit = self._soft_max_prompt_tokens
        state = self._latest_token_status
        if state.usage_available and state.prompt_tokens is not None:
            pct = state.prompt_tokens / limit * 100 if limit else 0
            suffix = " soft-over" if state.prompt_tokens > limit else ""
            if not _brain_read_cache_measurable(self._brain_provider):
                return (
                    f"tok {state.prompt_tokens:,}/{limit:,} ({pct:.1f}%)"
                    f" cache unavailable{suffix}"
                )
            cache_prompt_tokens = state.cache_prompt_tokens or state.prompt_tokens
            read_rate = (
                state.cache_read_tokens / cache_prompt_tokens * 100
                if cache_prompt_tokens > 0
                else 0.0
            )
            cache_suffix = (
                f" cache r{state.cache_read_tokens:,}/{cache_prompt_tokens:,}"
                f" ({read_rate:.1f}%)"
            )
            if state.cache_write_tokens > 0:
                cache_suffix += f" w{state.cache_write_tokens:,}"
            return (
                f"tok {state.prompt_tokens:,}/{limit:,} ({pct:.1f}%)"
                f"{cache_suffix}{suffix}"
            )
        if state.missing_usage:
            return f"tok unavailable/{limit:,} (copilot no usage)"
        return f"tok --/{limit:,} (--.-%)"

    def _is_soft_limit_exceeded(self) -> bool:
        """Check if current turn exceeded soft prompt token limit."""
        state = self._latest_token_status
        if not state.usage_available or state.prompt_tokens is None:
            return False
        return state.prompt_tokens > self._soft_max_prompt_tokens

    def _record_compaction_result(self, result: ContextCompactionResult) -> None:
        """Persist one successful compaction result for UI/debug inspection."""
        if result.source is None or result.trigger is None:
            return
        logger.info(
            "Context compacted via %s (trigger=%s, removed=%d, fallback=%s)",
            result.source,
            result.trigger,
            result.removed_messages,
            result.fallback,
        )
        if self.session_mgr is not None:
            self.session_mgr.record_compaction(
                source=result.source,
                trigger=result.trigger,
                removed_messages=result.removed_messages,
                fallback=result.fallback,
            )

    def _apply_soft_prompt_compaction(self) -> None:
        """Compact history after a turn when soft token budget is exceeded."""
        state = self._latest_token_status
        if not state.usage_available:
            return
        prompt_tokens = state.prompt_tokens
        if prompt_tokens is None or prompt_tokens <= self._soft_max_prompt_tokens:
            return
        result = self._compact_context(
            preserve_turns=self.config.context.preserve_turns,
            trigger="soft_limit",
        )
        if not result.changed:
            return
        via = f" via {result.source_label}" if result.source_label else ""
        details = (
            f"compacted {result.removed_messages} messages"
            if result.removed_messages > 0
            else "compacted context"
        )
        self.console.print_warning(
            "Soft token limit exceeded "
            f"({prompt_tokens:,}/{self._soft_max_prompt_tokens:,}); "
            f"{details}{via}.",
            indent=2,
        )

    def _compact_context_local(
        self,
        preserve_turns: int,
        *,
        trigger: str,
        fallback: bool = False,
    ) -> ContextCompactionResult:
        removed = self.conversation.compact(preserve_turns)
        if removed <= 0:
            return ContextCompactionResult(
                changed=False,
                removed_messages=0,
                source="local_fallback" if fallback else "local",
                trigger=trigger,
                fallback=fallback,
            )
        self.builder.clear_render_cache()
        if self.session_mgr is not None:
            self.session_mgr.rewrite_messages(self.conversation.get_messages())
        return ContextCompactionResult(
            changed=True,
            removed_messages=removed,
            source="local_fallback" if fallback else "local",
            trigger=trigger,
            fallback=fallback,
        )

    def _compact_context_remote(self, *, trigger: str) -> ContextCompactionResult:
        client = getattr(self, "conversation_compaction_client", None)
        if client is None:
            return ContextCompactionResult(changed=False)

        rendered_messages = self.builder.build(self.conversation)
        compacted_messages = client.compact_messages(
            rendered_messages,
            tools=self.registry.get_definitions(),
        )
        if not compacted_messages:
            return ContextCompactionResult(
                changed=False,
                source="codex_remote",
                trigger=trigger,
            )

        previous_entries = self.conversation.get_messages()
        previous_count = len(self.conversation.get_messages())
        entries = [
            SessionEntry(
                message=message,
                metadata={_RENDERED_STATIC_METADATA_KEY: True},
            )
            for message in compacted_messages
        ]
        self.conversation.replace_messages(entries)
        self.builder.clear_render_cache()
        if self.session_mgr is not None:
            self.session_mgr.rewrite_messages(entries)
        removed = max(previous_count - len(entries), 0)
        changed = entries != previous_entries
        return ContextCompactionResult(
            changed=changed,
            removed_messages=removed,
            source="codex_remote",
            trigger=trigger,
        )

    def _compact_context(
        self,
        *,
        preserve_turns: int,
        trigger: str,
    ) -> ContextCompactionResult:
        client = getattr(self, "conversation_compaction_client", None)
        if client is not None:
            try:
                result = self._compact_context_remote(trigger=trigger)
                if result.changed:
                    self._record_compaction_result(result)
                return result
            except Exception as exc:
                logger.warning(
                    "Codex remote compaction failed during %s; falling back to local compact: %s",
                    trigger,
                    exc,
                )
                result = self._compact_context_local(
                    preserve_turns,
                    trigger=trigger,
                    fallback=True,
                )
                if result.changed:
                    self._record_compaction_result(result)
                return result
        result = self._compact_context_local(
            preserve_turns,
            trigger=trigger,
            fallback=False,
        )
        if result.changed:
            self._record_compaction_result(result)
        return result

    def run_manual_compact(self) -> ContextCompactionResult:
        result = self._compact_context(
            preserve_turns=self.builder.preserve_turns,
            trigger="manual",
        )
        if result.changed and self.session_mgr is not None:
            self.session_mgr.finalize("compacted")
            self.session_mgr.create(self.user_id, self.display_name)
            self.conversation.set_on_message(self.session_mgr.append_message)
        return result

    def _make_turn_output(
        self,
        user_input: str,
        *,
        output_fn: Callable[[str | None], None] | None,
        channel: str,
        sender: str | None,
    ) -> Callable[[str | None], None]:
        """Return the per-turn output callback."""
        if output_fn is not None:
            return output_fn

        self.console.print_inbound(channel, sender, user_input)
        self.console.print_processing(channel, sender)

        def _output(content: str | None) -> None:
            self.console.print_inner_thoughts(channel, sender, content)

        return _output

    def _prepare_turn_attempt(
        self,
        user_input: str,
        *,
        channel: str,
        sender: str | None,
        timestamp: datetime | None,
        turn_metadata: dict[str, object] | None,
    ) -> _PreparedTurn:
        """Append the user input and prepare one responder attempt."""
        debug = self.console.debug
        pre_turn_anchor = len(self.conversation.get_messages())
        self.conversation.add(
            "user",
            user_input,
            channel=channel,
            sender=sender,
            timestamp=timestamp,
            metadata=turn_metadata,
        )
        messages = self.builder.build(self.conversation)
        # Snapshot the full per-turn overlay text ONCE here (dynamic blocks
        # then common ground, preserving today's final on-the-wire order)
        # and reuse the identical composed callable for every LLM call this
        # turn, including the single overflow retry. If agent_note edits a
        # note mid-turn, later rounds of THIS turn still see this snapshot;
        # the next turn picks up the change.
        latest_user_entry = self.conversation.get_messages()[-1]
        dynamic_overlay = _build_dynamic_turn_overlay(
            entry=latest_user_entry,
            builder=self.builder,
            note_store=getattr(self, "note_store", None),
        )
        common_ground_overlay, common_ground_debug = _build_common_ground_overlay(
            shared_state_store=getattr(self, "shared_state_store", None),
            config=self.config,
            turn_metadata=turn_metadata,
            console=self.console,
            debug=debug,
        )
        self._debug_common_ground_turn(
            common_ground_debug=common_ground_debug,
            common_ground_overlay=common_ground_overlay,
            debug=debug,
        )
        self._debug_latest_user_context(messages, debug=debug)

        turn_memory_snapshot = _TurnMemorySnapshot(agent_os_dir=self.agent_os_dir)
        turn_anchor = len(self.conversation.get_messages())
        return _PreparedTurn(
            debug=debug,
            pre_turn_anchor=pre_turn_anchor,
            turn_metadata=turn_metadata,
            messages=messages,
            message_overlay=_compose_message_overlays(
                dynamic_overlay,
                common_ground_overlay,
            ),
            turn_memory_snapshot=turn_memory_snapshot,
            turn_anchor=turn_anchor,
        )

    def _prepare_retry_turn_attempt(
        self,
        user_input: str,
        *,
        channel: str,
        sender: str | None,
        timestamp: datetime | None,
        turn_metadata: dict[str, object] | None,
        message_overlay: Callable[[list[Message]], list[Message]] | None,
    ) -> _PreparedTurn:
        """Prepare the single retry after overflow compaction.

        This intentionally reuses the original turn-start overlay (dynamic
        blocks + common ground) and skips extra debug output to preserve
        the previous retry behavior while keeping the original inbound
        timestamp stable across the single retry.
        """
        pre_turn_anchor = len(self.conversation.get_messages())
        self.conversation.add(
            "user",
            user_input,
            channel=channel,
            sender=sender,
            timestamp=timestamp,
            metadata=turn_metadata,
        )
        messages = self.builder.build(self.conversation)
        turn_memory_snapshot = _TurnMemorySnapshot(agent_os_dir=self.agent_os_dir)
        turn_anchor = len(self.conversation.get_messages())
        return _PreparedTurn(
            debug=self.console.debug,
            pre_turn_anchor=pre_turn_anchor,
            turn_metadata=turn_metadata,
            messages=messages,
            message_overlay=message_overlay,
            turn_memory_snapshot=turn_memory_snapshot,
            turn_anchor=turn_anchor,
        )

    def _debug_common_ground_turn(
        self,
        *,
        common_ground_debug: _CommonGroundTurnDebug,
        common_ground_overlay: Callable[[list[Message]], list[Message]] | None,
        debug: bool,
    ) -> None:
        """Print current common-ground injection state in debug mode."""
        if not debug:
            return

        cg_scope_id = common_ground_debug.scope_id
        cg_anchor_rev = common_ground_debug.anchor_shared_rev
        cg_turn_start_current_rev = common_ground_debug.current_shared_rev

        if not self.config.context.common_ground.enabled:
            self.console.print_debug("common-ground-turn", "disabled")
        elif not isinstance(cg_scope_id, str) or not cg_scope_id:
            self.console.print_debug("common-ground-turn", "skip no_scope")
        elif not isinstance(cg_anchor_rev, int):
            self.console.print_debug(
                "common-ground-turn",
                f"skip no_anchor scope={cg_scope_id}",
            )
        elif (
            not common_ground_debug.store_available or cg_turn_start_current_rev is None
        ):
            self.console.print_debug(
                "common-ground-turn",
                f"skip no_store scope={cg_scope_id} anchor={cg_anchor_rev}",
            )
        else:
            self.console.print_debug(
                "common-ground-turn",
                "injected="
                f"{common_ground_overlay is not None} "
                f"scope={cg_scope_id} "
                f"anchor={cg_anchor_rev} "
                f"current={cg_turn_start_current_rev}",
            )

    def _debug_latest_user_context(
        self,
        messages: list[Message],
        *,
        debug: bool,
    ) -> None:
        """Show the last user message as seen by the model in debug mode."""
        if not debug:
            return
        for message in reversed(messages):
            if message.role == "user" and isinstance(message.content, str):
                self.console.print_debug("context", message.content[:200])
                break

    def _get_turn_cancel_callbacks(
        self,
    ) -> tuple[Callable[[], bool] | None, Callable[[], None] | None]:
        """Return cancel hooks used by long-running turn operations."""
        return (
            getattr(self.turn_cancel, "is_requested", None),
            getattr(self.turn_cancel, "mark_pending", None),
        )

    def _make_preempt_checker(
        self,
        channel: str,
        scope_id: str | None,
    ) -> Callable[[], bool] | None:
        """Return a callback that checks whether fresher inbound is queued.

        When *scope_id* is available (multi-conversation adapters like
        Discord/LINE), scope the check to that conversation.  Otherwise
        fall back to channel-level matching.

        Also checks adapter-level debounce buffers so messages still
        being debounced can trigger preemption immediately.
        """
        if self._queue is None:
            return None
        q = self._queue

        # Collect adapters that support buffered-inbound checks.
        adapter = self.adapters.get(channel)
        has_buffered = getattr(adapter, "has_buffered_inbound", None)

        if scope_id is not None:

            def _has_pending() -> bool:
                if q.has_ready_pending_inbound_for_scope(scope_id):
                    return True
                if has_buffered is not None and has_buffered(scope_id):
                    return True
                return False
        else:

            def _has_pending() -> bool:
                return q.has_ready_pending_inbound_for_channel(channel)

        return _has_pending

    def _execute_turn_attempt(
        self,
        *,
        prepared: _PreparedTurn,
        output: Callable[[str | None], None],
        channel: str,
        sender: str | None,
        enable_memory_sync: bool,
        flush_pending_outbound: bool,
    ) -> str | None:
        """Run one prepared turn attempt."""
        tools = self.registry.get_definitions()
        is_cancel_requested, on_cancel_pending = self._get_turn_cancel_callbacks()

        self._reset_turn_token_usage()
        response = _run_brain_responder(
            client=self.client,
            messages=prepared.messages,
            tools=tools,
            conversation=self.conversation,
            builder=self.builder,
            registry=self.registry,
            console=self.console,
            config=self.config,
            channel=channel,
            sender=sender,
            on_before_tool_call=prepared.turn_memory_snapshot.capture_from_tool_call,
            memory_edit_allow_failure=self.memory_edit_allow_failure,
            max_iterations=self.config.tools.max_tool_iterations,
            memory_edit_turn_retry_limit=self.config.tools.memory_edit.turn_retry_limit,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
            message_overlay=prepared.message_overlay,
            on_model_response=self._record_brain_response_usage,
            skill_registry=getattr(self, "skill_registry", None),
            skill_check_agent=getattr(self, "skill_check_agent", None),
            turn_context=self.turn_context,
            check_preempt=self._make_preempt_checker(
                channel,
                prepared.turn_metadata.get("scope_id")
                if prepared.turn_metadata
                else None,
            ),
        )

        # --- Conscience agent post-check ---
        response = self._maybe_run_conscience_check(
            response=response,
            prepared=prepared,
            channel=channel,
            sender=sender,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
        )

        self._finalize_turn_token_status()
        final_content, used_fallback_content = _resolve_final_content(
            response.content,
            self.conversation.get_messages()[prepared.turn_anchor :],
        )
        final_content = _strip_timestamp_prefix(final_content)
        if prepared.debug:
            self.console.print_debug(
                "resolve",
                f"final_content_chars={len(final_content)}, "
                f"used_fallback={used_fallback_content}",
            )

        self._maybe_run_turn_artifact_sync(
            prepared=prepared,
            tools=tools,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
        )

        if enable_memory_sync:
            self._maybe_run_turn_memory_sync(
                prepared=prepared,
                tools=tools,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
            )

        if final_content and not used_fallback_content:
            self.conversation.add("assistant", final_content)
        output(final_content or None)

        if flush_pending_outbound:
            self._flush_pending_outbound()

        if enable_memory_sync:
            self._maybe_run_pre_compaction_memory_sync(
                prepared=prepared,
                tools=tools,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
            )

        self._apply_soft_prompt_compaction()
        return final_content or None

    def _maybe_run_conscience_check(
        self,
        *,
        response: LLMResponse,
        prepared: _PreparedTurn,
        channel: str,
        sender: str | None,
        is_cancel_requested: Callable[[], bool] | None,
        on_cancel_pending: Callable[[], None] | None,
    ) -> LLMResponse:
        """Run conscience agent post-check; re-run brain if feedback given."""
        from .conscience import collect_turn_tool_history

        agent: ConscienceAgent | None = getattr(self, "conscience_agent", None)
        if agent is None:
            return response

        # Extract user input from conversation (last user message before turn)
        user_input = ""
        for entry in reversed(self.conversation.get_messages()[: prepared.turn_anchor]):
            msg = entry.message
            if msg.role == "user":
                if isinstance(msg.content, str):
                    user_input = msg.content
                elif isinstance(msg.content, list):
                    user_input = " ".join(
                        p.text for p in msg.content if p.type == "text" and p.text
                    )
                break
        if not user_input.strip():
            return response

        tool_history = collect_turn_tool_history(
            self.conversation.get_messages(),
            prepared.turn_anchor,
        )
        agent_response = response.content

        tool_names = [t.name for t in self.registry.get_definitions()]
        feedback = agent.check(
            user_input=user_input,
            tool_history=tool_history,
            agent_response=agent_response,
            available_tools=tool_names,
        )
        if feedback is None:
            if self.console.debug:
                self.console.print_debug("conscience", "NONE")
            return response

        self.console.print_info(f"Conscience: {feedback}")

        # Inject feedback as system message and re-run brain
        if response.content:
            self.conversation.add("assistant", response.content)
        self.conversation.add("user", f"[conscience-check] {feedback}")
        tools = self.registry.get_definitions()
        messages = self.builder.build(self.conversation)
        if is_cancel_requested and is_cancel_requested():
            return response
        response = _run_brain_responder(
            client=self.client,
            messages=messages,
            tools=tools,
            conversation=self.conversation,
            builder=self.builder,
            registry=self.registry,
            console=self.console,
            config=self.config,
            channel=channel,
            sender=sender,
            memory_edit_allow_failure=self.memory_edit_allow_failure,
            max_iterations=self.config.tools.max_tool_iterations,
            memory_edit_turn_retry_limit=self.config.tools.memory_edit.turn_retry_limit,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
            # Reuse this turn's already-snapshotted overlay (dynamic blocks +
            # common ground) instead of leaving the re-run overlay-free: the
            # conscience re-run answers the same turn, so it should see the
            # same turn-start snapshot as the primary call, not a fresh
            # re-read of NoteStore/common-ground state.
            message_overlay=prepared.message_overlay,
            on_model_response=self._record_brain_response_usage,
            skill_registry=getattr(self, "skill_registry", None),
            skill_check_agent=getattr(self, "skill_check_agent", None),
            turn_context=self.turn_context,
        )
        return response

    def _maybe_run_turn_artifact_sync(
        self,
        *,
        prepared: _PreparedTurn,
        tools: list[ToolDefinition],
        is_cancel_requested: Callable[[], bool] | None,
        on_cancel_pending: Callable[[], None] | None,
    ) -> None:
        """Ensure same-turn artifact writes are registered in live memory."""
        sync_turn_messages = self.conversation.get_messages()[prepared.turn_anchor :]
        missing_artifact_paths = find_missing_artifact_registry_paths(
            sync_turn_messages,
            agent_os_dir=self.agent_os_dir,
        )
        if prepared.debug:
            self.console.print_debug(
                "artifact-sync",
                f"missing={len(missing_artifact_paths)}",
            )
        if not missing_artifact_paths:
            return

        try:
            sync_client = getattr(self, "memory_sync_client", None) or self.client
            _run_memory_sync_side_channel(
                sync_client,
                self.conversation,
                self.builder,
                tools,
                self.registry,
                self.console,
                missing_targets=[ARTIFACT_REGISTRY_TARGET],
                max_retries=self.config.tools.memory_sync.max_retries,
                reminder_text=_build_artifact_registry_sync_reminder(
                    missing_artifact_paths,
                    registry_target=ARTIFACT_REGISTRY_TARGET,
                ),
                on_before_tool_call=prepared.turn_memory_snapshot.capture_from_tool_call,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
            )
            if prepared.debug:
                self.console.print_debug("artifact-sync", "done")
        except ContextLengthExceededError:
            if prepared.debug:
                self.console.print_debug(
                    "artifact-sync",
                    "skipped: context length exceeded",
                )
        except Exception:
            if prepared.debug:
                self.console.print_debug("artifact-sync", "side-channel failed")

    def _maybe_run_turn_memory_sync(
        self,
        *,
        prepared: _PreparedTurn,
        tools: list[ToolDefinition],
        is_cancel_requested: Callable[[], bool] | None,
        on_cancel_pending: Callable[[], None] | None,
    ) -> None:
        """Run the scheduled side-channel memory sync for a normal turn."""
        is_system_heartbeat = (
            self.turn_context is not None and self.turn_context.metadata.get("system")
        )
        sync_cfg = self.config.tools.memory_sync
        should_sync = False
        if not is_system_heartbeat and sync_cfg.every_n_turns is not None:
            sync_turn_messages = self.conversation.get_messages()[
                prepared.turn_anchor :
            ]
            missing = find_missing_memory_sync_targets(sync_turn_messages)
            if not missing:
                self._turns_since_memory_sync = 0
            else:
                self._turns_since_memory_sync += 1
                if self._turns_since_memory_sync >= sync_cfg.every_n_turns:
                    should_sync = True
            if prepared.debug:
                self.console.print_debug(
                    "memory-sync",
                    f"missing={bool(missing)}, "
                    f"counter={self._turns_since_memory_sync}/{sync_cfg.every_n_turns}",
                )
        elif prepared.debug:
            reason = "heartbeat" if is_system_heartbeat else "disabled"
            self.console.print_debug("memory-sync", f"skipped: {reason}")

        if not should_sync:
            return

        try:
            sync_client = getattr(self, "memory_sync_client", None) or self.client
            if prepared.debug:
                dispatch = "memory_sync" if sync_client is not self.client else "brain"
                self.console.print_debug("memory-sync", f"dispatch client={dispatch}")
            _run_memory_sync_side_channel(
                sync_client,
                self.conversation,
                self.builder,
                tools,
                self.registry,
                self.console,
                missing_targets=missing,  # type: ignore[possibly-undefined]
                turns_accumulated=self._turns_since_memory_sync,
                max_retries=sync_cfg.max_retries,
                on_before_tool_call=prepared.turn_memory_snapshot.capture_from_tool_call,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
            )
            self._turns_since_memory_sync = 0
            if prepared.debug:
                self.console.print_debug("memory-sync", "done")
        except ContextLengthExceededError:
            if prepared.debug:
                self.console.print_debug(
                    "memory-sync",
                    "skipped: context length exceeded",
                )
        except Exception:
            if prepared.debug:
                self.console.print_debug("memory-sync", "side-channel failed")

    def _flush_pending_outbound(self) -> None:
        """Print and clear buffered outbound messages from send_message."""
        if self.turn_context is None:
            return
        for msg in self.turn_context.pending_outbound:
            self.console.print_outbound(
                msg.channel,
                msg.recipient,
                msg.body,
                attachments=msg.attachments or None,
            )
        self.turn_context.pending_outbound.clear()

    def _maybe_run_pre_compaction_memory_sync(
        self,
        *,
        prepared: _PreparedTurn,
        tools: list[ToolDefinition],
        is_cancel_requested: Callable[[], bool] | None,
        on_cancel_pending: Callable[[], None] | None,
    ) -> None:
        """Sync memory before soft compaction discards turn history."""
        if not self._is_soft_limit_exceeded() or self._turns_since_memory_sync <= 0:
            return

        sync_turn_messages = self.conversation.get_messages()[prepared.turn_anchor :]
        pre_compact_missing = find_missing_memory_sync_targets(sync_turn_messages)
        if not pre_compact_missing:
            return

        try:
            sync_client = getattr(self, "memory_sync_client", None) or self.client
            _run_memory_sync_side_channel(
                sync_client,
                self.conversation,
                self.builder,
                tools,
                self.registry,
                self.console,
                missing_targets=pre_compact_missing,
                turns_accumulated=self._turns_since_memory_sync,
                max_retries=self.config.tools.memory_sync.max_retries,
                on_before_tool_call=prepared.turn_memory_snapshot.capture_from_tool_call,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
            )
            self._turns_since_memory_sync = 0
            if prepared.debug:
                self.console.print_debug("memory-sync", "pre-compaction sync done")
        except Exception:
            if prepared.debug:
                self.console.print_debug(
                    "memory-sync",
                    "pre-compaction sync failed",
                )

    def _handle_context_overflow_retry(
        self,
        *,
        prepared: _PreparedTurn,
        user_input: str,
        output: Callable[[str | None], None],
        channel: str,
        sender: str | None,
        timestamp: datetime | None,
    ) -> tuple[bool, str | None]:
        """Archive, compact, and retry a turn once after context overflow."""
        _rollback_turn_memory_changes(
            prepared.turn_memory_snapshot,
            console=self.console,
            debug=prepared.debug,
        )
        self.conversation.truncate_to(prepared.pre_turn_anchor)

        _run_memory_archive(
            self.agent_os_dir,
            self.config.maintenance.archive,
            self.console,
        )
        self.builder.reload_boot_files()
        keep_turns = self.config.context.preserve_turns
        result = self._compact_context(
            preserve_turns=keep_turns,
            trigger="overflow_retry",
        )
        via = f" via {result.source_label}" if result.source_label else ""
        details = (
            f"compacted {result.removed_messages} messages"
            if result.removed_messages > 0
            else "compacted context"
        )
        self.console.print_warning(
            "Token limit exceeded. "
            f"{details}{via}; retrying once...",
        )

        retry_prepared = self._prepare_retry_turn_attempt(
            user_input,
            channel=channel,
            sender=sender,
            timestamp=timestamp,
            turn_metadata=prepared.turn_metadata,
            message_overlay=prepared.message_overlay,
        )
        try:
            final_content = self._execute_turn_attempt(
                prepared=retry_prepared,
                output=output,
                channel=channel,
                sender=sender,
                enable_memory_sync=False,
                flush_pending_outbound=False,
            )
            return True, final_content
        except ContextLengthExceededError:
            self._last_turn_failure_category = "context-length"
            _rollback_turn_memory_changes(
                retry_prepared.turn_memory_snapshot,
                console=self.console,
                debug=prepared.debug,
            )
            self.conversation.truncate_to(prepared.pre_turn_anchor)
            self.console.print_error(
                "Context still too large after emergency overflow compaction."
            )
            return False, None
        except Exception as e:
            self._last_turn_failure_category = _classify_turn_failure(e)
            _rollback_turn_memory_changes(
                retry_prepared.turn_memory_snapshot,
                console=self.console,
                debug=prepared.debug,
            )
            self.console.print_error(_surface_error_message(e))
            _inject_brain_failure_record(
                self.conversation,
                retry_prepared.turn_anchor,
                e,
                memory_rolled_back=True,
            )
            if self.session_mgr is not None:
                self.session_mgr.rewrite_messages(self.conversation.get_messages())
            return False, None

    def _record_turn_debug_summary(
        self,
        *,
        status: TurnRunStatus,
        final_content: str | None,
        turn_anchor: int | None,
    ) -> None:
        """Persist one debug turn summary when session logging is enabled."""
        if self.session_mgr is None:
            return

        turn_messages: list[SessionEntry]
        if turn_anchor is None:
            turn_messages = []
        else:
            turn_messages = self.conversation.get_messages()[turn_anchor:]

        max_prompt_tokens = self._turn_token_usage.max_prompt_tokens
        soft_limit_exceeded = bool(
            max_prompt_tokens is not None
            and max_prompt_tokens > self._soft_max_prompt_tokens
        )
        self.session_mgr.finish_turn(
            status=status,
            final_content=final_content,
            failure_category=self._last_turn_failure_category,
            soft_limit_exceeded=soft_limit_exceeded,
            turn_messages=turn_messages,
            checkpoint_messages=self.conversation.get_messages(),
        )
        # Persist render cache so prompt cache prefix survives restart.
        try:
            self.session_mgr.write_render_cache(
                self.builder.export_render_cache(),
                self.builder.boot_fingerprint(),
            )
        except Exception:
            pass  # best-effort; messages.jsonl is the authority

    def run_turn(
        self,
        user_input: str,
        *,
        output_fn: Callable[[str | None], None] | None = None,
        channel: str = "cli",
        sender: str | None = None,
        timestamp: datetime | None = None,
        turn_metadata: dict[str, object] | None = None,
    ) -> TurnRunStatus:
        """Process one user turn.

        Full lifecycle:
        1. Add user message to conversation
        2. Responder (LLM + tool loop)
        3. Memory sync side-channel
        4. Memory archive + backup hooks

        Handles ContextLengthExceededError (emergency compact + single retry),
        KeyboardInterrupt (patch incomplete tool calls), and general exceptions
        (rollback memory + restore conversation).

        Args:
            output_fn: Callback for the final response.  When *None* the
                direct-call path is used with channel display sections.
            channel: Channel name for display (direct-call path only).
            sender: Sender name for display (direct-call path only).
        Returns:
            Turn completion status for queue-level ack / requeue decisions.
        """
        self._last_turn_failure_category = None
        # Guard against dangling tool calls left by a previous hard
        # interruption; providers reject tool_use without a tool result.
        dangling = self.conversation.remove_dangling_tool_calls()
        if dangling:
            logger.warning(
                "Removed %d dangling tool-call record(s) before turn", dangling
            )
            if self.session_mgr is not None:
                self.session_mgr.rewrite_messages(self.conversation.get_messages())
        initial_turn_metadata = (
            dict(turn_metadata)
            if turn_metadata is not None
            else dict(self.turn_context.metadata)
            if self.turn_context is not None
            else None
        )
        effective_turn_metadata = _ensure_turn_runtime_metadata(
            channel=channel,
            timestamp=timestamp,
            metadata=initial_turn_metadata,
        )
        output = self._make_turn_output(
            user_input,
            output_fn=output_fn,
            channel=channel,
            sender=sender,
        )
        if self.session_mgr is not None:
            self.session_mgr.start_turn(
                channel=channel,
                sender=sender,
                inbound_kind=_classify_inbound_kind(
                    channel=channel,
                    metadata=effective_turn_metadata,
                ),
                input_text=user_input,
                input_timestamp=timestamp,
                turn_metadata=effective_turn_metadata,
            )
        prepared: _PreparedTurn | None = None
        self._last_proactive_yield = None

        try:
            prepared = self._prepare_turn_attempt(
                user_input,
                channel=channel,
                sender=sender,
                timestamp=timestamp,
                turn_metadata=effective_turn_metadata,
            )
            final_content = self._execute_turn_attempt(
                prepared=prepared,
                output=output,
                channel=channel,
                sender=sender,
                enable_memory_sync=True,
                flush_pending_outbound=True,
            )
            self._record_turn_debug_summary(
                status="completed",
                final_content=final_content,
                turn_anchor=prepared.turn_anchor,
            )
            return "completed"

        except ContextLengthExceededError:
            overflow_recovered, final_content = self._handle_context_overflow_retry(
                prepared=prepared,
                user_input=user_input,
                output=output,
                channel=channel,
                sender=sender,
                timestamp=timestamp,
            )
            self._record_turn_debug_summary(
                status="completed" if overflow_recovered else "failed",
                final_content=final_content,
                turn_anchor=prepared.turn_anchor,
            )
            return "completed" if overflow_recovered else "failed"

        except KeyboardInterrupt:
            # Preserve completed work; patch incomplete tool calls for API consistency
            if prepared is not None:
                _patch_interrupted_tool_calls(self.conversation, prepared.turn_anchor)
            if self.session_mgr is not None:
                self.session_mgr.rewrite_messages(self.conversation.get_messages())
            self._record_turn_debug_summary(
                status="interrupted",
                final_content=None,
                turn_anchor=prepared.turn_anchor if prepared is not None else None,
            )
            self.console.print_info("Interrupted.")
            return "interrupted"

        except ProactiveTurnYield as e:
            self._last_proactive_yield = e
            self._record_turn_debug_summary(
                status="completed",
                final_content=None,
                turn_anchor=prepared.turn_anchor if prepared is not None else None,
            )
            self.console.print_info(_surface_error_message(e))
            return "completed"

        except Exception as e:
            _rollback_turn_memory_changes(
                prepared.turn_memory_snapshot,
                console=self.console,
                debug=prepared.debug,
            )
            self._last_turn_failure_category = _classify_turn_failure(e)
            self.console.print_error(_surface_error_message(e))
            _inject_brain_failure_record(
                self.conversation,
                prepared.turn_anchor,
                e,
                memory_rolled_back=True,
            )
            if self.session_mgr is not None:
                self.session_mgr.rewrite_messages(self.conversation.get_messages())
            self._record_turn_debug_summary(
                status="failed",
                final_content=None,
                turn_anchor=prepared.turn_anchor if prepared is not None else None,
            )
            return "failed"

    @staticmethod
    def _coerce_non_negative_int(value: object, default: int) -> int:
        """Best-effort int coercion for config values used in queue retry logic."""
        if isinstance(value, int) and value >= 0:
            return value
        return default

    def _failed_inbound_retry_config(self) -> tuple[int, int, bool]:
        """Return failed inbound requeue runtime config."""
        app_cfg = getattr(self.config, "app", None)
        if app_cfg is None:
            return 0, 0, False
        limit = self._coerce_non_negative_int(
            getattr(app_cfg, "turn_failure_requeue_limit", 0),
            0,
        )
        delay_seconds = self._coerce_non_negative_int(
            getattr(app_cfg, "turn_failure_requeue_delay_seconds", 0),
            0,
        )
        requeue_non_retryable = bool(
            getattr(app_cfg, "requeue_non_retryable_turn_failures", False)
        )
        return limit, delay_seconds, requeue_non_retryable

    def _requeue_failed_inbound(
        self,
        msg: InboundMessage,
        receipt: Path | None,
    ) -> bool:
        """Re-enqueue a failed inbound turn with delay, bounded by config."""
        if self._queue is None:
            return False

        limit, base_delay_seconds, _ = self._failed_inbound_retry_config()
        retry_count = self._coerce_non_negative_int(
            msg.metadata.get(_TURN_FAILURE_REQUEUE_COUNT_KEY),
            0,
        )
        if retry_count >= limit:
            return False

        next_retry = retry_count + 1
        delay_seconds = base_delay_seconds * next_retry
        retry_msg = InboundMessage(
            channel=msg.channel,
            content=msg.content,
            priority=msg.priority,
            sender=msg.sender,
            metadata=dict(msg.metadata),
            timestamp=msg.timestamp,
            not_before=tz_now() + timedelta(seconds=delay_seconds),
        )
        retry_msg.metadata[_TURN_FAILURE_REQUEUE_COUNT_KEY] = next_retry
        retry_msg.metadata.setdefault(
            _TURN_FAILURE_FIRST_FAILED_AT_KEY,
            tz_now().isoformat(),
        )
        if receipt is None:
            self._queue.put(retry_msg)
        else:
            self._queue.requeue_active(receipt, retry_msg)
        retry_at = retry_msg.not_before.isoformat() if retry_msg.not_before else "now"
        self.console.print_warning(
            "Brain turn failed; re-enqueued inbound "
            f"retry {next_retry}/{limit} at {retry_at}.",
        )
        logger.warning(
            "Re-enqueued failed inbound %s retry %d/%d for %s",
            msg.channel,
            next_retry,
            limit,
            retry_at,
        )
        return True

    def _requeue_yielded_scheduled_turn(
        self,
        msg: InboundMessage,
        receipt: Path | None,
        *,
        scope_id: str,
    ) -> bool:
        """Requeue a yielded scheduled turn for short-delay reevaluation."""
        if self._queue is None:
            return False

        reason = msg.metadata.get("scheduled_reason")
        if not isinstance(reason, str) or not reason.strip():
            return False

        reeval_at = tz_now() + _PROACTIVE_YIELD_REEVALUATE_DELAY
        display_time = reeval_at.astimezone(get_tz()).strftime("%Y-%m-%d %H:%M")
        metadata = dict(msg.metadata)
        metadata["yielded_scope_id"] = scope_id
        retry_count = metadata.get("yield_reschedule_count", 0)
        metadata["yield_reschedule_count"] = (
            retry_count + 1 if isinstance(retry_count, int) and retry_count >= 0 else 1
        )
        retry_msg = InboundMessage(
            channel="system",
            content=(
                "[SCHEDULED]\n"
                f"Reason: {reason}\n"
                f"Scheduled at: {display_time}\n\n"
                "A newer inbound for the same conversation arrived before delivery. "
                "Reevaluate whether action is still needed."
            ),
            priority=msg.priority,
            sender="system",
            metadata=metadata,
            timestamp=reeval_at,
            not_before=reeval_at,
        )
        if receipt is None:
            self._queue.put(retry_msg)
        else:
            self._queue.requeue_active(receipt, retry_msg)
        logger.info(
            "Yielded scheduled turn requeued for reevaluation: scope=%s at=%s",
            scope_id,
            reeval_at.isoformat(),
        )
        return True

    def graceful_exit(self) -> None:
        """Handle graceful exit.

        Keeps finalize + archive only; backup and session cleanup are
        handled by the daily maintenance window.
        """
        if self.session_mgr is not None:
            self.session_mgr.finalize("completed")

        if self.agent_os_dir and self.config:
            _run_memory_archive(
                self.agent_os_dir,
                self.config.maintenance.archive,
                self.console,
            )

        self.console.print_goodbye()

    def _reload_system_prompt(self) -> bool:
        """Refresh the system prompt so date-sensitive text stays current."""
        try:
            raw_prompt = self.workspace.get_system_prompt("brain")
        except FileNotFoundError:
            logger.warning("System prompt reload failed: file not found")
            return False
        raw_prompt = raw_prompt.replace(
            "{agent_os_dir}",
            str(self.agent_os_dir),
        )
        policy = getattr(self, "brain_prompt_policy", None)
        if policy is not None:
            raw_prompt = policy.resolve(raw_prompt)
        self.builder.update_system_prompt(raw_prompt)
        return True

    def _perform_reload_resources(self) -> None:
        """Reload system prompt plus both boot-file cache tiers from disk."""
        try:
            prompt_reloaded = self._reload_system_prompt()
            self.builder.reload_boot_files()
            if prompt_reloaded:
                self.console.print_info(
                    "System prompt, boot files, and tool boot files reloaded."
                )
            else:
                self.console.print_warning(
                    "Boot files and tool boot files reloaded; "
                    "system prompt file not found."
                )
        except Exception as e:
            logger.warning("Resource reload failed: %s", e)
            self.console.print_error(_surface_error_message(e))

    def _perform_reload_system_prompt(self) -> None:
        """Reload only the system prompt on the agent thread."""
        try:
            if self._reload_system_prompt():
                self.console.print_info("System prompt reloaded.")
            else:
                self.console.print_error(
                    "Failed to reload system prompt: file not found."
                )
        except Exception as e:
            logger.warning("System prompt reload failed: %s", e)
            self.console.print_error(_surface_error_message(e))

    def _rotate_session(self) -> None:
        """Finalize the current session and persist current conversation to a new one."""
        if self.session_mgr is None:
            return
        self.session_mgr.finalize("refreshed")
        self.session_mgr.create(self.user_id, self.display_name)
        self.conversation.set_on_message(self.session_mgr.append_message)
        for entry in self.conversation.get_messages():
            self.session_mgr.append_message(entry)
        self.session_mgr.write_checkpoint(self.conversation.get_messages())

    def _perform_new_session(self) -> None:
        """Archive memory and rotate into a fresh empty session."""
        try:
            _run_memory_archive(
                self.agent_os_dir,
                self.config.maintenance.archive,
                self.console,
            )
            self._turns_since_memory_sync = 0
            self.conversation.clear()
            if self.turn_context is not None:
                self.turn_context.clear()
            self._reload_system_prompt()
            self.builder.reload_boot_files()
            self._rotate_session()
            self.console.print_info("Started a new session after archive.")
        except Exception as e:
            logger.warning("New session rotation failed: %s", e)

    def _perform_context_refresh(self, preserve_turns: int = 2) -> None:
        """Compact conversation, reload boot files, rotate session."""
        try:
            # 1. Compact conversation
            result = self._compact_context(
                preserve_turns=preserve_turns,
                trigger="context_refresh",
            )

            # 2. Re-resolve system prompt with current date
            self._reload_system_prompt()

            # 3. Reload boot files from disk
            self.builder.reload_boot_files()

            # 4. Session rotation
            self._rotate_session()

            via = f" via {result.source_label}" if result.source_label else ""
            details = (
                f"{result.removed_messages} messages compacted"
                if result.removed_messages > 0
                else "context compacted"
            )
            self.console.print_info(
                f"Context refreshed: {details}{via}, "
                f"boot files reloaded, new session started."
            )
        except Exception as e:
            logger.warning("Context refresh failed: %s", e)

    def _perform_maintenance(self) -> None:
        """Run daily maintenance: archive -> context_refresh -> backup -> session_file_cleanup."""
        cfg = self.config.maintenance if self.config else None
        if cfg is None or not cfg.enabled:
            return

        logger.info("Daily maintenance started")
        try:
            # 1. Archive
            _run_memory_archive(
                self.agent_os_dir,
                cfg.archive,
                self.console,
            )

            # 2. Context refresh (compact + reload + session rotate)
            self._perform_context_refresh(
                preserve_turns=cfg.context_refresh.preserve_turns,
            )

            # 3. Backup (force=True: maintenance always backs up regardless of interval)
            if cfg.backup.enabled and self.memory_backup_mgr:
                try:
                    self.memory_backup_mgr.check_and_backup(force=True)
                except Exception as e:
                    logger.warning("Maintenance backup failed: %s", e)

            # 4. Session file cleanup
            if cfg.session_file_cleanup.enabled and self.agent_os_dir:
                try:
                    from ..session.cleanup import cleanup_sessions

                    cleanup_sessions(
                        self.agent_os_dir / "session",
                        retention_days=cfg.session_file_cleanup.retention_days,
                    )
                except Exception as e:
                    logger.warning("Maintenance session file cleanup failed: %s", e)

            # Mark scheduler so it doesn't re-trigger today
            if self._maintenance_scheduler:
                self._maintenance_scheduler.mark_done()

            self.console.print_info("Daily maintenance completed.")
        except Exception as e:
            logger.warning("Daily maintenance failed: %s", e)

    def _schedule_next_heartbeat(self, msg: InboundMessage) -> None:
        """Create the next recurring heartbeat after a successful turn."""
        from .adapters.scheduler import make_heartbeat_message, random_delay

        recur_spec = msg.metadata.get("recur_spec", "2h-5h")
        try:
            delay = random_delay(recur_spec)
        except ValueError:
            logger.warning("Invalid recur_spec %r; using default 2h-5h", recur_spec)
            delay = random_delay("2h-5h")

        next_time_raw = tz_now() + delay
        next_time = self._apply_quiet_hours(next_time_raw)
        next_msg = make_heartbeat_message(
            not_before=next_time,
            interval_spec=recur_spec,
        )
        self._queue.put(next_msg)
        delay_min = (next_time - tz_now()).total_seconds() / 60
        if delay_min >= 120:
            logger.info("Next heartbeat in %.1fh", delay_min / 60)
        else:
            logger.info("Next heartbeat in %.0fm", delay_min)

        self._maybe_schedule_pre_sleep_sync(was_deferred=next_time > next_time_raw)

    # -- Task/Note injection helpers -----------------------------------------

    def _inject_task_context(self, msg: InboundMessage, content: str) -> str:
        """Append pending task list to heartbeat/task-due messages."""
        task_store = getattr(self, "task_store", None)
        if task_store is None:
            return content
        is_heartbeat = bool(
            msg.metadata.get("system") and msg.metadata.get("recurring")
        )
        is_task_due = bool(msg.metadata.get("task_due"))
        if not is_heartbeat and not is_task_due:
            return content
        pending = task_store.list_pending()
        if not pending:
            return content
        task_block = task_store.format_task_list(pending)
        return f"{content}\n\n## Tasks ({len(pending)})\n{task_block}"

    def _inject_heartbeat_reliability_notice(
        self,
        msg: InboundMessage,
        content: str,
        *,
        processing_started_at: datetime,
    ) -> str:
        """Append per-turn heartbeat reliability guidance to recurring heartbeats."""
        is_heartbeat = bool(
            msg.metadata.get("system") and msg.metadata.get("recurring")
        )
        if not is_heartbeat:
            return content

        notices = [_HEARTBEAT_RELIABILITY_NOTICE]
        if self._earliest_next_heartbeat_hits_quiet_hours(
            msg,
            processing_started_at=processing_started_at,
        ):
            notices.append(_HEARTBEAT_QUIET_HOURS_NOTICE)
        return f"{content}\n\n" + "\n\n".join(notices)

    def _earliest_next_heartbeat_hits_quiet_hours(
        self,
        msg: InboundMessage,
        *,
        processing_started_at: datetime,
    ) -> bool:
        """Return True when the minimum next heartbeat delay lands in quiet hours."""
        recur_spec = msg.metadata.get("recur_spec")
        if not isinstance(recur_spec, str) or not recur_spec.strip():
            return False
        try:
            from .adapters.scheduler import parse_interval

            min_minutes, _ = parse_interval(recur_spec)
        except ValueError:
            return False

        heartbeat_cfg = getattr(self.config, "heartbeat", None)
        parsed_quiet_windows = getattr(heartbeat_cfg, "parsed_quiet_windows", None)
        if not callable(parsed_quiet_windows):
            return False
        try:
            windows = parsed_quiet_windows()
        except Exception:
            return False
        if not isinstance(windows, list) or not windows:
            return False

        from ..core.schema import is_in_quiet_hours

        earliest_next = processing_started_at + timedelta(minutes=min_minutes)
        return is_in_quiet_hours(earliest_next, windows, get_tz())

    def _inject_note_triggers(self, msg: InboundMessage, content: str) -> str:
        """Append [NOTE UPDATE] hint when user message matches note triggers."""
        note_store = getattr(self, "note_store", None)
        if note_store is None:
            return content
        # Only trigger on non-system user messages
        if msg.metadata.get("system"):
            return content
        matching = note_store.find_matching_triggers(msg.content)
        if not matching:
            return content
        lines = ["[NOTE UPDATE] The following notes may need updating:"]
        for note in matching:
            lines.append(f'- {note.key} (current: "{note.value}")')
        lines.append("Review and update these notes if the message indicates a change.")
        return f"{content}\n\n" + "\n".join(lines)

    def _defer_pending_heartbeat(self) -> None:
        """Push back pending heartbeat after a non-heartbeat turn.

        Resets the heartbeat timer using the same interval spec so the
        agent does not wake up immediately after real activity.
        """
        from .adapters.scheduler import make_heartbeat_message, random_delay

        was_deferred = False
        for filepath, msg in self._queue.scan_pending(channel="system"):
            if not msg.metadata.get("system") or not msg.metadata.get("recurring"):
                continue
            # Found the pending heartbeat; remove and re-create with fresh delay
            recur_spec = msg.metadata.get("recur_spec")
            if not recur_spec:
                adapter = self.adapters.get("system")
                recur_spec = getattr(adapter, "_interval", None) or "2h-5h"
            self._queue.remove_pending(filepath)
            delay = random_delay(recur_spec)
            next_time_raw = tz_now() + delay
            next_time = self._apply_quiet_hours(next_time_raw)
            next_msg = make_heartbeat_message(
                not_before=next_time,
                interval_spec=recur_spec,
            )
            self._queue.put(next_msg)
            was_deferred = next_time > next_time_raw
            delay_min = (next_time - tz_now()).total_seconds() / 60
            if delay_min >= 120:
                logger.info("Deferred heartbeat by %.1fh", delay_min / 60)
            else:
                logger.info("Deferred heartbeat by %.0fm", delay_min)
            break  # Only one heartbeat at a time

        self._maybe_schedule_pre_sleep_sync(was_deferred=was_deferred)

    def _apply_quiet_hours(self, dt: datetime) -> datetime:
        """Push *dt* past quiet hours if it falls within a blackout window."""
        from ..core.schema import is_in_quiet_hours, next_quiet_end

        windows = self.config.heartbeat.parsed_quiet_windows()
        if not windows:
            return dt
        tz = get_tz()
        if is_in_quiet_hours(dt, windows, tz):
            end = next_quiet_end(dt, windows, tz)
            logger.info("Heartbeat deferred past quiet hours to %s", end)
            return end
        return dt

    def _maybe_schedule_pre_sleep_sync(self, *, was_deferred: bool) -> None:
        """Schedule (or replace) a pre-sleep memory sync when heartbeat was
        deferred past quiet hours.  The sync fires while the prompt cache
        is still warm (within the 1h TTL) so the side-channel call is cheap.
        """
        if self._queue is None:
            return

        # Remove any existing pre-sleep sync message first (dedup)
        for filepath, msg in self._queue.scan_pending(channel="system"):
            if msg.metadata.get("pre_sleep_sync"):
                self._queue.remove_pending(filepath)
                break

        if not was_deferred:
            return

        from .adapters.scheduler import make_pre_sleep_sync_message

        sync_time = tz_now() + timedelta(minutes=30)
        self._queue.put(
            make_pre_sleep_sync_message(
                not_before=sync_time,
            )
        )
        logger.info("Scheduled pre-sleep sync at %s", sync_time.isoformat())

    def _handle_pre_sleep_sync(self, receipt: Path | None) -> None:
        """Run memory sync side-channel only.  No brain turn."""
        if self._turns_since_memory_sync <= 0:
            logger.info("Pre-sleep sync: nothing to sync (counter=0)")
            if self._queue is not None and receipt is not None:
                self._queue.ack(receipt)
            return

        from ..memory.tool_analysis import MEMORY_SYNC_TARGETS

        sync_client = getattr(self, "memory_sync_client", None) or self.client
        tools = self.registry.get_definitions()
        try:
            _run_memory_sync_side_channel(
                sync_client,
                self.conversation,
                self.builder,
                tools,
                self.registry,
                self.console,
                missing_targets=list(MEMORY_SYNC_TARGETS),
                turns_accumulated=self._turns_since_memory_sync,
                max_retries=self.config.tools.memory_sync.max_retries,
            )
            self._turns_since_memory_sync = 0
            self.console.print_info("Pre-sleep memory sync completed")
        except Exception:
            logger.warning("Pre-sleep sync failed", exc_info=True)

        if self._queue is not None and receipt is not None:
            self._queue.ack(receipt)

    # ------------------------------------------------------------------
    # Queue-based interface
    # ------------------------------------------------------------------

    def register_adapter(self, adapter: ChannelAdapter) -> None:
        """Register a channel adapter."""
        self.adapters[adapter.channel_name] = adapter

    def enqueue(
        self,
        msg: InboundMessage
        | ShutdownSentinel
        | NewSessionSentinel
        | ReloadSentinel
        | ReloadSystemPromptSentinel,
    ) -> None:
        """Push a message into the persistent queue (thread-safe)."""
        if self._queue is None:
            raise RuntimeError("No queue configured; call AgentCore with queue=...")
        if isinstance(msg, InboundMessage):
            shared_state_store = getattr(self, "shared_state_store", None)
            scope_resolver = getattr(self, "scope_resolver", None)
            if shared_state_store is not None and scope_resolver is not None:
                scope_id = scope_resolver.inbound(msg)
                if scope_id:
                    msg.metadata = dict(msg.metadata)
                    msg.metadata["scope_id"] = scope_id
                    msg.metadata["anchor_shared_rev"] = (
                        shared_state_store.get_current_rev(scope_id)
                    )
        self._queue.put(msg)

    def request_shutdown(self, *, graceful: bool = True) -> None:
        """Signal the agent to shut down via the queue."""
        self.enqueue(ShutdownSentinel(graceful=graceful))

    def request_new_session(self) -> None:
        """Signal the agent to rotate into a fresh session."""
        self.enqueue(NewSessionSentinel())

    def request_reload(self) -> None:
        """Signal the agent to reload prompt and boot resources."""
        self.enqueue(ReloadSentinel())

    def request_reload_system_prompt(self) -> None:
        """Signal the agent to reload only the system prompt."""
        self.enqueue(ReloadSystemPromptSentinel())

    def run(self) -> None:
        """Queue-based main loop.  Blocks until shutdown.

        Starts all registered adapters, then pulls messages from the
        persistent priority queue.  Each message is processed through
        ``run_turn`` and the response is routed back to the originating
        adapter.
        """
        if self._queue is None:
            raise RuntimeError("No queue configured; call AgentCore with queue=...")

        for adapter in self.adapters.values():
            adapter.start(self)

        # Start daily maintenance scheduler
        maint_cfg = self.config.maintenance if self.config else None
        if maint_cfg and maint_cfg.enabled:
            self._maintenance_scheduler = _MaintenanceScheduler(
                self._queue,
                maint_cfg,
            )
            self._maintenance_scheduler.start()

        # Start delayed message promotion thread
        self._queue.start_promotion()

        try:
            while True:
                msg, receipt = self._queue.get()
                if isinstance(msg, ShutdownSentinel):
                    if msg.graceful:
                        self.graceful_exit()
                    break
                if isinstance(msg, MaintenanceSentinel):
                    if self._queue.pending_inbound_count() == 0:
                        self._perform_maintenance()
                    continue
                if isinstance(msg, NewSessionSentinel):
                    self._perform_new_session()
                    continue
                if isinstance(msg, ReloadSentinel):
                    self._perform_reload_resources()
                    continue
                if isinstance(msg, ReloadSystemPromptSentinel):
                    self._perform_reload_system_prompt()
                    continue
                self._process_inbound(msg, receipt)
        except KeyboardInterrupt:
            self.graceful_exit()
        finally:
            self._queue.stop_promotion()
            if self._maintenance_scheduler:
                self._maintenance_scheduler.stop()
            for adapter in self.adapters.values():
                adapter.stop()

    def _process_inbound(self, msg: InboundMessage, receipt: Path | None) -> None:
        """Process one inbound message through the turn pipeline."""
        self._maybe_rescan_skills()

        inbound_scope = (
            self.copilot_runtime.inbound_scope(msg)
            if self.copilot_runtime is not None
            else nullcontext()
        )

        with inbound_scope:
            # Pre-sleep sync: memory sync only, no brain turn
            if msg.metadata.get("pre_sleep_sync"):
                self._handle_pre_sleep_sync(receipt)
                return

            turn_status: TurnRunStatus | None = None
            pre_turn_len = len(self.conversation.get_messages())
            proactive_yield: ProactiveTurnYield | None = None
            self._last_turn_failure_category = None
            processing_started_at = tz_now()
            turn_metadata = build_turn_timing_metadata(
                channel=msg.channel,
                metadata=msg.metadata,
                event_timestamp=msg.timestamp,
                processing_started_at=processing_started_at,
            )
            try:
                if self.turn_context is not None:
                    self.turn_context.set_inbound(
                        msg.channel, msg.sender, turn_metadata
                    )

                # Notify all adapters so terminal-owning ones (CLI) can suspend
                for a in self.adapters.values():
                    a.on_turn_start(msg.channel)

                self.console.print_inbound(
                    msg.channel,
                    msg.sender,
                    msg.content,
                    ts=msg.timestamp,
                )
                self.console.print_processing(msg.channel, msg.sender)

                # Inner thoughts callback: display on console only, never sent.
                # Actual message delivery happens via the send_message tool.
                def _thoughts(content: str | None) -> None:
                    self.console.print_inner_thoughts(msg.channel, msg.sender, content)

                # Dynamic content injection before run_turn
                turn_content = msg.content
                turn_content = self._inject_task_context(msg, turn_content)
                turn_content = self._inject_heartbeat_reliability_notice(
                    msg,
                    turn_content,
                    processing_started_at=processing_started_at,
                )
                turn_content = self._inject_note_triggers(msg, turn_content)

                turn_status = self.run_turn(
                    turn_content,
                    output_fn=_thoughts,
                    channel=msg.channel,
                    sender=msg.sender,
                    timestamp=msg.timestamp,
                    turn_metadata=turn_metadata,
                )
            finally:
                proactive_yield = getattr(self, "_last_proactive_yield", None)
                self._last_proactive_yield = None
                had_turn_context = self.turn_context is not None
                had_send_message = False
                if self.turn_context is not None:
                    had_send_message = bool(self.turn_context.sent_hashes)
                    self.turn_context.clear()

                turn_messages = self.conversation.get_messages()[pre_turn_len:]
                is_heartbeat_like = bool(msg.metadata.get("system"))
                is_scheduled = (
                    msg.channel == "system" and "scheduled_reason" in msg.metadata
                )
                is_task_due = msg.channel == "system" and bool(
                    msg.metadata.get("task_due")
                )
                is_discord_review = msg.channel == "discord" and msg.metadata.get(
                    "source"
                ) in {
                    "guild_review",
                    "guild_mention_review",
                }

                should_evict = False
                evict_reason = ""
                if turn_status == "completed" and had_turn_context:
                    if is_heartbeat_like and not had_send_message:
                        should_evict = True
                        evict_reason = "silent heartbeat/startup"
                    elif is_scheduled or is_task_due:
                        effects = analyze_turn_effects(
                            turn_messages,
                            had_send_message=had_send_message,
                        )
                        if effects.is_scheduled_noop:
                            should_evict = True
                            evict_reason = (
                                "noop task due turn"
                                if is_task_due
                                else "noop scheduled turn"
                            )
                    elif is_discord_review and not had_send_message:
                        effects = analyze_turn_effects(
                            turn_messages,
                            had_send_message=had_send_message,
                        )
                        if effects.is_scheduled_noop:
                            should_evict = True
                            evict_reason = "noop discord review turn"

                if should_evict:
                    evicted = self.conversation.truncate_to(pre_turn_len)
                    logger.debug(
                        "Evicted %s (%d messages)",
                        evict_reason,
                        evicted,
                    )
                scheduled_yield_requeued = False
                if (
                    proactive_yield is not None
                    and turn_status == "completed"
                    and (is_scheduled or is_task_due)
                ):
                    scheduled_yield_requeued = self._requeue_yielded_scheduled_turn(
                        msg,
                        receipt,
                        scope_id=proactive_yield.scope_id,
                    )
                if self._queue is not None and turn_status == "completed":
                    if not scheduled_yield_requeued:
                        self._queue.ack(receipt)
                    # Auto-schedule next heartbeat for recurring messages
                    if msg.metadata.get("recurring"):
                        self._schedule_next_heartbeat(msg)
                    elif not scheduled_yield_requeued:
                        self._defer_pending_heartbeat()
                elif self._queue is not None and turn_status == "failed":
                    _, _, requeue_non_retryable = self._failed_inbound_retry_config()
                    should_requeue_failed_turn = _should_requeue_failed_turn(
                        self._last_turn_failure_category,
                        requeue_non_retryable=requeue_non_retryable,
                    )
                    requeued_failed_turn = (
                        should_requeue_failed_turn
                        and self._requeue_failed_inbound(msg, receipt)
                    )
                    if requeued_failed_turn:
                        pass
                    else:
                        if msg.metadata.get("recurring"):
                            self._schedule_next_heartbeat(msg)
                        if not should_requeue_failed_turn:
                            self.console.print_warning(
                                "Brain turn failed with a non-retryable error; acknowledging inbound without queue replay."
                            )
                        self._queue.ack(receipt)
                elif self._queue is not None and turn_status == "interrupted":
                    self._queue.ack(receipt)
                for a in self.adapters.values():
                    a.on_turn_complete()
