"""Message schema for agent queue protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..timezone_utils import now as tz_now


@dataclass
class InboundMessage:
    """A message entering the agent queue from any channel."""

    channel: str  # "cli", "line", "system"
    content: str
    priority: int  # 0 = highest
    sender: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=tz_now)
    not_before: datetime | None = None  # time lock; None = immediate


@dataclass
class OutboundMessage:
    """A response routed back to the originating channel."""

    channel: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)  # absolute file paths


@dataclass
class PendingOutbound:
    """An outbound message awaiting retry (for unreliable channels like LINE)."""

    message: OutboundMessage
    retry_count: int = 0
    max_retries: int = 3
    next_retry: datetime | None = None


@dataclass
class ShutdownSentinel:
    """Transient control signal to stop the queue loop. Never persisted."""

    graceful: bool = True


@dataclass
class MaintenanceSentinel:
    """Transient control signal to trigger daily maintenance. Never persisted."""

    pass


@dataclass
class NewSessionSentinel:
    """Transient control signal to rotate into a fresh session."""

    pass


@dataclass
class ReloadSentinel:
    """Transient control signal to reload prompt and boot resources."""

    pass


@dataclass
class ReloadSystemPromptSentinel:
    """Transient control signal to reload only the system prompt."""

    pass


@dataclass
class CompactSentinel:
    """Transient control signal to compact the conversation. Never persisted."""

    pass


@dataclass
class ClearSentinel:
    """Transient control signal to clear the conversation. Never persisted."""

    pass


@dataclass
class RestartSentinel:
    """Transient control signal to exit for a process restart once idle. Never persisted."""

    pass
