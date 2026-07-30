"""Worker subagent execution engine."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..llm.base import LLMClient
from ..llm.schema import Message, make_tool_result_message
from ..session.debug_client import DebugLoggingLLMClient
from ..tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 8


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


class _DebugSinkProtocol:
    """Minimal type hint for the session debug sink."""


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
    ) -> None:
        self._client = client
        self._source_registry = source_registry
        self._excluded_tools = excluded_tools
        self._system_prompt = system_prompt
        self._ui_console = ui_console
        self._tool_overrides = tool_overrides or {}
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
        return filtered

    def _build_user_message(
        self,
        prompt: str,
        context_files: list[str] | None,
        agent_os_dir: Path | None,
    ) -> str:
        """Build user message with optional context file preamble."""
        parts: list[str] = []
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
            request_messages = self._compact_messages(messages, worker_label)
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
                request_messages = self._compact_messages(messages, worker_label)
                response = client.chat_with_tools(request_messages, tool_defs)
                tokens_used += response.total_tokens or 0

            # Final response (no tool calls)
            if response.content:
                last_text = response.content

            truncated = bool(response.tool_calls) and turns >= effective_max_turns
            return WorkerResult(
                success=not truncated,
                text=last_text or "",
                turns_used=turns,
                tokens_used=tokens_used,
                duration_ms=_now_ms() - started_ms,
                truncated=truncated,
            )

        except Exception as exc:
            logger.warning("Worker %s failed: %s", worker_label, exc)
            return WorkerResult(
                success=False,
                text=last_text or "",
                turns_used=turns,
                tokens_used=tokens_used,
                duration_ms=_now_ms() - started_ms,
                truncated=False,
                error=str(exc),
            )


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + (_CHARS_PER_TOKEN - 1)) // _CHARS_PER_TOKEN)
