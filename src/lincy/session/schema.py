"""Pydantic models for session persistence."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from ..llm.schema import ContentPart, Message, ToolCall

Role = Literal["user", "assistant", "system", "tool"]


class SessionEntry(BaseModel):
    """A conversation entry with channel metadata.

    Wraps an LLM Message with session-layer fields (channel, sender)
    so the Message schema stays pure for LLM communication.
    """

    message: Message
    channel: str | None = None     # "cli", "line", "discord", ...
    sender: str | None = None      # user identifier
    metadata: dict[str, Any] | None = None  # channel-specific data

    # -- Convenience properties delegating to self.message --

    @property
    def role(self) -> Role:
        return self.message.role

    @property
    def content(self) -> str | list[ContentPart] | None:
        return self.message.content

    @property
    def timestamp(self) -> datetime | None:
        return self.message.timestamp

    @property
    def reasoning_content(self) -> str | None:
        return self.message.reasoning_content

    @property
    def reasoning_details(self) -> list[dict] | None:
        return self.message.reasoning_details

    @property
    def tool_calls(self) -> list[ToolCall] | None:
        return self.message.tool_calls

    @property
    def tool_call_id(self) -> str | None:
        return self.message.tool_call_id

    @property
    def name(self) -> str | None:
        return self.message.name


class SessionMetadata(BaseModel):
    """Metadata for a persisted chat session."""

    session_id: str
    user_id: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    status: Literal["active", "completed", "exited", "truncated", "refreshed"] = "active"
    message_count: int = 0
