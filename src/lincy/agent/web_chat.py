"""Shared Web Chat event model and JSONL store."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..jsonl_store import JsonlStore
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


class WebChatStore(JsonlStore[WebChatEvent]):
    """Append-only JSONL store for Web Chat events."""

    model_type = WebChatEvent

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


