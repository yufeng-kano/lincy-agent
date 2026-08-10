"""Turn-scoped runtime helpers for memory sync and recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from pathlib import Path

from ..context import ContextBuilder, Conversation
from ..core.schema import MemoryArchiveConfig
from ..llm import LLMResponse
from ..llm.base import LLMClient
from ..llm.schema import Message, ToolCall, ToolDefinition, make_tool_result_message
from ..memory import extract_memory_edit_paths
from ..memory.hooks import check_and_archive_buffers
from ..tools import ToolRegistry
from .side_channel import run_side_channel_tool_loop
from .tool_setup import resolve_memory_path
from .run_helpers import (
    _debug_print_responder_output,
    _raise_if_cancel_requested,
    _surface_error_message,
)
from .ui_event_console import AgentUiPort

logger = logging.getLogger(__name__)


@dataclass
class _MemoryFileSnapshot:
    """Original state of one memory file before the current turn writes it."""

    existed: bool
    was_file: bool
    content: bytes | None = None
    size: int | None = None


class _TurnMemorySnapshot:
    """Capture and rollback memory file changes made during one user turn."""

    def __init__(self, *, agent_os_dir: Path):
        self._agent_os_dir = agent_os_dir
        self._temp_memory_file = (
            agent_os_dir / "memory" / "agent" / "temp-memory.md"
        ).resolve()
        self._snapshots: dict[Path, _MemoryFileSnapshot] = {}

    def capture_from_tool_call(self, tool_call: ToolCall) -> None:
        """Snapshot all memory paths referenced by a memory_edit call."""
        if tool_call.name != "memory_edit":
            return

        for path in extract_memory_edit_paths(tool_call):
            resolved = self._resolve_memory_file(path)
            if resolved is None or resolved in self._snapshots:
                continue

            if resolved.exists():
                if resolved.is_file():
                    if resolved == self._temp_memory_file:
                        self._snapshots[resolved] = _MemoryFileSnapshot(
                            existed=True,
                            was_file=True,
                            size=resolved.stat().st_size,
                        )
                    else:
                        self._snapshots[resolved] = _MemoryFileSnapshot(
                            existed=True,
                            was_file=True,
                            content=resolved.read_bytes(),
                        )
                else:
                    self._snapshots[resolved] = _MemoryFileSnapshot(
                        existed=True,
                        was_file=False,
                    )
            else:
                self._snapshots[resolved] = _MemoryFileSnapshot(
                    existed=False,
                    was_file=False,
                )

    def rollback(self) -> int:
        """Restore all captured files to their pre-turn state."""
        restored = 0
        for path in sorted(
            self._snapshots.keys(),
            key=lambda current: len(current.parts),
            reverse=True,
        ):
            snapshot = self._snapshots[path]
            if snapshot.existed:
                if not snapshot.was_file:
                    continue
                if snapshot.size is not None:
                    if path.exists() and path.is_file():
                        with path.open("r+b") as f:
                            f.truncate(snapshot.size)
                        restored += 1
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(snapshot.content or b"")
                restored += 1
                continue

            if path.exists() and path.is_file():
                path.unlink()
                restored += 1

        return restored

    def _resolve_memory_file(self, raw_path: str) -> Path | None:
        return resolve_memory_path(raw_path, self._agent_os_dir)


@dataclass
class TurnTokenUsage:
    """Per-turn usage aggregation for brain responses."""

    usage_available: bool = False
    max_prompt_tokens: int | None = None
    completion_tokens_for_max_prompt: int | None = None
    total_tokens_for_max_prompt: int | None = None
    cache_prompt_tokens_for_display: int | None = None
    cache_read_tokens_for_display: int = 0
    cache_write_tokens_for_display: int = 0
    saw_missing_usage: bool = False

    def record(self, response: LLMResponse) -> None:
        """Track max prompt usage and best cache-read sample separately."""
        if not response.usage_available:
            self.saw_missing_usage = True
            return
        self.usage_available = True
        if response.prompt_tokens is None:
            return
        if (
            self.max_prompt_tokens is None
            or response.prompt_tokens >= self.max_prompt_tokens
        ):
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
    """Latest token usage shown in the status bar."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_prompt_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usage_available: bool = False
    missing_usage: bool = False


def _build_memory_sync_reminder(
    missing_targets: list[str],
    turns_accumulated: int = 1,
) -> str:
    """Build directive for the memory-sync side-channel LLM call."""
    targets = "\n".join(f"- {target}" for target in missing_targets)
    turn_scope = "1 turn" if turns_accumulated == 1 else f"{turns_accumulated} turns"
    return (
        "[MEMORY SYNC - ROLLUP]\n"
        f"The following files have not been updated for {turn_scope}:\n{targets}\n\n"
        "You must persist the missing interactions now.\n"
        "For each listed target, write EXACTLY ONE rollup entry covering the\n"
        "missing window in chronological order.\n"
        "Do not skip any NEW fact from that window.\n\n"
        "Format for temp-memory.md rollup:\n"
        "- Prefix: `[YYYY-MM-DD HH:MM] ` (use the latest turn timestamp in scope)\n"
        f"- Start entry body with `[rollup {turn_scope}]`\n"
        "- Include real names for people when applicable\n"
        "- DELTA ONLY: record facts new since the last successful temp-memory write\n"
        "  (or the start of this missing window). Do NOT re-narrate the whole day\n"
        "  or restate closed loops already written earlier in temp-memory.\n"
        "- POINTER for durable homes: if a fact already lives in expenses DB,\n"
        "  health.md, long-term.md, tasks, schedule_action, or people/* — write one\n"
        "  short pointer line (ids/times only), not the full process narrative.\n"
        "- End with an Open loops only section: unresolved items only; drop items\n"
        "  that closed in this window. Do not paste a full day todo list.\n"
        "- Optional brief reaction/emotion if it affects next actions; skip filler.\n"
        "- FORBIDDEN boilerplate: fixed channel tags like '回覆走 DC', full daily\n"
        "  ban lists restated every entry, re-copying long-term rules, or replaying\n"
        "  already-logged expense/nutrition/med narratives.\n\n"
        "Call memory_edit now."
    )


def _build_artifact_registry_sync_reminder(
    artifact_paths: list[str],
    *,
    registry_target: str,
) -> str:
    """Build directive for same-turn artifact registry sync."""
    artifact_list = "\n".join(f"- {path}" for path in artifact_paths)
    return (
        "[ARTIFACT REGISTRY SYNC]\n"
        "The following artifact files were written in this turn without updating\n"
        f"{registry_target}:\n{artifact_list}\n\n"
        f"You must update {registry_target} now via memory_edit.\n"
        "Append EXACTLY ONE entry per artifact path, unless the same path already\n"
        "has an entry and only needs a precise update.\n\n"
        "Required entry format:\n"
        "- [YYYY-MM-DD] [file|creation] title | path: artifacts/... | note: why it matters\n\n"
        "Use [file] for durable documents or attachments.\n"
        "Use [creation] for stories, drafts, or other generated works.\n"
        "Do not treat artifact storage itself as a reminder mechanism.\n"
        "If the artifact changes future behavior, update the relevant live memory\n"
        "file separately. If follow-up is needed later, use schedule_action.\n\n"
        "Call memory_edit now."
    )


def _rollback_turn_memory_changes(
    snapshot: _TurnMemorySnapshot,
    *,
    console: AgentUiPort,
    debug: bool,
) -> None:
    """Best-effort rollback for partial turn memory writes."""
    try:
        restored = snapshot.rollback()
    except Exception:
        logger.exception("Failed to rollback memory writes for failed turn")
        console.print_warning("Failed to rollback partial memory writes for failed turn.")
        return

    if debug and restored > 0:
        console.print_debug("turn rollback", f"restored {restored} memory file(s)")


def _patch_interrupted_tool_calls(conversation: Conversation, since: int) -> int:
    """Fill missing tool results for interrupted tool calls."""
    messages = conversation.get_messages()
    last_assistant_idx = None
    for index in range(len(messages) - 1, since - 1, -1):
        if messages[index].role == "assistant" and messages[index].tool_calls:
            last_assistant_idx = index
            break
    if last_assistant_idx is None:
        return 0

    existing = {
        messages[index].tool_call_id
        for index in range(last_assistant_idx + 1, len(messages))
        if messages[index].role == "tool" and messages[index].tool_call_id
    }
    added = 0
    for tool_call in messages[last_assistant_idx].tool_calls:
        if tool_call.id not in existing:
            conversation.add_tool_result(tool_call.id, tool_call.name, "[Interrupted by user]")
            added += 1
    return added


def _inject_brain_failure_record(
    conversation: Conversation,
    turn_anchor: int,
    error: Exception,
    *,
    memory_rolled_back: bool,
) -> None:
    """Replace partial turn output with a failure notice."""
    executed: list[str] = []
    for entry in conversation.get_messages()[turn_anchor:]:
        if entry.role == "tool" and entry.name:
            executed.append(entry.name)

    conversation.truncate_to(turn_anchor)

    error_name = type(error).__name__
    parts = [
        f"[BRAIN ERROR] LLM call failed ({error_name}). "
        "This turn was NOT completed.",
    ]
    detail = _surface_error_message(error)
    if detail and detail != error_name:
        parts.append(f"Detail: {detail}")
    if executed:
        parts.append(
            "Tools executed before failure: "
            + ", ".join(executed)
            + ". Their side effects (sent messages, API calls) "
            "were NOT rolled back."
        )
    if memory_rolled_back:
        parts.append("Memory file changes were rolled back to pre-turn state.")
    parts.append(
        "IMPORTANT: On your next response, carefully verify "
        "(1) whether messages were actually delivered, "
        "(2) whether memory is consistent with reality."
    )
    conversation.add("assistant", "\n".join(parts))


def _run_memory_sync_side_channel(
    client: LLMClient,
    conversation: Conversation,
    builder: ContextBuilder,
    tools: list[ToolDefinition],
    registry: ToolRegistry,
    console: AgentUiPort,
    missing_targets: list[str],
    turns_accumulated: int = 1,
    max_retries: int = 1,
    reminder_text: str | None = None,
    on_before_tool_call: Callable[[ToolCall], None] | None = None,
    is_cancel_requested: Callable[[], bool] | None = None,
    on_cancel_pending: Callable[[], None] | None = None,
) -> None:
    """Side-channel LLM call to sync missing memory targets."""
    if not any(definition.name == "memory_edit" for definition in tools):
        return

    local_messages = builder.build(conversation)
    local_messages.append(
        Message(
            role="user",
            content=reminder_text
            or _build_memory_sync_reminder(
                missing_targets,
                turns_accumulated,
            ),
        ),
    )

    def execute_memory_edit(tool_call: ToolCall):
        if tool_call.name != "memory_edit" or not registry.has_tool(tool_call.name):
            return None
        if on_before_tool_call is not None:
            on_before_tool_call(tool_call)
        _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
        with console.spinner("Executing..."):
            result = registry.execute(tool_call)
        _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
        return result

    for attempt in range(1 + max_retries):
        had_error = False

        def observe(response: LLMResponse) -> bool:
            _debug_print_responder_output(console, response, label="memory-sync")
            return False

        def execute_memory_edit_with_status(tool_call: ToolCall):
            nonlocal had_error
            result = execute_memory_edit(tool_call)
            if result is not None and result.is_error:
                had_error = True
            return result

        run_side_channel_tool_loop(
            client=client,
            messages=local_messages,
            tools=tools,
            execute_fn=execute_memory_edit_with_status,
            console=console,
            max_iterations=1,
            raise_if_cancel_requested=lambda: _raise_if_cancel_requested(
                is_cancel_requested, on_pending=on_cancel_pending,
            ),
            on_response=observe,
        )
        if not had_error:
            break
        if attempt < max_retries:
            logger.info("memory-sync retry %d/%d after error", attempt + 1, max_retries)


_EMPTY_RESPONSE_NUDGE = (
    "[SYSTEM] Your previous response was empty. "
    "As a companion, you must always reply to the user. "
    "Respond naturally to their message now. "
    "Do not call any tools. Just talk."
)


def _run_empty_response_fallback(
    client: LLMClient,
    conversation: Conversation,
    builder: ContextBuilder,
    console: AgentUiPort,
    is_cancel_requested: Callable[[], bool] | None = None,
    on_cancel_pending: Callable[[], None] | None = None,
) -> str:
    """Side-channel LLM call to get a text response when responder returned empty."""
    local_messages = builder.build(conversation)
    local_messages.append(
        Message(role="user", content=_EMPTY_RESPONSE_NUDGE),
    )
    _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
    with console.spinner():
        response = client.chat(local_messages)
    _raise_if_cancel_requested(is_cancel_requested, on_pending=on_cancel_pending)
    if response and response.strip():
        return response
    return ""


def _run_memory_archive(
    agent_os_dir: Path,
    archive_config: MemoryArchiveConfig,
    console: AgentUiPort,
) -> None:
    """Run memory archive and swallow non-fatal errors."""
    try:
        result = check_and_archive_buffers(agent_os_dir, archive_config)
        if result.archived:
            console.print_info(f"Memory archived: {result.summary}")
    except Exception as error:
        logger.warning("Memory archive failed: %s", error)
