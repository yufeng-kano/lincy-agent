"""Runtime UI event emitter used by AgentCore and CLI wiring."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime

from ..timezone_utils import now as tz_now
from typing import Iterator, Protocol

from ..cli.claude_code_stream_json import parse_claude_code_stream_json_line
from ..cli.formatter import (
    format_gui_tool_call,
    format_gui_tool_result,
    format_tool_call,
    format_tool_result,
)
from ..context.conversation import split_turns
from ..llm.content import content_to_text
from ..llm.schema import ContentPart, ToolCall
from ..session.schema import SessionEntry
from ..tui.events import (
    AssistantTextEvent,
    DebugEvent,
    ErrorEvent,
    InboundMessageEvent,
    OutboundMessageEvent,
    ProcessingStartedEvent,
    ProcessingFinishedEvent,
    ResumeHistoryEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
    WarningEvent,
)
from ..tui.sink import UiSink


class AgentUiPort(Protocol):
    """UI methods required by AgentCore and runtime helpers."""

    debug: bool
    show_tool_use: bool
    def print_tool_call(self, tool_call: ToolCall) -> None: ...
    def print_tool_result(self, tool_call: ToolCall, result: str | list[ContentPart]) -> None: ...
    def print_assistant(self, content: str | None) -> None: ...
    def print_inbound(
        self,
        channel: str,
        sender: str | None,
        content: str,
        *,
        ts: datetime | None = None,
    ) -> None: ...
    def print_processing(self, channel: str, sender: str | None) -> None: ...
    def print_outbound(
        self,
        channel: str,
        sender: str | None,
        content: str | None,
        *,
        ts: datetime | None = None,
        attachments: list[str] | None = None,
    ) -> None: ...
    def print_inner_thoughts(self, channel: str, sender: str | None, content: str | None) -> None: ...
    def print_error(self, message: str) -> None: ...
    def print_warning(self, message: str, *, indent: int = 0) -> None: ...
    def print_info(self, message: str) -> None: ...
    def print_debug(self, label: str, message: str) -> None: ...
    def print_debug_block(self, label: str, content: str) -> None: ...
    def print_goodbye(self) -> None: ...
    def set_timezone(self, timezone: str) -> None: ...
    def spinner(self, text: str = "Thinking...") -> Iterator[None]: ...


CtxStatusProvider = Callable[[], str | None]


class UiEventConsole:
    """Runtime UI adapter backed by a typed ``UiSink``."""

    def __init__(self, ui_sink: UiSink, *, debug: bool = False, show_tool_use: bool = False) -> None:
        self._ui = ui_sink
        self.debug = debug
        self.show_tool_use = show_tool_use
        self._current_user: str | None = None
        self._timezone: str | None = None
        self._ctx_status_provider = None

    def set_current_user(self, user_id: str) -> None:
        self._current_user = user_id

    def set_timezone(self, timezone: str) -> None:
        self._timezone = timezone

    def set_debug(self, enabled: bool) -> None:
        self.debug = enabled

    def set_show_tool_use(self, enabled: bool) -> None:
        self.show_tool_use = enabled

    def set_ctx_status_provider(self, provider: CtxStatusProvider | None) -> None:
        self._ctx_status_provider = provider

    @staticmethod
    def _is_failed_tool_result(result: str) -> bool:
        if result.startswith("Error"):
            return True
        if not result.startswith("{"):
            return False
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("status") == "failed"

    @staticmethod
    def _has_tool_warnings(result: str) -> bool:
        if not result.startswith("{"):
            return False
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return False
        warnings = payload.get("warnings")
        return isinstance(warnings, list) and bool(warnings)

    def print_tool_call(self, tool_call: ToolCall) -> None:
        if not self.show_tool_use:
            return
        text = format_tool_call(tool_call)
        self._ui.emit(ToolCallEvent(name=tool_call.name, summary=text))

    def print_tool_result(self, tool_call: ToolCall, result: str | list[ContentPart]) -> None:
        if isinstance(result, list):
            display_result = content_to_text(result)
        else:
            display_result = result
        failed = self._is_failed_tool_result(display_result)
        warn = self._has_tool_warnings(display_result)
        text = format_tool_result(tool_call, display_result)
        if not self.show_tool_use and not (failed or warn):
            return
        self._ui.emit(
            ToolResultEvent(
                name=tool_call.name,
                summary=text,
                failed=failed,
                warning=warn,
            )
        )

    def print_subagent_tool_call(self, label: str, tool_call: ToolCall) -> None:
        """Show a subagent's tool call under its own label (e.g. worker-3)."""
        if not self.show_tool_use:
            return
        text = format_tool_call(tool_call)
        self._ui.emit(
            ToolCallEvent(name=f"{label} {tool_call.name}", summary=text)
        )

    def print_subagent_tool_result(
        self, label: str, tool_call: ToolCall, result: str | list[ContentPart],
    ) -> None:
        """Show a subagent's tool result under its own label."""
        if isinstance(result, list):
            display_result = content_to_text(result)
        else:
            display_result = result
        failed = self._is_failed_tool_result(display_result)
        warn = self._has_tool_warnings(display_result)
        if not self.show_tool_use and not (failed or warn):
            return
        text = format_tool_result(tool_call, display_result)
        self._ui.emit(
            ToolResultEvent(
                name=f"{label} {tool_call.name}",
                summary=text,
                failed=failed,
                warning=warn,
            )
        )

    def print_shell_stream_line(self, line: str) -> None:
        if not self.show_tool_use:
            return
        event = parse_claude_code_stream_json_line(line)
        if event.kind == "tool_use":
            self._ui.emit(ToolStreamEvent(line=f"[stream] {event.tool_name}"))

    def print_gui_step(
        self,
        tool_call: ToolCall,
        result: str,
        step: int,
        max_steps: int,
        elapsed_sec: float = 0.0,
        total_elapsed_sec: float = 0.0,
        *,
        worker_timing: dict[str, float] | None = None,
    ) -> None:
        if not self.show_tool_use:
            return
        call_text = format_gui_tool_call(tool_call)
        timing = ""
        if elapsed_sec > 0:
            timing += f" {elapsed_sec:.1f}s"
        if total_elapsed_sec > 0:
            timing += f" total={total_elapsed_sec:.1f}s"
        if worker_timing:
            timing += (
                f" ss={worker_timing.get('screenshot', 0.0):.1f}s"
                f" inf={worker_timing.get('inference', 0.0):.1f}s"
            )
        self._ui.emit(
            ToolCallEvent(
                name="gui_task",
                summary=f"[{step}/{max_steps}{timing}] {call_text}",
            )
        )
        result_text = format_gui_tool_result(tool_call, result)
        if result_text:
            self._ui.emit(
                ToolResultEvent(
                    name="gui_task",
                    summary=result_text,
                    failed=self._is_failed_tool_result(result),
                    warning=False,
                )
            )

    def print_inbound(
        self,
        channel: str,
        sender: str | None,
        content: str,
        *,
        ts: datetime | None = None,
    ) -> None:
        event_ts = ts or tz_now()
        self._ui.emit(
            InboundMessageEvent(
                timestamp=event_ts,
                channel=channel,
                sender=sender,
                content=content,
            )
        )

    def print_processing(self, channel: str, sender: str | None) -> None:
        self._ui.emit(ProcessingStartedEvent(channel=channel, sender=sender))

    def print_outbound(
        self,
        channel: str,
        sender: str | None,
        content: str | None,
        *,
        ts: datetime | None = None,
        attachments: list[str] | None = None,
    ) -> None:
        if not content:
            return
        suffix = ""
        if attachments:
            suffix = f" [attachments: {len(attachments)}]"
        event_ts = ts or tz_now()
        self._ui.emit(
            OutboundMessageEvent(
                timestamp=event_ts,
                channel=channel,
                recipient=sender,
                content=f"{content}{suffix}",
            )
        )

    def print_inner_thoughts(self, channel: str, sender: str | None, content: str | None) -> None:
        if not content or not content.strip():
            return
        self._ui.emit(AssistantTextEvent(content=content))

    def print_assistant(self, content: str | None) -> None:
        if not content:
            return
        self._ui.emit(AssistantTextEvent(content=content))

    def print_error(self, message: str) -> None:
        self._ui.emit(ErrorEvent(message=message))

    def print_warning(self, message: str, *, indent: int = 0) -> None:
        if indent > 0:
            message = (" " * indent) + message
        self._ui.emit(WarningEvent(message=message))

    def print_info(self, message: str) -> None:
        self._ui.emit(ResumeHistoryEvent(summary=message))

    def print_debug(self, label: str, message: str) -> None:
        if self.debug:
            self._ui.emit(DebugEvent(label=label, message=message))

    def print_debug_block(self, label: str, content: str) -> None:
        if not self.debug:
            return
        self._ui.emit(DebugEvent(label=label, message=content))

    def print_welcome(self) -> None:
        self._ui.emit(ResumeHistoryEvent(summary="Chat started. Type /help for commands."))

    def print_goodbye(self) -> None:
        self._ui.emit(ResumeHistoryEvent(summary="Bye!"))

    def print_resume_history(
        self,
        entries: list[SessionEntry],
        replay_turns: int | None,
        show_tool_calls: bool,
    ) -> None:
        if not entries:
            return
        self._ui.emit(
            ResumeHistoryEvent(
                summary=f"Resuming session: {len(entries)} messages"
            )
        )
        turns = split_turns(entries)
        if replay_turns is not None:
            turns = turns[-replay_turns:]
        for turn in turns:
            if turn[0].role != "user":
                continue
            user_entry = turn[0]
            user_content = (
                content_to_text(user_entry.content)
                if isinstance(user_entry.content, list)
                else (user_entry.content or "")
            )
            if user_content.strip():
                self.print_inbound(user_entry.channel or "cli", user_entry.sender, user_content, ts=user_entry.timestamp)
            self._ui.emit(
                ResumeHistoryEvent(
                    summary=f"processing [{user_entry.channel or 'cli'}]"
                )
            )
            tool_call_map: dict[str, ToolCall] = {}
            last_response_text: str | None = None
            for entry in turn[1:]:
                if entry.role == "assistant" and entry.tool_calls:
                    text_content = (
                        content_to_text(entry.content)
                        if isinstance(entry.content, list)
                        else (entry.content or "")
                    )
                    if text_content.strip():
                        self.print_assistant(text_content)
                    for tc in entry.tool_calls:
                        tool_call_map[tc.id] = tc
                    if show_tool_calls:
                        for tc in entry.tool_calls:
                            if not tc.name.startswith("_"):
                                self.print_tool_call(tc)
                elif entry.role == "assistant" and not entry.tool_calls:
                    last_response_text = (
                        content_to_text(entry.content)
                        if isinstance(entry.content, list)
                        else (entry.content or "")
                    )
                elif entry.role == "tool" and show_tool_calls and not (entry.name or "").startswith("_"):
                    result_text = (
                        content_to_text(entry.content)
                        if isinstance(entry.content, list)
                        else (entry.content or "")
                    )
                    matched = tool_call_map.get(entry.tool_call_id or "")
                    if matched is not None:
                        self.print_tool_result(matched, result_text)
                    else:
                        self._ui.emit(ToolResultEvent(name=entry.name or "tool", summary=result_text))
            self.print_outbound(user_entry.channel or "cli", user_entry.sender, last_response_text)
            self._ui.emit(ProcessingFinishedEvent(channel=user_entry.channel or "cli", interrupted=False))

    @contextmanager
    def spinner(self, text: str = "Thinking...") -> Iterator[None]:
        """No-op context manager for compatibility with AgentCore."""
        yield
