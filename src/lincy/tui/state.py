"""UI state model for the Textual chat application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .events import (
    AssistantTextEvent,
    CtxStatusEvent,
    DebugEvent,
    ErrorEvent,
    InboundMessageEvent,
    InterruptStateEvent,
    OutboundMessageEvent,
    ProcessingFinishedEvent,
    ProcessingStartedEvent,
    ResumeHistoryEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
    UiEvent,
    WarningEvent,
)
from ..timezone_utils import localise as tz_localise
from .formatting import indent_lines


@dataclass(slots=True)
class UiLogEntry:
    """One logical row in the TUI log pane."""

    kind: str
    text: str
    timestamp: datetime | None = None


@dataclass(slots=True)
class UiState:
    """Serializable state backing the Textual UI widgets."""

    ctx_status: str = ""
    busy: bool = False
    interrupt_state: str = "idle"
    interrupt_message: str = ""
    log: list[UiLogEntry] = field(default_factory=list)
    pending_count: int = 0

    def _append_log(self, kind: str, text: str, *, timestamp: datetime | None = None) -> bool:
        """Append a log row and suppress immediate duplicates."""
        candidate = UiLogEntry(kind, text, timestamp)
        if self.log:
            last = self.log[-1]
            if last.kind == candidate.kind and last.text == candidate.text:
                return False
        self.log.append(candidate)
        return True

    def _ts(self, ts: datetime) -> str:
        """Format event timestamp using local time for display."""
        return tz_localise(ts).strftime("%m/%d %H:%M:%S")

    def append_event(self, event: UiEvent) -> bool:
        """Apply one UI event to local state.

        Returns True when a new log row was appended.
        """
        match event:
            case CtxStatusEvent(text=text):
                self.ctx_status = text
                return False
            case InterruptStateEvent(phase=phase, message=message):
                self.interrupt_state = phase
                self.interrupt_message = message
                return False
            case ProcessingStartedEvent(timestamp=ts, channel=channel, sender=sender):
                self.busy = True
                source = f"{channel}/{sender}" if sender else channel
                return self._append_log("processing", f"source={source}", timestamp=ts)
            case ProcessingFinishedEvent(timestamp=ts, interrupted=interrupted):
                self.busy = False
                if interrupted:
                    return self._append_log("info", "Turn interrupted", timestamp=ts)
                return self._append_log("info", "Turn complete", timestamp=ts)
            case InboundMessageEvent(timestamp=ts, channel=channel, sender=sender, content=content):
                source = f"{channel}/{sender}" if sender else channel
                return self._append_log(
                    "inbound",
                    f"{self._ts(ts)} source={source}\n{content}",
                    timestamp=ts,
                )
            case AssistantTextEvent(timestamp=ts, content=content):
                return self._append_log("assistant", content, timestamp=ts)
            case OutboundMessageEvent():
                # Outbound display is redundant with send_message tool logs in the TUI.
                return False
            case ToolCallEvent(timestamp=ts, name=name, summary=summary):
                text = name if not summary.strip() else f"{name}\n{indent_lines(summary)}"
                return self._append_log("tool_call", text, timestamp=ts)
            case ToolResultEvent(timestamp=ts, name=name, summary=summary, failed=failed, warning=warning):
                level = "tool_error" if failed else ("tool_warn" if warning else "tool_result")
                text = name if not summary.strip() else f"{name}\n{indent_lines(summary)}"
                return self._append_log(level, text, timestamp=ts)
            case ToolStreamEvent(timestamp=ts, line=line):
                return self._append_log("tool_stream", line, timestamp=ts)
            case WarningEvent(timestamp=ts, message=message):
                return self._append_log("warning", message, timestamp=ts)
            case ErrorEvent(timestamp=ts, message=message):
                return self._append_log("error", message, timestamp=ts)
            case DebugEvent(timestamp=ts, label=label, message=message):
                return self._append_log("debug", f"{label}\n{message}", timestamp=ts)
            case ResumeHistoryEvent(timestamp=ts, summary=summary):
                return self._append_log("resume", summary, timestamp=ts)
            case _:
                return self._append_log("info", str(event))
