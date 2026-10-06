"""Typed UI events emitted by the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypeAlias

from ..timezone_utils import now as tz_now


InterruptPhase = Literal["idle", "requested", "pending", "acknowledged", "completed"]


@dataclass(slots=True)
class UiEventBase:
    """Base class for UI events emitted by the agent/runtime."""

    timestamp: datetime = field(default_factory=tz_now)


@dataclass(slots=True)
class InboundMessageEvent(UiEventBase):
    channel: str = "cli"
    sender: str | None = None
    content: str = ""


@dataclass(slots=True)
class ProcessingStartedEvent(UiEventBase):
    channel: str = "cli"
    sender: str | None = None
    label: str = "processing"


@dataclass(slots=True)
class ProcessingFinishedEvent(UiEventBase):
    channel: str = "cli"
    sender: str | None = None
    interrupted: bool = False


@dataclass(slots=True)
class AssistantTextEvent(UiEventBase):
    content: str = ""


@dataclass(slots=True)
class ToolCallEvent(UiEventBase):
    name: str = ""
    summary: str = ""


@dataclass(slots=True)
class ToolResultEvent(UiEventBase):
    name: str = ""
    summary: str = ""
    failed: bool = False
    warning: bool = False


@dataclass(slots=True)
class ToolStreamEvent(UiEventBase):
    line: str = ""


@dataclass(slots=True)
class WarningEvent(UiEventBase):
    message: str = ""


@dataclass(slots=True)
class ErrorEvent(UiEventBase):
    message: str = ""


@dataclass(slots=True)
class DebugEvent(UiEventBase):
    label: str = ""
    message: str = ""


@dataclass(slots=True)
class CtxStatusEvent(UiEventBase):
    text: str = ""


@dataclass(slots=True)
class ResumeHistoryEvent(UiEventBase):
    summary: str = ""


@dataclass(slots=True)
class OutboundMessageEvent(UiEventBase):
    channel: str = "cli"
    recipient: str | None = None
    content: str = ""


@dataclass(slots=True)
class InterruptStateEvent(UiEventBase):
    phase: InterruptPhase = "idle"
    message: str = ""


UiEvent: TypeAlias = (
    InboundMessageEvent
    | ProcessingStartedEvent
    | ProcessingFinishedEvent
    | AssistantTextEvent
    | ToolCallEvent
    | ToolResultEvent
    | ToolStreamEvent
    | WarningEvent
    | ErrorEvent
    | DebugEvent
    | CtxStatusEvent
    | ResumeHistoryEvent
    | OutboundMessageEvent
    | InterruptStateEvent
)
