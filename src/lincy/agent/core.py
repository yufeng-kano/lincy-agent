"""Agent core logic: responder + memory sync.

Extracted from cli/app.py to decouple agent logic from CLI adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from .adapters.protocol import ChannelAdapter
    from .compactor_agent import CompactorAgent
    from .conscience import ConscienceAgent
    from .skill_check import SkillCheckAgent
    from .scope import ScopeResolver
    from .shared_state import SharedStateStore
    from ..brain_prompt_policy import BrainPromptPolicy
    from ..llm.providers.copilot_runtime import CopilotRuntime
    from ..session.schema import SessionEntry
    from ..worker.runner import WorkerRunner

from ..context import ContextBuilder, Conversation
from ..core.schema import AppConfig
from ..llm import LLMResponse
from ..llm.base import ConversationCompactionClient, LLMClient
from ..llm.schema import (
    ContextLengthExceededError,
    Message,
    ToolCall,
    ToolDefinition,
)
from ..memory import (
    ARTIFACT_REGISTRY_TARGET,
    check_and_archive_buffers,
    curate_queue_via_worker,
    digest_day_via_worker,
    find_missing_artifact_registry_paths,
    find_missing_memory_sync_targets,
    scan_over_budget_files,
)
from ..memory.backup import MemoryBackupManager
from ..session import SessionManager
from ..skills import rebuild_personal_skills_index
from ..timezone_utils import get_tz, now as tz_now
from ..tools import FilteredToolRegistry, ToolRegistry
from ..tui.sink import UiSink
from ..workspace import WorkspaceManager
from . import responder as _responder
from .heartbeat import (
    apply_quiet_hours,
    make_heartbeat_message,
    parse_interval,
    random_delay,
    schedule_pre_sleep_sync,
)
from .maintenance import MaintenanceScheduler
from .compaction import ContextCompactionResult, ContextCompactor
from .requeue import RequeuePolicy, TurnFailureCategory, classify_turn_failure, should_requeue_failed_turn
from .token_telemetry import LatestTokenStatus, TokenTelemetry, TurnTokenUsage
from .queue import PersistentPriorityQueue
from .responder import (
    _CommonGroundTurnDebug,
    _build_common_ground_overlay,
    _build_dynamic_turn_overlay,
    _compose_message_overlays,
)
from .run_helpers import (
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
from .turn_context import ProactiveTurnYield, TurnContext
from .turn_effects import analyze_turn_effects
from ..turn_timing import TURN_PROCESSING_STARTED_AT_KEY, build_turn_timing_metadata

# Re-exported for backward compatibility with tests importing from
# lincy.agent.core. AgentCore itself does not use every symbol here.
from .turn_runtime import (
    _TurnMemorySnapshot,
    _build_artifact_registry_sync_reminder,
    _inject_brain_failure_record,
    _patch_interrupted_tool_calls,
    _rollback_turn_memory_changes,
    _run_memory_archive,
    _run_memory_sync_side_channel,
)
from .ui_event_console import AgentUiPort, UiEventConsole

logger = logging.getLogger(__name__)
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
        worker_runner: "WorkerRunner | None" = None,
        conversation_compaction_client: ConversationCompactionClient | None = None,
        compactor_agent: "CompactorAgent | None" = None,
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
        self.worker_runner = worker_runner
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
        self.compactor_agent = compactor_agent
        self.copilot_runtime = copilot_runtime
        self.brain_prompt_policy = brain_prompt_policy
        self.skill_registry = SkillGovernanceRegistry.load(
            agent_os_dir,
            governance_config=self.config.tools.skill_governance,
        )
        self._maintenance_scheduler: MaintenanceScheduler | None = None
        self._turns_since_memory_sync: int = 0
        self.adapters: dict[str, ChannelAdapter] = {}
        brain_cfg = self.config.agents.get("brain")
        self._brain_provider = brain_cfg.llm.provider if brain_cfg is not None else ""
        self._soft_max_prompt_tokens = self.config.context.soft_max_prompt_tokens
        self._latest_token_status = LatestTokenStatus()
        self._turn_token_usage = TurnTokenUsage()
        self._token_telemetry = TokenTelemetry(self)
        self._compactor = ContextCompactor(self)
        self._requeue_policy = RequeuePolicy(self)
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

    def _telemetry(self) -> TokenTelemetry:
        telemetry = getattr(self, "_token_telemetry", None)
        if telemetry is None:
            telemetry = TokenTelemetry(self)
            self._token_telemetry = telemetry
        return telemetry

    def _compaction(self) -> ContextCompactor:
        compactor = getattr(self, "_compactor", None)
        if compactor is None:
            compactor = ContextCompactor(self)
            self._compactor = compactor
        return compactor

    def _requeue(self) -> RequeuePolicy:
        policy = getattr(self, "_requeue_policy", None)
        if policy is None:
            policy = RequeuePolicy(self)
            self._requeue_policy = policy
        return policy

    def _reset_turn_token_usage(self) -> None:
        self._telemetry().reset()

    def _record_brain_response_usage(self, response: LLMResponse) -> None:
        self._telemetry().record(response)

    def _finalize_turn_token_status(self) -> None:
        self._telemetry().finalize()

    def get_token_status_text(self) -> str:
        return self._telemetry().status_text()

    def _is_soft_limit_exceeded(self) -> bool:
        state = self._latest_token_status
        return bool(state.usage_available and state.prompt_tokens is not None and state.prompt_tokens > self._soft_max_prompt_tokens)

    def _record_compaction_result(self, result: ContextCompactionResult) -> None:
        self._compaction().record_result(result)

    def _apply_soft_prompt_compaction(self) -> None:
        self._compaction().apply_soft_prompt_compaction()

    def _compact_context_local(self, preserve_turns: int, *, trigger: str, fallback: bool = False) -> ContextCompactionResult:
        return self._compaction().compact_local(preserve_turns, trigger=trigger, fallback=fallback)

    def _compact_context_remote(self, *, trigger: str) -> ContextCompactionResult:
        return self._compaction().compact_remote(trigger=trigger)

    def _compact_context(self, *, preserve_turns: int, trigger: str) -> ContextCompactionResult:
        return self._compaction().compact(preserve_turns=preserve_turns, trigger=trigger)

    def run_manual_compact(self) -> ContextCompactionResult:
        return self._compaction().run_manual_compact()

    def _make_turn_output(
        self,
        user_input: str,
        *,
        output_fn: Callable[[str | None], None] | None,
        channel: str,
        sender: str | None,
        timestamp: datetime | None = None,
    ) -> Callable[[str | None], None]:
        """Return the per-turn output callback."""
        if output_fn is not None:
            return output_fn

        self.console.print_inbound(channel, sender, user_input, ts=timestamp)
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

    def _run_memory_sync(
        self,
        *,
        tools: list[ToolDefinition],
        missing_targets: list[str],
        turns_accumulated: int = 1,
        reminder_text: str | None = None,
        on_before_tool_call: Callable[[ToolCall], None] | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
        on_cancel_pending: Callable[[], None] | None = None,
    ) -> None:
        """Run memory sync with the configured side-channel client."""
        sync_client = getattr(self, "memory_sync_client", None) or self.client
        _run_memory_sync_side_channel(
            sync_client,
            self.conversation,
            self.builder,
            tools,
            self.registry,
            self.console,
            missing_targets=missing_targets,
            turns_accumulated=turns_accumulated,
            max_retries=self.config.tools.memory_sync.max_retries,
            reminder_text=reminder_text,
            on_before_tool_call=on_before_tool_call,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
        )

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
            self._run_memory_sync(
                tools=tools,
                missing_targets=[ARTIFACT_REGISTRY_TARGET],
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
            self._run_memory_sync(
                tools=tools,
                missing_targets=missing,  # type: ignore[possibly-undefined]
                turns_accumulated=self._turns_since_memory_sync,
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
            self._run_memory_sync(
                tools=tools,
                missing_targets=pre_compact_missing,
                turns_accumulated=self._turns_since_memory_sync,
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
            self._last_turn_failure_category = classify_turn_failure(e)
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
            timestamp=timestamp,
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
            self._last_turn_failure_category = classify_turn_failure(e)
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

    def _failed_inbound_retry_config(self) -> tuple[int, int, bool]:
        return self._requeue().failed_inbound_retry_config()

    def _requeue_failed_inbound(self, msg: InboundMessage, receipt: Path | None) -> bool:
        return self._requeue().requeue_failed_inbound(msg, receipt)

    def _requeue_yielded_scheduled_turn(self, msg: InboundMessage, receipt: Path | None, *, scope_id: str) -> bool:
        return self._requeue().requeue_yielded_scheduled_turn(msg, receipt, scope_id=scope_id)

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
        """Run daily maintenance: archive -> curate -> context refresh -> backup -> cleanup."""
        cfg = self.config.maintenance if self.config else None
        if cfg is None or not cfg.enabled:
            return

        logger.info("Daily maintenance started")
        try:
            # Curation owns temp-memory archival so a full day only leaves with
            # its digest committed in the same maintenance run.
            if cfg.curate.enabled and self.worker_runner is not None:
                try:
                    # Full scan before consuming the queue: memory_edit only
                    # queues files on write, so a stock over-budget file that
                    # is never written again would otherwise sit unqueued
                    # forever (design doc component 2b).
                    queued = scan_over_budget_files(
                        self.agent_os_dir, self.config.tools.memory_edit.warnings
                    )
                    if queued:
                        logger.info(
                            "Curation scan queued %d stock over-budget file(s)",
                            len(queued),
                        )
                except Exception as e:
                    logger.warning("Maintenance curation scan failed: %s", e)
                try:
                    result = check_and_archive_buffers(
                        self.agent_os_dir,
                        cfg.archive,
                        curate_config=cfg.curate,
                        digest_day=lambda d, c, m: digest_day_via_worker(
                            self.worker_runner, d, c, m
                        ),
                    )
                    if result.archived:
                        self.console.print_info(f"Memory archived: {result.summary}")
                except Exception as e:
                    logger.warning("Maintenance temp-memory curation failed: %s", e)
                try:
                    curate_queue_via_worker(self.worker_runner, self.agent_os_dir)
                except Exception as e:
                    logger.warning("Maintenance queued file curation failed: %s", e)
            elif cfg.curate.enabled:
                logger.warning("Memory curation is enabled but no worker is available")
            else:
                _run_memory_archive(
                    self.agent_os_dir,
                    cfg.archive,
                    self.console,
                )

            # Context refresh (compact + reload + session rotate)
            self._perform_context_refresh(
                preserve_turns=cfg.context_refresh.preserve_turns,
            )

            # Backup (force=True: maintenance always backs up regardless of interval)
            if cfg.backup.enabled and self.memory_backup_mgr:
                try:
                    self.memory_backup_mgr.check_and_backup(force=True)
                except Exception as e:
                    logger.warning("Maintenance backup failed: %s", e)

            # Session file cleanup
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
        was_deferred = False
        for filepath, msg in self._queue.scan_pending(channel="system"):
            if not msg.metadata.get("system") or not msg.metadata.get("recurring"):
                continue
            # Found the pending heartbeat; remove and re-create with fresh delay
            recur_spec = msg.metadata.get("recur_spec")
            if not recur_spec:
                adapter = self.adapters.get("system")
                recur_spec = getattr(adapter, "interval", None) or "2h-5h"
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
        return apply_quiet_hours(dt, self.config.heartbeat.parsed_quiet_windows())

    def _maybe_schedule_pre_sleep_sync(self, *, was_deferred: bool) -> None:
        """Schedule (or replace) a pre-sleep memory sync when heartbeat was
        deferred past quiet hours.  The sync fires while the prompt cache
        is still warm (within the 1h TTL) so the side-channel call is cheap.
        """
        if self._queue is None:
            return

        schedule_pre_sleep_sync(queue=self._queue, was_deferred=was_deferred)

    def _handle_pre_sleep_sync(self, receipt: Path | None) -> None:
        """Run memory sync side-channel only.  No brain turn."""
        if self._turns_since_memory_sync <= 0:
            logger.info("Pre-sleep sync: nothing to sync (counter=0)")
            if self._queue is not None and receipt is not None:
                self._queue.ack(receipt)
            return

        from ..memory.tool_analysis import MEMORY_SYNC_TARGETS

        tools = self.registry.get_definitions()
        try:
            self._run_memory_sync(
                tools=tools,
                missing_targets=list(MEMORY_SYNC_TARGETS),
                turns_accumulated=self._turns_since_memory_sync,
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
            self._maintenance_scheduler = MaintenanceScheduler(
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

                _thoughts = self._make_turn_output(
                    msg.content,
                    output_fn=None,
                    channel=msg.channel,
                    sender=msg.sender,
                    timestamp=msg.timestamp,
                )

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
                evict_if_noop = bool(msg.metadata.get("evict_if_noop"))

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
                    elif evict_if_noop and not had_send_message:
                        effects = analyze_turn_effects(
                            turn_messages,
                            had_send_message=had_send_message,
                        )
                        if effects.is_scheduled_noop:
                            should_evict = True
                            evict_reason = "noop review turn"

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
                    should_requeue = should_requeue_failed_turn(
                        self._last_turn_failure_category,
                        requeue_non_retryable=requeue_non_retryable,
                    )
                    requeued_failed_turn = (
                        should_requeue
                        and self._requeue_failed_inbound(msg, receipt)
                    )
                    if requeued_failed_turn:
                        pass
                    else:
                        if msg.metadata.get("recurring"):
                            self._schedule_next_heartbeat(msg)
                        if not should_requeue:
                            self.console.print_warning(
                                "Brain turn failed with a non-retryable error; acknowledging inbound without queue replay."
                            )
                        self._queue.ack(receipt)
                elif self._queue is not None and turn_status == "interrupted":
                    self._queue.ack(receipt)
                for a in self.adapters.values():
                    a.on_turn_complete()
