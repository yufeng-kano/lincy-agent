"""Brain responder loop and staged-planning orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .note_store import NoteStore
    from .skill_check import SkillCheckAgent
    from .shared_state import SharedStateStore
    from .skill_governance import SkillGovernanceRegistry
    from .turn_context import TurnContext
    from ..session.schema import SessionEntry
from .turn_context import ProactiveTurnYield

from ..context import ContextBuilder, Conversation
from ..context.cache_breakpoints import advance_cache_breakpoint
from ..core.schema import AppConfig, ToolsConfig
from ..llm import LLMResponse
from ..llm.base import LLMClient
from ..llm.schema import ContentPart, Message, ToolCall, ToolDefinition
from ..memory import is_failed_memory_edit_result, summarize_memory_edit_failure
from ..tools import ToolRegistry, is_claude_code_stream_json_command
from .run_helpers import (
    _debug_print_responder_output,
    _emit_reasoning_block_if_needed,
    _raise_if_cancel_requested,
    _surface_error_message,
)
from .skill_governance import (
    build_skill_deferral_text,
    build_skill_prerequisite_messages,
)
from .staged_planning import (
    STAGE1_SYNTHETIC_TOOL_NAME,
    build_plan_context_message,
    build_stage1_findings_for_conversation,
    build_stage1_findings_overlay_message,
    build_stage3_plan_overlay_message,
    format_stage2_plan_for_tui,
    run_stage1_information_gathering,
    run_stage2_brain_planning,
)
from .turn_overlay import build_dynamic_turn_overlay_text
from .ui_event_console import AgentUiPort

logger = logging.getLogger(__name__)
_PROACTIVE_SKILL_CHECK_MAX_SKILLS = 1
_STATE_COMMIT_TOOLS = frozenset({"agent_note", "memory_edit", "schedule_action"})
_READ_ONLY_STATE_TOOL_ACTIONS = {
    "agent_note": frozenset({"list"}),
    "schedule_action": frozenset({"list"}),
}


def _state_commit_tool_repeat_warning(tool_name: str) -> str:
    return (
        f"Error: {tool_name} was already used successfully in this turn. "
        "SERIOUS WARNING: batch all agent_note, memory_edit, and schedule_action "
        "state commits into one call per tool per turn. Do not create unnecessary "
        "API cost by repeating state-only tool calls. list actions are read-only; "
        "mutating calls must use batch_update/requests/batch_add/batch_remove. "
        "Stop the tool loop now unless the previous call failed."
    )


def _read_only_state_tool_repeat_warning(tool_name: str) -> str:
    return (
        f"Error: repeated read-only {tool_name} call detected. "
        "The previous identical read-only state lookup already succeeded, "
        "so repeating it will only add prompt tokens and delay the user. "
        "Stop the tool loop now unless the previous call failed."
    )


@dataclass(frozen=True)
class _CommonGroundTurnDebug:
    """Stable per-turn common-ground debug snapshot captured during overlay build."""

    scope_id: str | None = None
    anchor_shared_rev: int | None = None
    current_shared_rev: int | None = None
    store_available: bool = False


def _is_error_tool_result(result: object) -> bool:
    """Return True when a tool result is an error ToolResult."""
    from ..tools.registry import ToolResult

    return isinstance(result, ToolResult) and result.is_error


def _is_state_commit_tool_call(tool_call: ToolCall) -> bool:
    """Return True when this call intends to mutate durable state."""
    if tool_call.name == "memory_edit":
        return True
    if tool_call.name == "schedule_action":
        action = tool_call.arguments.get("action")
        return action in {"batch_add", "batch_remove"}
    if tool_call.name != "agent_note":
        return False
    action = tool_call.arguments.get("action")
    return action in {"create", "batch_update", "remove"}


def _is_read_only_state_tool_call(tool_call: ToolCall) -> bool:
    actions = _READ_ONLY_STATE_TOOL_ACTIONS.get(tool_call.name)
    if actions is None:
        return False
    return tool_call.arguments.get("action") in actions


def _is_successful_state_commit(tool_call: ToolCall, result: object) -> bool:
    """Return True when a state commit tool should count against the turn."""
    if not _is_state_commit_tool_call(tool_call):
        return False
    if _is_error_tool_result(result):
        return False
    content = getattr(result, "content", None)
    if isinstance(content, str) and content.lstrip().startswith("Error:"):
        return False
    if (
        tool_call.name == "memory_edit"
        and isinstance(content, str)
        and is_failed_memory_edit_result(content)
    ):
        return False
    return True


def _is_successful_tool_result(result: object) -> bool:
    if _is_error_tool_result(result):
        return False
    content = getattr(result, "content", None)
    return not (isinstance(content, str) and content.lstrip().startswith("Error:"))


class _StateCommitTurnTracker:
    """Track per-turn state commit tools that already succeeded."""

    def __init__(self) -> None:
        self._successful_tools: set[str] = set()

    def should_block(self, tool_call: ToolCall) -> bool:
        if not _is_state_commit_tool_call(tool_call):
            return False
        return tool_call.name in self._successful_tools

    def observe_result(self, tool_call: ToolCall, result: object) -> None:
        if _is_successful_state_commit(tool_call, result):
            self._successful_tools.add(tool_call.name)


class _ReadOnlyStateToolRepeatTracker:
    """Stop consecutive identical read-only state lookups within one turn."""

    def __init__(self) -> None:
        self._last_signature: tuple[str, tuple[tuple[str, object], ...]] | None = None

    @staticmethod
    def _signature(tool_call: ToolCall) -> tuple[str, tuple[tuple[str, object], ...]]:
        return (tool_call.name, tuple(sorted(tool_call.arguments.items())))

    def should_block(self, tool_call: ToolCall) -> bool:
        if not _is_read_only_state_tool_call(tool_call):
            return False
        return self._signature(tool_call) == self._last_signature

    def observe_result(self, tool_call: ToolCall, result: object) -> None:
        if _is_read_only_state_tool_call(tool_call) and _is_successful_tool_result(result):
            self._last_signature = self._signature(tool_call)
        else:
            self._last_signature = None


def _format_memory_edit_failure_summaries(summaries: list[str]) -> str:
    """Format per-call memory_edit failure summaries for warning output."""
    if not summaries:
        return "unknown_failure"
    unique: list[str] = []
    for item in summaries:
        if item not in unique:
            unique.append(item)
    text = " | ".join(unique[:2])
    if len(unique) > 2:
        text += " | +"
    return text


def _make_synthetic_message_overlay(
    extra_messages: list[Message] | tuple[Message, ...],
) -> Callable[[list[Message]], list[Message]]:
    """Return an overlay callback that appends synthetic context messages."""
    extras = tuple(extra_messages)

    def _overlay(messages: list[Message]) -> list[Message]:
        return [*messages, *extras]

    return _overlay


def _append_text_block(content: str, block: str) -> str:
    """Append a stable note block to the current-turn user message."""
    if not content:
        return block
    return f"{content}\n\n{block}"


def _make_latest_user_text_overlay(
    extra_text: str,
) -> Callable[[list[Message]], list[Message]]:
    """Return an overlay callback that appends text to the latest user turn."""
    block = extra_text.strip()

    def _overlay(messages: list[Message]) -> list[Message]:
        for idx in range(len(messages) - 1, -1, -1):
            message = messages[idx]
            if message.role != "user":
                continue
            if isinstance(message.content, str):
                updated = list(messages)
                updated[idx] = message.model_copy(update={
                    "content": _append_text_block(message.content, block),
                })
                return updated
            if isinstance(message.content, list):
                parts = list(message.content)
                for part_idx in range(len(parts) - 1, -1, -1):
                    part = parts[part_idx]
                    if part.type != "text":
                        continue
                    parts[part_idx] = part.model_copy(update={
                        "text": _append_text_block(part.text, block),
                    })
                    updated = list(messages)
                    updated[idx] = message.model_copy(update={"content": parts})
                    return updated
        return [*messages, Message(role="user", content=block)]

    return _overlay


def _build_dynamic_turn_overlay(
    *,
    entry: "SessionEntry",
    builder: ContextBuilder,
    note_store: "NoteStore | None",
) -> Callable[[list[Message]], list[Message]] | None:
    """Build the responder overlay for the per-turn dynamic blocks.

    Assembles [Runtime Context], [Timing Notice], [Decision Reminder], and
    [Agent Notes] (see agent/turn_overlay.py) and returns an overlay that
    appends them to the latest user message of the outgoing request only.
    These blocks used to be baked into ContextBuilder's rendered/frozen
    messages on every turn; they now live only on the wire, never in
    Conversation or the builder's render cache. Returns None when none of
    the blocks have anything to say (e.g. no notes, no boot dir, timing
    unremarkable, decision reminder disabled).
    """
    text = build_dynamic_turn_overlay_text(
        entry=entry,
        agent_os_dir=builder.agent_os_dir,
        decision_reminder_enabled=builder.decision_reminder_enabled,
        decision_reminder_files=builder.decision_reminder_files,
        decision_reminder_core_values=builder.decision_reminder_core_values,
        note_store=note_store,
    )
    if not text:
        return None
    return _make_latest_user_text_overlay(text)


def _prepare_turn_call_messages(
    messages: list[Message],
    message_overlay: Callable[[list[Message]], list[Message]] | None = None,
) -> list[Message]:
    """Advance the latest-turn breakpoint before any per-turn overlays."""
    prepared = _advance_responder_cache_breakpoint(messages)
    if message_overlay is not None:
        prepared = message_overlay(prepared)
    return prepared


def _compose_message_overlays(
    first: Callable[[list[Message]], list[Message]] | None,
    second: Callable[[list[Message]], list[Message]] | None,
) -> Callable[[list[Message]], list[Message]] | None:
    """Compose two message overlays in order."""
    if first is None:
        return second
    if second is None:
        return first

    def _overlay(messages: list[Message]) -> list[Message]:
        return second(first(messages))

    return _overlay


# Compatibility aliases for scripts/tests that import private responder names.
_advance_responder_cache_breakpoint = advance_cache_breakpoint


def _load_plan_context_files(
    *,
    rel_paths: list[str],
    builder: ContextBuilder,
    console: AgentUiPort,
) -> list[tuple[str, str]]:
    """Load plan_context_files from agent_os_dir and warn on failure."""
    agent_os_dir = getattr(builder, "agent_os_dir", None)
    if not isinstance(agent_os_dir, Path):
        if rel_paths:
            console.print_warning(
                "plan_context_files unavailable: agent_os_dir is not set.",
                indent=2,
            )
        return []

    loaded: list[tuple[str, str]] = []
    for rel_path in rel_paths:
        try:
            content = (agent_os_dir / rel_path).read_text(encoding="utf-8")
            loaded.append((rel_path, content))
        except Exception as error:
            console.print_warning(
                f"plan_context_files: skipping {rel_path}: "
                f"{_surface_error_message(error)}",
                indent=2,
            )
    return loaded


def _maybe_defer_tool_round_for_skills(
    *,
    response: LLMResponse,
    conversation: Conversation,
    console: AgentUiPort,
    skill_registry: "SkillGovernanceRegistry | None",
) -> dict[str, object] | None:
    """Inject missing skill guides and defer the current tool round once."""
    if skill_registry is None:
        return None

    loaded_skill_names = skill_registry.loaded_skill_names_from_conversation(conversation)
    requirements = skill_registry.find_missing_requirements(
        response.tool_calls,
        loaded_skill_names=loaded_skill_names,
    )
    if not requirements:
        return None

    injected = skill_registry.build_injected_guides(requirements)
    if not injected:
        return None

    from ..tools.registry import ToolResult

    deferral_text = build_skill_deferral_text(
        missing_skill_names=[item.skill_name for item in injected],
    )
    tool_results_this_round: dict[str, object] = {}
    for tool_call in response.tool_calls:
        console.print_tool_call(tool_call)
        result = ToolResult(deferral_text, is_error=True)
        console.print_tool_result(tool_call, result.content)
        conversation.add_tool_result(tool_call.id, tool_call.name, result.content)
        tool_results_this_round[tool_call.id] = result

    for item in injected:
        call_msg, result_msg = build_skill_prerequisite_messages(item)
        conversation.add_assistant_with_tools(None, call_msg.tool_calls or [])
        conversation.add_tool_result(
            result_msg.tool_call_id or item.call.id,
            result_msg.name or item.call.name,
            result_msg.content or "",
        )
    return tool_results_this_round


def _flatten_message_text(content: object) -> str:
    """Return plain text from a message content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.text
            for part in content
            if isinstance(part, ContentPart) and part.type == "text" and part.text
        )
    return ""


def _latest_user_text_from_conversation(conversation: Conversation) -> str:
    """Return the latest raw user text from conversation history."""
    for entry in reversed(conversation.get_messages()):
        if entry.role != "user":
            continue
        text = _flatten_message_text(entry.content).strip()
        if text:
            return text
    return ""


def _format_skill_guide_info(prefix: str, skill_names: list[str]) -> str:
    """Format a concise info line for proactive skill-guide state."""
    if not skill_names:
        return prefix
    return f"{prefix}: {', '.join(skill_names)}"


def _maybe_inject_proactive_skill_guides(
    *,
    messages: list[Message],
    conversation: Conversation,
    builder: ContextBuilder,
    console: AgentUiPort,
    skill_registry: "SkillGovernanceRegistry | None",
    skill_check_agent: "SkillCheckAgent | None",
) -> list[Message]:
    """Run the prompt-only skill checker before the first brain response."""
    if skill_registry is None or skill_check_agent is None:
        return messages

    latest_user_text = _latest_user_text_from_conversation(conversation)
    if not latest_user_text:
        return messages

    loaded_skill_names = skill_registry.loaded_skill_names_from_conversation(conversation)
    catalog = skill_registry.list_skill_catalog()
    if not catalog:
        return messages

    selected_skill_names = skill_check_agent.pick_skill_names(
        latest_user_input=latest_user_text,
        skills=catalog,
        loaded_skill_names=loaded_skill_names,
        max_skills=_PROACTIVE_SKILL_CHECK_MAX_SKILLS,
    )
    if not selected_skill_names:
        if console.debug:
            console.print_debug("skill-check", "no proactive skill injection")
        return messages

    requirements = skill_registry.requirements_for_skill_names(
        selected_skill_names,
        loaded_skill_names=loaded_skill_names,
    )
    requirement_names = {item.skill_name for item in requirements}
    already_loaded_names = [
        name for name in selected_skill_names
        if name not in requirement_names
    ]
    if already_loaded_names:
        console.print_info(
            _format_skill_guide_info(
                "Skill guide already loaded",
                already_loaded_names,
            )
        )
    injected = skill_registry.build_injected_guides(requirements)
    if not injected:
        return messages

    for item in injected:
        call_msg, result_msg = build_skill_prerequisite_messages(item)
        conversation.add_assistant_with_tools(None, call_msg.tool_calls or [])
        conversation.add_tool_result(
            result_msg.tool_call_id or item.call.id,
            result_msg.name or item.call.name,
            result_msg.content or "",
        )
    console.print_info(
        _format_skill_guide_info(
            "Loaded skill guide",
            [item.skill_name for item in injected],
        )
    )
    if console.debug:
        joined = ", ".join(item.skill_name for item in injected)
        console.print_debug("skill-check", f"proactively injected: {joined}")
    return builder.build(conversation)


def _run_responder(
    client: LLMClient,
    messages: list[Message],
    tools: list[ToolDefinition],
    conversation: Conversation,
    builder: ContextBuilder,
    registry: ToolRegistry,
    console: AgentUiPort,
    on_before_tool_call: Callable[[ToolCall], None] | None = None,
    memory_edit_allow_failure: bool = False,
    max_iterations: int = 10,
    memory_edit_turn_retry_limit: int = 3,
    is_cancel_requested: Callable[[], bool] | None = None,
    on_cancel_pending: Callable[[], None] | None = None,
    message_overlay: Callable[[list[Message]], list[Message]] | None = None,
    on_model_response: Callable[[LLMResponse], None] | None = None,
    thinking_channel: str | None = None,
    thinking_sender: str | None = None,
    tools_config: ToolsConfig | None = None,
    skill_registry: "SkillGovernanceRegistry | None" = None,
    turn_context: "TurnContext | None" = None,
    check_preempt: Callable[[], bool] | None = None,
    max_preempts: int = 2,
) -> LLMResponse:
    """Run responder with the tool-call loop and return the final response."""
    messages = _prepare_turn_call_messages(messages, message_overlay)
    _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
    with console.spinner():
        response = client.chat_with_tools(messages, tools)
    if on_model_response is not None:
        on_model_response(response)
    _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
    _debug_print_responder_output(console, response, label="responder")
    _emit_reasoning_block_if_needed(
        console,
        response,
        channel=thinking_channel,
        sender=thinking_sender,
    )

    memory_edit_turn_fail_streak = 0
    preempt_count = 0
    iterations = 0
    state_commit_tracker = _StateCommitTurnTracker()
    read_only_state_tool_tracker = _ReadOnlyStateToolRepeatTracker()
    while response.has_tool_calls():
        iterations += 1
        if iterations > max_iterations:
            logger.warning(
                "Responder loop exceeded %d iterations; breaking.",
                max_iterations,
            )
            console.print_warning(
                f"Tool loop exceeded {max_iterations} iterations; stopping.",
            )
            break
        chunk = response.content or ""
        if chunk.strip():
            console.print_assistant(chunk)

        round_anchor = len(conversation)
        conversation.add_assistant_with_tools(
            response.content,
            response.tool_calls,
            reasoning_content=response.reasoning_content,
            reasoning_details=response.reasoning_details,
        )

        failed_memory_edit_this_round = False
        repeated_state_commit_this_round = False
        repeated_read_only_state_tool_this_round = False
        memory_edit_failure_summaries: list[str] = []
        tool_results_this_round: dict[str, object] = {}
        deferred_results = _maybe_defer_tool_round_for_skills(
            response=response,
            conversation=conversation,
            console=console,
            skill_registry=skill_registry,
        )
        if deferred_results is not None:
            tool_results_this_round = deferred_results
            messages = builder.build(conversation)
            messages = _prepare_turn_call_messages(messages, message_overlay)
            _raise_if_cancel_requested(
                is_cancel_requested,
                on_pending=on_cancel_pending,
            )
            with console.spinner():
                response = client.chat_with_tools(messages, tools)
            if on_model_response is not None:
                on_model_response(response)
            _raise_if_cancel_requested(
                is_cancel_requested,
                on_pending=on_cancel_pending,
            )
            _debug_print_responder_output(console, response, label="responder")
            _emit_reasoning_block_if_needed(
                console,
                response,
                channel=thinking_channel,
                sender=thinking_sender,
            )
            continue

        # Split tool calls into concurrent-safe and sequential.
        _is_safe = getattr(registry, "is_concurrency_safe", None)
        concurrent_calls = [
            tc for tc in response.tool_calls
            if (
                tc.name not in _STATE_COMMIT_TOOLS
                and _is_safe
                and registry.has_tool(tc.name)
                and _is_safe(tc.name)
            )
        ]
        sequential_calls = [
            tc for tc in response.tool_calls
            if tc not in concurrent_calls
        ]

        # Submit concurrent-safe calls to thread pool.
        concurrent_results: dict[str, object] = {}
        if concurrent_calls:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(concurrent_calls)) as pool:
                futures = {
                    pool.submit(registry.execute, tc): tc
                    for tc in concurrent_calls
                }
                for future in futures:
                    tc = futures[future]
                    console.print_tool_call(tc)
                for future in futures:
                    tc = futures[future]
                    concurrent_results[tc.id] = future.result()

        # Execute sequential calls with full logic (preemption, spinner, etc.).
        preempted = False
        for tc_idx, tool_call in enumerate(sequential_calls):
            _raise_if_cancel_requested(
                is_cancel_requested,
                on_pending=on_cancel_pending,
            )
            if not registry.has_tool(tool_call.name):
                from ..tools.registry import ToolResult

                result = ToolResult(
                    f"Error: Unknown tool '{tool_call.name}'",
                    is_error=True,
                )
                conversation.add_tool_result(
                    tool_call.id,
                    tool_call.name,
                    result.content,
                )
                tool_results_this_round[tool_call.id] = result
                continue

            # -- Preempt check: before a side-effect tool, see if
            #    fresher inbound has arrived so the LLM can reconsider.
            if (
                check_preempt is not None
                and preempt_count < max_preempts
                and registry.is_side_effect(tool_call.name)
                and check_preempt()
            ):
                from ..tools.registry import ToolResult as _TR

                preempt_count += 1
                preempted = True
                logger.info(
                    "Preempted before side-effect tool %s (count=%d/%d)",
                    tool_call.name,
                    preempt_count,
                    max_preempts,
                )
                console.print_info(
                    f"New inbound detected; preempting {tool_call.name}",
                )
                # Fill cancelled results for this and remaining sequential calls.
                for remaining in sequential_calls[tc_idx:]:
                    cancel_result = _TR(
                        "Error: preempted — new message arrived; action cancelled.",
                        is_error=True,
                    )
                    console.print_tool_call(remaining)
                    console.print_tool_result(remaining, cancel_result.content)
                    conversation.add_tool_result(
                        remaining.id,
                        remaining.name,
                        cancel_result.content,
                    )
                    tool_results_this_round[remaining.id] = cancel_result
                break

            console.print_tool_call(tool_call)
            if on_before_tool_call is not None:
                on_before_tool_call(tool_call)

            if state_commit_tracker.should_block(tool_call):
                from ..tools.registry import ToolResult

                repeated_state_commit_this_round = True
                result = ToolResult(
                    _state_commit_tool_repeat_warning(tool_call.name),
                    is_error=True,
                )
                console.print_warning(
                    f"{tool_call.name} was called more than once in this turn; "
                    "stopping to avoid unnecessary API cost.",
                )
                console.print_tool_result(tool_call, result.content)
                conversation.add_tool_result(
                    tool_call.id,
                    tool_call.name,
                    result.content,
                )
                tool_results_this_round[tool_call.id] = result
                continue

            if read_only_state_tool_tracker.should_block(tool_call):
                from ..tools.registry import ToolResult

                repeated_read_only_state_tool_this_round = True
                result = ToolResult(
                    _read_only_state_tool_repeat_warning(tool_call.name),
                    is_error=True,
                )
                console.print_warning(
                    f"{tool_call.name} repeated the same read-only lookup; "
                    "stopping to avoid unnecessary API cost.",
                )
                console.print_tool_result(tool_call, result.content)
                conversation.add_tool_result(
                    tool_call.id,
                    tool_call.name,
                    result.content,
                )
                tool_results_this_round[tool_call.id] = result
                continue

            shell_command = tool_call.arguments.get("command")
            skip_spinner = (
                tool_call.name == "gui_task"
                or (
                    tool_call.name == "execute_shell"
                    and console.show_tool_use
                    and isinstance(shell_command, str)
                    and is_claude_code_stream_json_command(shell_command)
                )
            )
            if skip_spinner:
                result = registry.execute(tool_call)
            else:
                with console.spinner("Executing..."):
                    result = registry.execute(tool_call)
            console.print_tool_result(tool_call, result.content)
            conversation.add_tool_result(tool_call.id, tool_call.name, result.content)
            tool_results_this_round[tool_call.id] = result
            state_commit_tracker.observe_result(tool_call, result)
            read_only_state_tool_tracker.observe_result(tool_call, result)
            if turn_context is not None and turn_context.proactive_yield is not None:
                scope_id = turn_context.proactive_yield.scope_id
                raise ProactiveTurnYield(scope_id)
            _raise_if_cancel_requested(
                is_cancel_requested,
                on_pending=on_cancel_pending,
            )
            if (
                tool_call.name == "memory_edit"
                and isinstance(result.content, str)
                and is_failed_memory_edit_result(result.content)
            ):
                failed_memory_edit_this_round = True
                summary = summarize_memory_edit_failure(result.content)
                if summary:
                    memory_edit_failure_summaries.append(summary)

        # Collect concurrent results in original order.
        for tc in concurrent_calls:
            result = concurrent_results[tc.id]
            console.print_tool_result(tc, result.content)
            conversation.add_tool_result(tc.id, tc.name, result.content)
            tool_results_this_round[tc.id] = result

        if preempted:
            # Roll back the round, then re-add with cleaned assistant
            # content to preserve completed tool results while stripping
            # stale draft text (e.g. "I'll send that now").
            if (response.content or "").strip():
                console.print_info(
                    "(above assistant text was for a preempted action)",
                )
            conversation.truncate_to(round_anchor)
            conversation.add_assistant_with_tools(
                None,
                response.tool_calls,
                reasoning_content=response.reasoning_content,
                reasoning_details=response.reasoning_details,
            )
            for tc in response.tool_calls:
                tr = tool_results_this_round.get(tc.id)
                if tr is not None:
                    conversation.add_tool_result(tc.id, tc.name, tr.content)
            response.content = None
            response.tool_calls = []
            return response

        if repeated_state_commit_this_round or repeated_read_only_state_tool_this_round:
            response.content = None
            response.tool_calls = []
            return response

        if failed_memory_edit_this_round:
            memory_edit_turn_fail_streak += 1
            failure_detail = _format_memory_edit_failure_summaries(
                memory_edit_failure_summaries,
            )
            if memory_edit_turn_fail_streak >= memory_edit_turn_retry_limit:
                if memory_edit_allow_failure:
                    console.print_warning(
                        "memory_edit turn-level retries exhausted"
                        f" ({failure_detail}); failed "
                        f"{memory_edit_turn_fail_streak} time(s); "
                        "allow_failure=true, continuing turn.",
                    )
                    break
                raise RuntimeError(
                    "memory_edit turn-level retries exhausted "
                    f"({failure_detail}); failed "
                    f"{memory_edit_turn_fail_streak} time(s); fail-closed for this turn."
                )
            console.print_warning(
                "memory_edit failed this round "
                f"({failure_detail}); retrying turn "
                f"({memory_edit_turn_fail_streak}/{memory_edit_turn_retry_limit})",
                indent=2,
            )
        else:
            memory_edit_turn_fail_streak = 0

        messages = builder.build(conversation)
        messages = _advance_responder_cache_breakpoint(messages)
        if message_overlay is not None:
            messages = message_overlay(messages)
        _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
        with console.spinner():
            response = client.chat_with_tools(messages, tools)
        if on_model_response is not None:
            on_model_response(response)
        _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
        _debug_print_responder_output(console, response, label="responder")
        _emit_reasoning_block_if_needed(
            console,
            response,
            channel=thinking_channel,
            sender=thinking_sender,
        )

    return response


def _run_brain_responder(
    *,
    client: LLMClient,
    messages: list[Message],
    tools: list[ToolDefinition],
    conversation: Conversation,
    builder: ContextBuilder,
    registry: ToolRegistry,
    console: AgentUiPort,
    config: AppConfig,
    channel: str,
    sender: str | None,
    on_before_tool_call: Callable[[ToolCall], None] | None = None,
    memory_edit_allow_failure: bool = False,
    max_iterations: int = 10,
    memory_edit_turn_retry_limit: int = 3,
    is_cancel_requested: Callable[[], bool] | None = None,
    on_cancel_pending: Callable[[], None] | None = None,
    message_overlay: Callable[[list[Message]], list[Message]] | None = None,
    on_model_response: Callable[[LLMResponse], None] | None = None,
    run_responder_fn: Callable[..., LLMResponse] | None = None,
    stage1_gather_fn: Callable[..., object] = run_stage1_information_gathering,
    stage2_plan_fn: Callable[..., object | None] = run_stage2_brain_planning,
    skill_registry: "SkillGovernanceRegistry | None" = None,
    skill_check_agent: "SkillCheckAgent | None" = None,
    turn_context: "TurnContext | None" = None,
    check_preempt: Callable[[], bool] | None = None,
    max_preempts: int = 2,
) -> LLMResponse:
    """Run the brain responder, optionally using staged planning."""
    tools_cfg = (
        config.tools
        if isinstance(getattr(config, "tools", None), ToolsConfig)
        else None
    )
    if run_responder_fn is None:
        run_responder_fn = _run_responder
    messages = _maybe_inject_proactive_skill_guides(
        messages=messages,
        conversation=conversation,
        builder=builder,
        console=console,
        skill_registry=skill_registry,
        skill_check_agent=skill_check_agent,
    )

    brain_cfg = config.agents.get("brain")
    staged = getattr(brain_cfg, "staged_planning", None)
    batch_guidance_enabled = bool(
        getattr(config.features.send_message_batch_guidance, "enabled", False)
    )
    if staged is None or not staged.enabled:
        return run_responder_fn(
            client,
            messages,
            tools,
            conversation,
            builder,
            registry,
            console,
            on_before_tool_call=on_before_tool_call,
            memory_edit_allow_failure=memory_edit_allow_failure,
            max_iterations=max_iterations,
            memory_edit_turn_retry_limit=memory_edit_turn_retry_limit,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
            message_overlay=message_overlay,
            on_model_response=on_model_response,
            thinking_channel=channel,
            thinking_sender=sender,
            tools_config=tools_cfg,
            skill_registry=skill_registry,
            turn_context=turn_context,
            check_preempt=check_preempt,
            max_preempts=max_preempts,
        )

    def raise_cancel() -> None:
        _raise_if_cancel_requested(
            is_cancel_requested,
            on_pending=on_cancel_pending,
        )

    overlayed_messages = _prepare_turn_call_messages(messages, message_overlay)
    stage1_max_iterations = max(1, min(staged.gather_max_iterations, max_iterations))
    has_prior_findings = any(
        getattr(entry, "name", None) == STAGE1_SYNTHETIC_TOOL_NAME
        for entry in conversation.get_messages()
    )

    try:
        console.print_info("Stage 1/3: gather")
        stage1 = stage1_gather_fn(
            client=client,
            messages=overlayed_messages,
            all_tools=tools,
            registry=registry,
            console=console,
            raise_if_cancel_requested=raise_cancel,
            max_iterations=stage1_max_iterations,
            skip_memory_search_gate=has_prior_findings,
        )
        if console.debug:
            console.print_debug(
                "staged-plan",
                f"stage1 tool_calls={stage1.tool_calls} "
                f"transcript_chars={len(stage1.transcript)}",
            )

        if (
            stage1.findings_text
            and stage1.findings_text != "(no stage1 tools available)"
        ):
            stage1_call, stage1_content = build_stage1_findings_for_conversation(
                stage1.findings_text,
            )
            conversation.add_assistant_with_tools(None, [stage1_call])
            conversation.add_tool_result(
                stage1_call.id,
                stage1_call.name,
                stage1_content,
            )

        console.print_info("Stage 2/3: plan")
        stage2_messages = list(overlayed_messages)
        plan_context_loaded = _load_plan_context_files(
            rel_paths=staged.plan_context_files,
            builder=builder,
            console=console,
        )
        plan_context_msg = build_plan_context_message(plan_context_loaded)
        if plan_context_msg is not None:
            stage2_messages.append(plan_context_msg)
        stage2 = stage2_plan_fn(
            client=client,
            messages=stage2_messages,
            stage1=stage1,
            all_tools=tools,
            registry=registry,
            console=console,
            raise_if_cancel_requested=raise_cancel,
            send_message_batch_guidance=batch_guidance_enabled,
            max_iterations=max_iterations,
        )
        if stage2 is None:
            console.print_warning(
                "Stage 2 planning failed; falling back to legacy responder loop.",
                indent=2,
            )
            return run_responder_fn(
                client,
                messages,
                tools,
                conversation,
                builder,
                registry,
                console,
                on_before_tool_call=on_before_tool_call,
                memory_edit_allow_failure=memory_edit_allow_failure,
                max_iterations=max_iterations,
                memory_edit_turn_retry_limit=memory_edit_turn_retry_limit,
                is_cancel_requested=is_cancel_requested,
                on_cancel_pending=on_cancel_pending,
                message_overlay=message_overlay,
                on_model_response=on_model_response,
                thinking_channel=channel,
                thinking_sender=sender,
                tools_config=tools_cfg,
                skill_registry=skill_registry,
                turn_context=turn_context,
                check_preempt=check_preempt,
                max_preempts=max_preempts,
            )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        logger.warning("Staged planning failed; falling back to legacy responder", exc_info=True)
        console.print_warning(
            "Staged planning failed; falling back to legacy responder loop: "
            f"{_surface_error_message(error)}",
            indent=2,
        )
        return run_responder_fn(
            client,
            messages,
            tools,
            conversation,
            builder,
            registry,
            console,
            on_before_tool_call=on_before_tool_call,
            memory_edit_allow_failure=memory_edit_allow_failure,
            max_iterations=max_iterations,
            memory_edit_turn_retry_limit=memory_edit_turn_retry_limit,
            is_cancel_requested=is_cancel_requested,
            on_cancel_pending=on_cancel_pending,
            message_overlay=message_overlay,
            on_model_response=on_model_response,
            thinking_channel=channel,
            thinking_sender=sender,
            tools_config=tools_cfg,
            skill_registry=skill_registry,
            turn_context=turn_context,
            check_preempt=check_preempt,
            max_preempts=max_preempts,
        )

    plan_text = format_stage2_plan_for_tui(stage2.plan_text)
    console.print_inner_thoughts(channel, sender, f"[PLAN][Stage2]\n{plan_text}")

    stage3_overlay_messages: list[Message] = [
        build_stage1_findings_overlay_message(stage1.findings_text),
        build_stage3_plan_overlay_message(stage2.plan_text),
    ]
    if plan_context_msg is not None:
        stage3_overlay_messages.append(plan_context_msg)
    stage3_extra = _make_synthetic_message_overlay(stage3_overlay_messages)
    stage3_overlay = _compose_message_overlays(message_overlay, stage3_extra)

    console.print_info("Stage 3/3: execute")
    return run_responder_fn(
        client,
        messages,
        tools,
        conversation,
        builder,
        registry,
        console,
        on_before_tool_call=on_before_tool_call,
        memory_edit_allow_failure=memory_edit_allow_failure,
        max_iterations=max_iterations,
        memory_edit_turn_retry_limit=memory_edit_turn_retry_limit,
        is_cancel_requested=is_cancel_requested,
        on_cancel_pending=on_cancel_pending,
        message_overlay=stage3_overlay,
        on_model_response=on_model_response,
        thinking_channel=channel,
        thinking_sender=sender,
        tools_config=tools_cfg,
        skill_registry=skill_registry,
        turn_context=turn_context,
        check_preempt=check_preempt,
        max_preempts=max_preempts,
    )


def _build_common_ground_overlay(
    *,
    shared_state_store: SharedStateStore | None,
    config: AppConfig,
    turn_metadata: dict[str, object] | None,
    console: AgentUiPort,
    debug: bool,
) -> tuple[Callable[[list[Message]], list[Message]] | None, _CommonGroundTurnDebug]:
    """Build per-turn common-ground synthetic tool overlay when revisions diverge."""
    metadata = turn_metadata or {}
    scope_id = metadata.get("scope_id")
    anchor_shared_rev = metadata.get("anchor_shared_rev")
    debug_scope_id = scope_id if isinstance(scope_id, str) and scope_id else None
    debug_anchor_rev = anchor_shared_rev if isinstance(anchor_shared_rev, int) else None
    base_debug = _CommonGroundTurnDebug(
        scope_id=debug_scope_id,
        anchor_shared_rev=debug_anchor_rev,
        store_available=shared_state_store is not None,
    )
    if shared_state_store is None:
        return None, base_debug

    cg_cfg = config.context.common_ground
    if not cg_cfg.enabled:
        return None, base_debug
    if debug_scope_id is None:
        return None, base_debug
    if debug_anchor_rev is None:
        return None, base_debug

    current_shared_rev = shared_state_store.get_current_rev(debug_scope_id)
    current_debug = _CommonGroundTurnDebug(
        scope_id=debug_scope_id,
        anchor_shared_rev=debug_anchor_rev,
        current_shared_rev=current_shared_rev,
        store_available=True,
    )
    if debug_anchor_rev > current_shared_rev:
        console.print_warning(
            "common-ground skipped: cache underflow "
            f"(anchor={debug_anchor_rev} > current={current_shared_rev})",
            indent=2,
        )
        if debug:
            console.print_debug(
                "common-ground",
                "skip underflow "
                f"scope={debug_scope_id} anchor={debug_anchor_rev} "
                f"current={current_shared_rev}",
            )
        return None, current_debug

    if debug_anchor_rev == current_shared_rev:
        if debug:
            console.print_debug(
                "common-ground",
                f"no inject scope={debug_scope_id} anchor=current={debug_anchor_rev}",
            )
        return None, current_debug

    text = shared_state_store.build_common_ground_text(
        scope_id=debug_scope_id,
        upto_rev=debug_anchor_rev,
        current_rev=current_shared_rev,
        max_entries=cg_cfg.max_entries,
        max_chars=cg_cfg.max_chars,
        max_entry_chars=cg_cfg.max_entry_chars,
    )
    if not text:
        return None, current_debug

    if debug:
        console.print_debug(
            "common-ground",
            "injected "
            f"scope={debug_scope_id} anchor={debug_anchor_rev} "
            f"current={current_shared_rev} chars={len(text)}",
        )
    return _make_latest_user_text_overlay(text), current_debug
