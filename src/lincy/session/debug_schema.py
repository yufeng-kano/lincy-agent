"""Pydantic models for debug-first session diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm import LLMResponse, Message, ToolDefinition
from .schema import SessionEntry

DebugEventKind = Literal[
    "turn_start",
    "llm_request",
    "llm_response",
    "llm_error",
    "compaction",
    "turn_end",
    "checkpoint",
]


class SessionDebugEvent(BaseModel):
    """Small append-only timeline event for one session."""

    seq: int
    ts: datetime
    session_id: str
    turn_id: str | None = None
    request_id: str | None = None
    kind: DebugEventKind
    client_label: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class SessionLLMRequestRecord(BaseModel):
    """Exact request payload passed to the normalized LLM client interface."""

    seq: int
    ts: datetime
    session_id: str
    turn_id: str | None = None
    request_id: str
    round: int | None = None
    client_label: str
    provider: str | None = None
    model: str | None = None
    call_type: Literal["chat", "chat_with_tools"]
    temperature: float | None = None
    response_schema: dict[str, Any] | None = None
    messages: list[Message]
    tools: list[ToolDefinition] | None = None


class SessionLLMResponseRecord(BaseModel):
    """Normalized LLM response or failure for one logged request.

    ``provider`` / ``model`` stay the *intended* primary profile. The
    ``served_*`` fields say which failover candidate actually handled the call;
    they are ``None`` for records written before failover was recorded and for
    clients without a fallback chain.
    """

    seq: int
    ts: datetime
    session_id: str
    turn_id: str | None = None
    request_id: str
    round: int | None = None
    client_label: str
    provider: str | None = None
    model: str | None = None
    served_provider: str | None = None
    served_model: str | None = None
    served_candidate_index: int | None = None
    call_type: Literal["chat", "chat_with_tools"]
    latency_ms: int
    response: LLMResponse | None = None
    response_text: str | None = None
    error: str | None = None


class SessionTurnRecord(BaseModel):
    """One debug summary row per turn."""

    turn_id: str
    ts_started: datetime
    ts_finished: datetime
    session_id: str
    channel: str
    sender: str | None = None
    inbound_kind: str
    input_timestamp: datetime | None = None
    input_text: str
    turn_metadata: dict[str, Any] | None = None
    status: Literal["completed", "failed", "interrupted"]
    failure_category: str | None = None
    llm_rounds: int = 0
    usage_available: bool = False
    missing_usage: bool = False
    max_prompt_tokens: int | None = None
    completion_tokens_for_max_prompt: int | None = None
    total_tokens_for_max_prompt: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    soft_limit_exceeded: bool = False
    compaction_source: str | None = None
    compaction_trigger: str | None = None
    compacted_messages_removed: int = 0
    compaction_fallback: bool = False
    final_content: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    turn_message_count: int = 0


class SessionCheckpoint(BaseModel):
    """Current conversation snapshot for fast resume/debug inspection."""

    session_id: str
    saved_at: datetime
    messages: list[SessionEntry]
