"""Textual TUI foundation for chat-cli (Phase 0/1 scaffolding)."""

from .app import ChatTextualApp
from .controller import TextualController, TurnCancelController
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
from .sink import QueueUiSink, UiSink

__all__ = [
    "AssistantTextEvent",
    "ChatTextualApp",
    "CtxStatusEvent",
    "DebugEvent",
    "ErrorEvent",
    "InboundMessageEvent",
    "InterruptStateEvent",
    "OutboundMessageEvent",
    "ProcessingFinishedEvent",
    "ProcessingStartedEvent",
    "QueueUiSink",
    "ResumeHistoryEvent",
    "TextualController",
    "ToolCallEvent",
    "ToolResultEvent",
    "ToolStreamEvent",
    "TurnCancelController",
    "UiEvent",
    "UiSink",
    "WarningEvent",
]
