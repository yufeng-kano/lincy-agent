"""Shared Web Chat event model and JSONL store."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..timezone_utils import now as tz_now


WebChatKind = Literal["message", "status", "error"]
WebChatRole = Literal["user", "assistant", "system"]
WebChatStatus = Literal["queued", "processing", "idle", "error"]


class WebChatEvent(BaseModel):
    """One durable Web Chat UI event."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = Field(default_factory=tz_now)
    kind: WebChatKind
    role: WebChatRole | None = None
    content: str | None = None
    status: WebChatStatus | None = None
    request_id: str | None = None


class WebChatMessageRequest(BaseModel):
    """Incoming remote-TUI message payload.

    ``channel`` selects which inbound channel the message is attributed to.
    Default is ``cli`` (same as the Textual TUI). ``web`` is not a send option.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    channel: str = "cli"


class WebChatStore:
    """Append-only JSONL store for Web Chat events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, event: WebChatEvent) -> WebChatEvent:
        """Validate and append one event atomically enough for local JSONL use."""
        validated = WebChatEvent.model_validate(event)
        line = validated.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return validated

    def append_event(
        self,
        *,
        kind: WebChatKind,
        role: WebChatRole | None = None,
        content: str | None = None,
        status: WebChatStatus | None = None,
        request_id: str | None = None,
    ) -> WebChatEvent:
        """Build and append one event."""
        return self.append(
            WebChatEvent(
                kind=kind,
                role=role,
                content=content,
                status=status,
                request_id=request_id,
            )
        )

    def recent_events(self, limit: int) -> list[WebChatEvent]:
        """Return the most recent valid events in file order."""
        if not self.path.exists():
            return []
        bounded_limit = max(1, min(limit, 1000))
        events: list[WebChatEvent] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                events.append(WebChatEvent.model_validate_json(line))
            except Exception:
                continue
        return events[-bounded_limit:]

    def read_from_offset(self, offset: int) -> tuple[list[WebChatEvent], int]:
        """Read valid events appended after *offset* and return the new byte offset."""
        if not self.path.exists():
            return [], 0
        file_size = self.path.stat().st_size
        start = offset if offset <= file_size else 0
        events: list[WebChatEvent] = []
        with self.path.open("r", encoding="utf-8") as fh:
            fh.seek(start)
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(WebChatEvent.model_validate(json.loads(line)))
                except Exception:
                    continue
            new_offset = fh.tell()
        return events, new_offset
