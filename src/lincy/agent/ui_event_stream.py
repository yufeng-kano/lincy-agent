"""Export typed UI events to a JSONL stream consumed by the web dashboard.

This is a read-only tap on the existing ``UiSink`` pipeline: the TUI keeps its own
sink untouched and a fan-out wrapper mirrors every event into a per-run JSONL file.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..tui.events import (
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
from ..tui.sink import UiSink


logger = logging.getLogger(__name__)

# One runaway tool result must not produce a pathological JSONL line.
MAX_FIELD_CHARS = 16_000
TRUNCATION_MARKER = "... [truncated]"

_DEFAULT_RECENT_LIMIT = 500
_MAX_RECENT_LIMIT = 2000

# Subagent tool events arrive with the label folded into the tool name:
# ``UiEventConsole.print_subagent_tool_call`` / ``print_subagent_tool_result`` emit
# ``f"{worker_label} {tool_name}"`` and ``print_gui_step`` emits the literal
# ``gui_task``. Keep this parser in sync with those call sites.
_SUBAGENT_NAME_RE = re.compile(r"^(worker-\d+)\s+(.+)$")
_GUI_AGENT_LABEL = "gui_task"

_TOOL_EVENT_TYPES = ("tool_call", "tool_result")

# Wire type name + payload fields per event dataclass; ``timestamp`` becomes ``ts``.
_EVENT_SPECS: dict[type, tuple[str, tuple[str, ...]]] = {
    InboundMessageEvent: ("inbound_message", ("channel", "sender", "content")),
    ProcessingStartedEvent: ("processing_started", ("channel", "sender", "label")),
    ProcessingFinishedEvent: ("processing_finished", ("channel", "sender", "interrupted")),
    AssistantTextEvent: ("assistant_text", ("content",)),
    ToolCallEvent: ("tool_call", ("name", "summary")),
    ToolResultEvent: ("tool_result", ("name", "summary", "failed", "warning")),
    ToolStreamEvent: ("tool_stream", ("line",)),
    WarningEvent: ("warning", ("message",)),
    ErrorEvent: ("error", ("message",)),
    DebugEvent: ("debug", ("label", "message")),
    CtxStatusEvent: ("ctx_status", ("text",)),
    ResumeHistoryEvent: ("resume_history", ("summary",)),
    OutboundMessageEvent: ("outbound_message", ("channel", "recipient", "content")),
    InterruptStateEvent: ("interrupt_state", ("phase", "message")),
}


class UiEventRecord(BaseModel):
    """One exported UI event line."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    seq: int
    ts: datetime
    type: str
    agent: str | None = None
    data: dict


def _truncate(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + TRUNCATION_MARKER
    return value


def _attribute_agent(event_type: str, data: dict) -> str | None:
    """Recover the subagent label folded into a tool name, stripping it from ``data``."""
    if event_type not in _TOOL_EVENT_TYPES:
        return None
    name = data.get("name") or ""
    match = _SUBAGENT_NAME_RE.match(name)
    if match is not None:
        data["name"] = match.group(2)
        return match.group(1)
    if name == _GUI_AGENT_LABEL:
        return _GUI_AGENT_LABEL
    return None


def serialize_ui_event(event: UiEvent, *, seq: int) -> UiEventRecord:
    """Map one typed UI event onto its wire record."""
    spec = _EVENT_SPECS.get(type(event))
    if spec is None:
        raise TypeError(f"unsupported UI event type: {type(event).__name__}")
    event_type, fields = spec
    data = {name: _truncate(getattr(event, name)) for name in fields}
    agent = _attribute_agent(event_type, data)
    return UiEventRecord(
        seq=seq,
        ts=event.timestamp,
        type=event_type,
        agent=agent,
        data=data,
    )


class UiEventStore:
    """Append-only JSONL store for exported UI events, one file per chat-cli run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0

    def rotate_on_start(self) -> None:
        """Move a previous run's log aside so each run starts from an empty file."""
        with self._lock:
            self._seq = 0
            if self.path.exists():
                self.path.replace(self.path.with_name(f"{self.path.stem}.prev.jsonl"))

    def next_seq(self) -> int:
        """Allocate the next monotonic sequence number for this run."""
        with self._lock:
            self._seq += 1
            return self._seq

    def append(self, record: UiEventRecord) -> UiEventRecord:
        """Validate and append one record atomically enough for local JSONL use."""
        validated = UiEventRecord.model_validate(record)
        line = validated.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return validated

    def recent_events(self, limit: int = _DEFAULT_RECENT_LIMIT) -> list[UiEventRecord]:
        """Return the most recent valid records in file order."""
        if not self.path.exists():
            return []
        bounded_limit = max(1, min(limit, _MAX_RECENT_LIMIT))
        records: list[UiEventRecord] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(UiEventRecord.model_validate_json(line))
            except Exception:
                continue
        return records[-bounded_limit:]

    def read_from_offset(self, offset: int) -> tuple[list[UiEventRecord], int]:
        """Read valid records appended after *offset* and return the new byte offset."""
        if not self.path.exists():
            return [], 0
        file_size = self.path.stat().st_size
        start = offset if offset <= file_size else 0
        records: list[UiEventRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            fh.seek(start)
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    records.append(UiEventRecord.model_validate(json.loads(line)))
                except Exception:
                    continue
            new_offset = fh.tell()
        return records, new_offset


class UiEventExportSink:
    """UiSink that mirrors typed events into the web export JSONL."""

    def __init__(self, store: UiEventStore) -> None:
        self._store = store
        self._warned = False

    def emit(self, event: UiEvent) -> None:
        # The export is a side channel: it must never break the TUI or the agent.
        try:
            record = serialize_ui_event(event, seq=self._store.next_seq())
            self._store.append(record)
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "UI event export failed; further failures stay silent",
                    exc_info=True,
                )


class FanoutUiSink:
    """Forward every event to several sinks, isolating failures per sink."""

    def __init__(self, sinks: tuple[UiSink, ...]) -> None:
        self._sinks = sinks

    def emit(self, event: UiEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                logger.warning("UI sink %r failed to emit", sink, exc_info=True)
