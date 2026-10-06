"""Worker subagent execution engine."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..context.cache_breakpoints import advance_cache_breakpoint
from ..llm.session import llm_session
from ..llm.base import LLMClient
from ..llm.schema import Message, make_tool_result_message
from ..session.debug_client import DebugLoggingLLMClient
from ..tools.registry import ToolRegistry
from .notes import WorkerNotes

logger = logging.getLogger(__name__)
_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 8
_ACTION_LOG_MAX_CHARS = 4000
_ACTION_ARGS_MAX_CHARS = 200
_ACTION_RESULT_MAX_CHARS = 300
_TURN_LIMIT_TOOL_RESULT = "Not executed: worker turn limit reached."
_FORCED_REPORT_PROMPT = (
    "Turn limit reached. Do not call tools. Report now: what was done, "
    "exact values produced or submitted, what remains incomplete and why, "
    "and the paths of any files or logs you left behind. "
    "If a gui_task session is unfinished (BLOCKED or PAUSED), include its "
    "session_id and status so the task can be resumed."
)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Outcome of a single worker invocation."""

    success: bool
    text: str
    turns_used: int
    tokens_used: int
    duration_ms: int
    truncated: bool
    error: str | None = None
    action_log: str = ""


def run_simple_tool_loop(
    client: LLMClient,
    *,
    build_messages: Any,
    tool_definitions: list[Any],
    handle_tool_calls: Any,
    finalization_messages: Any,
) -> str:
    """Run a basic synchronous tool loop and require a final text response."""
    response = client.chat_with_tools(build_messages(), tool_definitions)
    while response.has_tool_calls():
        handle_tool_calls(response)
        response = client.chat_with_tools(build_messages(), tool_definitions)

    content = response.content or ""
    if not content.strip():
        content = client.chat_with_tools(build_messages(), []).content or ""
    if not content.strip():
        content = client.chat_with_tools(finalization_messages(), []).content or ""
    if not content.strip():
        raise RuntimeError("Model returned empty final response during tool loop.")
    return content


class WorkerRunner:
    """Run autonomous tool loops with an independent context window."""

    def __init__(
        self,
        client: LLMClient,
        source_registry: ToolRegistry,
        excluded_tools: frozenset[str],
        system_prompt: str,
        *,
        max_turns: int = 30,
        max_context_tokens: int = 96000,
        cache_control: dict[str, str] | None = None,
        sink: Any = None,
        provider: str | None = None,
        model: str | None = None,
        ui_console: Any = None,
        tool_overrides: dict[str, tuple[Any, Any]] | None = None,
        extra_tools: dict[str, tuple[Any, Any]] | None = None,
        notes: WorkerNotes | None = None,
        notes_summarizer: Callable[[str], str] | None = None,
    ) -> None:
        self._client = client
        self._source_registry = source_registry
        self._excluded_tools = excluded_tools
        self._system_prompt = system_prompt
        self._ui_console = ui_console
        self._tool_overrides = tool_overrides or {}
        self._extra_tools = extra_tools or {}
        self._notes = notes
        self._notes_summarizer = notes_summarizer
        self._notes_thread: threading.Thread | None = None
        self._max_turns = max_turns
        self._max_context_tokens = max_context_tokens
        self._cache_control = cache_control
        self._sink = sink
        self._provider = provider
        self._model = model

    def _build_filtered_registry(self) -> ToolRegistry:
        """Clone tools from source registry, excluding blocked names.

        Names present in tool_overrides are registered with the override
        implementation instead (e.g. file tools without the memory guard).
        extra_tools are worker-only tools absent from the source registry.
        """
        filtered = ToolRegistry()
        for name, (func, defn) in self._source_registry._tools.items():
            if name in self._excluded_tools:
                continue
            override = self._tool_overrides.get(name)
            if override is not None:
                filtered.register(name, override[0], override[1])
            else:
                filtered.register(name, func, defn)
        for name, (func, defn) in self._extra_tools.items():
            if name not in self._excluded_tools:
                filtered.register(name, func, defn)
        return filtered

    def _build_user_message(
        self,
        prompt: str,
        context_files: list[str] | None,
        agent_os_dir: Path | None,
    ) -> str:
        """Build user message with optional context file preamble."""
        parts: list[str] = []
        notes_text = self._notes.read().strip() if self._notes is not None else ""
        if notes_text:
            parts.append(f"[Worker notes]\n{notes_text}\n[/Worker notes]")
        for path_str in context_files or []:
            resolved = Path(path_str).expanduser()
            if not resolved.is_absolute() and agent_os_dir:
                resolved = agent_os_dir / resolved
            try:
                content = resolved.read_text(encoding="utf-8")
                parts.append(f"[Context: {path_str}]\n{content}\n[/Context]")
            except OSError:
                parts.append(f"[Context: {path_str}]\n(file not found)\n[/Context]")
        parts.append(prompt)
        return "\n\n".join(parts)

    def _print_tool_call(self, worker_label: str, tool_call: Any) -> None:
        """Surface worker tool activity in the CLI; never break the run."""
        if self._ui_console is None:
            return
        try:
            self._ui_console.print_subagent_tool_call(worker_label, tool_call)
        except Exception:
            logger.debug("Worker %s UI tool-call emit failed", worker_label, exc_info=True)

    def _print_tool_result(
        self, worker_label: str, tool_call: Any, content: Any,
    ) -> None:
        if self._ui_console is None:
            return
        try:
            self._ui_console.print_subagent_tool_result(worker_label, tool_call, content)
        except Exception:
            logger.debug("Worker %s UI tool-result emit failed", worker_label, exc_info=True)

    def _wrap_client(self, worker_label: str) -> LLMClient:
        """Wrap the base client with per-invocation debug logging."""
        if self._sink is None:
            return self._client
        return DebugLoggingLLMClient(
            self._client,
            sink=self._sink,
            client_label=worker_label,
            provider=self._provider,
            model=self._model,
        )

    def _estimate_message_tokens(self, message: Message) -> int:
        total = _MESSAGE_OVERHEAD_TOKENS
        content = message.content
        if isinstance(content, str):
            total += _estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if part.type == "text" and part.text:
                    total += _estimate_text_tokens(part.text)
                elif part.type == "image":
                    # Worker traffic is text-heavy. Use a simple fixed cost so
                    # multimodal messages do not look artificially free.
                    total += 256

        if message.reasoning_content:
            total += _estimate_text_tokens(message.reasoning_content)
        if message.tool_calls:
            for tool_call in message.tool_calls:
                total += _estimate_text_tokens(tool_call.name)
                total += _estimate_text_tokens(json.dumps(tool_call.arguments))
        if message.name:
            total += _estimate_text_tokens(message.name)
        if message.tool_call_id:
            total += _estimate_text_tokens(message.tool_call_id)
        return total

    def _estimate_messages_tokens(self, messages: list[Message]) -> int:
        return sum(self._estimate_message_tokens(message) for message in messages)

    def _oldest_turn_span(self, messages: list[Message]) -> int:
        if not messages:
            return 0
        if messages[0].role != "assistant":
            return 1
        span = 1
        while span < len(messages) and messages[span].role == "tool":
            span += 1
        return span

    def _trim_initial_user_message(
        self,
        message: Message,
        *,
        available_tokens: int,
    ) -> Message:
        if not isinstance(message.content, str):
            return message
        available_chars = max(0, available_tokens * _CHARS_PER_TOKEN)
        if len(message.content) <= available_chars:
            return message
        prefix = "[Earlier context trimmed]\n"
        suffix_budget = max(64, available_chars - len(prefix))
        trimmed = prefix + message.content[-suffix_budget:]
        return message.model_copy(update={"content": trimmed})

    def _compact_messages(self, messages: list[Message], worker_label: str) -> list[Message]:
        budget = self._max_context_tokens
        if budget <= 0:
            return messages

        compacted = [message.model_copy(deep=True) for message in messages]
        if self._estimate_messages_tokens(compacted) <= budget:
            return compacted

        anchor_count = 0
        if compacted and compacted[0].role == "system":
            anchor_count = 1
        if len(compacted) > anchor_count and compacted[anchor_count].role == "user":
            anchor_count += 1

        anchors = compacted[:anchor_count]
        tail = compacted[anchor_count:]

        while tail and self._estimate_messages_tokens(anchors + tail) > budget:
            del tail[: self._oldest_turn_span(tail)]

        compacted = anchors + tail
        if (
            len(compacted) >= 2
            and compacted[0].role == "system"
            and compacted[1].role == "user"
            and self._estimate_messages_tokens(compacted) > budget
        ):
            remaining = max(
                64,
                budget - self._estimate_messages_tokens([compacted[0]]) - _MESSAGE_OVERHEAD_TOKENS,
            )
            compacted[1] = self._trim_initial_user_message(
                compacted[1],
                available_tokens=remaining,
            )

        before = self._estimate_messages_tokens(messages)
        after = self._estimate_messages_tokens(compacted)
        logger.debug(
            "Worker %s compacted prompt tokens approx %s -> %s (budget=%s)",
            worker_label,
            before,
            after,
            budget,
        )
        return compacted

    @llm_session("worker")
    def run(
        self,
        prompt: str,
        *,
        context_files: list[str] | None = None,
        max_turns_override: int | None = None,
        agent_os_dir: Path | None = None,
        worker_label: str = "worker",
    ) -> WorkerResult:
        """Execute the worker agentic loop and return the result."""
        effective_max_turns = max_turns_override or self._max_turns
        client = self._wrap_client(worker_label)
        registry = self._build_filtered_registry()
        tool_defs = registry.get_definitions()

        # Build initial messages
        system_msg = Message(
            role="system",
            content=self._system_prompt,
            cache_control=self._cache_control,
        )
        user_content = self._build_user_message(prompt, context_files, agent_os_dir)
        user_msg = Message(role="user", content=user_content)
        messages: list[Message] = [system_msg, user_msg]

        turns = 0
        tokens_used = 0
        last_text: str | None = None
        started_ms = _now_ms()

        try:
            request_messages = advance_cache_breakpoint(
                self._compact_messages(messages, worker_label)
            )
            response = client.chat_with_tools(request_messages, tool_defs)
            tokens_used += response.total_tokens or 0

            while response.tool_calls and turns < effective_max_turns:
                # Capture assistant text if present
                if response.content:
                    last_text = response.content

                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                for tc in response.tool_calls:
                    self._print_tool_call(worker_label, tc)
                    result = registry.execute(tc)
                    self._print_tool_result(worker_label, tc, result.content)
                    messages.append(make_tool_result_message(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=result.content,
                    ))

                turns += 1
                request_messages = advance_cache_breakpoint(
                    self._compact_messages(messages, worker_label)
                )
                response = client.chat_with_tools(request_messages, tool_defs)
                tokens_used += response.total_tokens or 0

            # Final response (no tool calls)
            if response.content:
                last_text = response.content

            truncated = bool(response.tool_calls) and turns >= effective_max_turns
            if truncated:
                # Without a report the brain re-dispatches from scratch, so
                # close out the pending calls and ask for one tool-free answer.
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))
                for tc in response.tool_calls:
                    messages.append(make_tool_result_message(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=_TURN_LIMIT_TOOL_RESULT,
                    ))
                messages.append(Message(role="user", content=_FORCED_REPORT_PROMPT))
                # Anthropic rejects a history containing tool_use/tool_result
                # blocks when the request defines no tools, so the tools stay
                # attached; the prompt forbids calling them. A tool call that
                # still comes back is never executed, but any text it carries
                # is the report.
                try:
                    report = client.chat_with_tools(
                        advance_cache_breakpoint(self._compact_messages(messages, worker_label)),
                        tool_defs,
                    )
                    tokens_used += report.total_tokens or 0
                    if report.content:
                        last_text = report.content
                except Exception as exc:
                    logger.warning(
                        "Worker %s forced final report failed: %s", worker_label, exc,
                    )

            worker_result = WorkerResult(
                success=not truncated,
                text=last_text or "",
                turns_used=turns,
                tokens_used=tokens_used,
                duration_ms=_now_ms() - started_ms,
                truncated=truncated,
                # The log only helps the brain resume unfinished work.
                action_log=_build_action_log(messages) if truncated else "",
            )

        except Exception as exc:
            logger.warning("Worker %s failed: %s", worker_label, exc)
            worker_result = WorkerResult(
                success=False,
                text=last_text or "",
                turns_used=turns,
                tokens_used=tokens_used,
                duration_ms=_now_ms() - started_ms,
                truncated=False,
                error=str(exc),
                action_log=_build_action_log(messages),
            )

        if self._notes is not None and self._notes_summarizer is not None:
            notes = self._notes
            summarizer = self._notes_summarizer

            # Compression is an LLM call; keep it off the caller's path so the
            # finished result is returned immediately.
            def compress_notes() -> None:
                try:
                    notes.compress(summarizer)
                except Exception:
                    logger.warning("Worker notes compression failed", exc_info=True)

            self._notes_thread = threading.Thread(
                target=compress_notes, name="worker-notes-compress", daemon=True,
            )
            self._notes_thread.start()
        return worker_result


def _build_action_log(messages: list[Message]) -> str:
    """Render executed tool calls as one deterministic line each.

    Keeps the most recent entries when the log exceeds the char budget,
    since the latest state is what the brain needs to resume.
    """
    results = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    lines: list[str] = []
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for tc in message.tool_calls:
            args = json.dumps(tc.arguments, ensure_ascii=False)
            if len(args) > _ACTION_ARGS_MAX_CHARS:
                args = args[:_ACTION_ARGS_MAX_CHARS] + "..."
            content = results.get(tc.id)
            if content is None:
                result = "(no result)"
            elif isinstance(content, str):
                result = content
            else:
                result = " ".join(
                    (part.text or "") if part.type == "text" else "[image]"
                    for part in content
                )
            result = " ".join(result.split())
            if len(result) > _ACTION_RESULT_MAX_CHARS:
                result = result[:_ACTION_RESULT_MAX_CHARS] + "..."
            lines.append(f"{len(lines) + 1}. {tc.name}({args}) -> {result}")

    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > _ACTION_LOG_MAX_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    omitted = len(lines) - len(kept)
    if omitted:
        kept.insert(0, f"({omitted} earlier entries omitted)")
    return "\n".join(kept)


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + (_CHARS_PER_TOKEN - 1)) // _CHARS_PER_TOKEN)
