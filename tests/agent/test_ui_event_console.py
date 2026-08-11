"""Tests for agent UI event replay."""

from datetime import datetime, timezone

from lincy.agent.ui_event_console import UiEventConsole
from lincy.llm.schema import Message
from lincy.session.schema import SessionEntry
from lincy.tui.events import (
    InboundMessageEvent,
    OutboundMessageEvent,
    ProcessingFinishedEvent,
    ResumeHistoryEvent,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_resume_history_skips_non_user_preamble_turn():
    sink = _RecordingSink()
    console = UiEventConsole(sink)
    timestamp = datetime(2026, 8, 11, tzinfo=timezone.utc)
    entries = [
        SessionEntry(
            message=Message(
                role="assistant",
                content="[Codex compaction checkpoint]",
                codex_compaction_encrypted_content="encrypted",
            )
        ),
        SessionEntry(
            message=Message(role="user", content="hello", timestamp=timestamp),
            channel="cli",
            sender="user",
        ),
        SessionEntry(message=Message(role="assistant", content="hi")),
    ]

    console.print_resume_history(entries, replay_turns=None, show_tool_calls=False)

    inbound = [event for event in sink.events if isinstance(event, InboundMessageEvent)]
    assert [event.content for event in inbound] == ["hello"]
    assert [
        event.summary
        for event in sink.events
        if isinstance(event, ResumeHistoryEvent)
        and event.summary == "processing [cli]"
    ] == ["processing [cli]"]
    assert any(
        isinstance(event, OutboundMessageEvent) and event.content == "hi"
        for event in sink.events
    )
    assert any(isinstance(event, ProcessingFinishedEvent) for event in sink.events)
