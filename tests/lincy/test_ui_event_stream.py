from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lincy.agent.ui_event_stream import (
    MAX_FIELD_CHARS,
    TRUNCATION_MARKER,
    FanoutUiSink,
    UiEventExportSink,
    UiEventRecord,
    UiEventStore,
    serialize_ui_event,
)
from lincy.tui.events import (
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
    WarningEvent,
)


TS = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


ALL_EVENT_CASES = [
    (
        InboundMessageEvent(timestamp=TS, channel="web", sender="me", content="hi"),
        "inbound_message",
        {"channel": "web", "sender": "me", "content": "hi"},
    ),
    (
        ProcessingStartedEvent(timestamp=TS, channel="cli", sender=None, label="processing"),
        "processing_started",
        {"channel": "cli", "sender": None, "label": "processing"},
    ),
    (
        ProcessingFinishedEvent(timestamp=TS, channel="cli", sender=None, interrupted=True),
        "processing_finished",
        {"channel": "cli", "sender": None, "interrupted": True},
    ),
    (
        AssistantTextEvent(timestamp=TS, content="thinking"),
        "assistant_text",
        {"content": "thinking"},
    ),
    (
        ToolCallEvent(timestamp=TS, name="read_file", summary="read_file(a)"),
        "tool_call",
        {"name": "read_file", "summary": "read_file(a)"},
    ),
    (
        ToolResultEvent(timestamp=TS, name="read_file", summary="ok", failed=False, warning=True),
        "tool_result",
        {"name": "read_file", "summary": "ok", "failed": False, "warning": True},
    ),
    (
        ToolStreamEvent(timestamp=TS, line="[stream] Bash"),
        "tool_stream",
        {"line": "[stream] Bash"},
    ),
    (WarningEvent(timestamp=TS, message="careful"), "warning", {"message": "careful"}),
    (ErrorEvent(timestamp=TS, message="boom"), "error", {"message": "boom"}),
    (
        DebugEvent(timestamp=TS, label="ctx", message="detail"),
        "debug",
        {"label": "ctx", "message": "detail"},
    ),
    (CtxStatusEvent(timestamp=TS, text="12k/128k"), "ctx_status", {"text": "12k/128k"}),
    (
        ResumeHistoryEvent(timestamp=TS, summary="Resuming session"),
        "resume_history",
        {"summary": "Resuming session"},
    ),
    (
        OutboundMessageEvent(timestamp=TS, channel="web", recipient="me", content="done"),
        "outbound_message",
        {"channel": "web", "recipient": "me", "content": "done"},
    ),
    (
        InterruptStateEvent(timestamp=TS, phase="requested", message="stopping"),
        "interrupt_state",
        {"phase": "requested", "message": "stopping"},
    ),
]


@pytest.mark.parametrize("event,expected_type,expected_data", ALL_EVENT_CASES)
def test_serialize_maps_every_event_type(event, expected_type, expected_data):
    record = serialize_ui_event(event, seq=7)

    assert record.type == expected_type
    assert record.data == expected_data
    assert record.seq == 7
    assert record.ts == TS
    assert record.agent is None
    assert record.id


def test_serialize_rejects_unknown_event_type():
    with pytest.raises(TypeError):
        serialize_ui_event(object(), seq=1)


def test_serialize_extracts_worker_label_from_tool_name():
    call = serialize_ui_event(
        ToolCallEvent(timestamp=TS, name="worker-12 execute_shell", summary="ls"),
        seq=1,
    )
    result = serialize_ui_event(
        ToolResultEvent(timestamp=TS, name="worker-12 execute_shell", summary="ok"),
        seq=2,
    )

    assert call.agent == "worker-12"
    assert call.data["name"] == "execute_shell"
    assert result.agent == "worker-12"
    assert result.data["name"] == "execute_shell"


def test_serialize_attributes_gui_task_events():
    record = serialize_ui_event(
        ToolCallEvent(timestamp=TS, name="gui_task", summary="[1/20] click"),
        seq=1,
    )

    assert record.agent == "gui_task"
    assert record.data["name"] == "gui_task"


def test_serialize_keeps_worker_prefix_out_of_non_tool_events():
    record = serialize_ui_event(
        AssistantTextEvent(timestamp=TS, content="worker-1 execute_shell"),
        seq=1,
    )

    assert record.agent is None


def test_serialize_truncates_long_string_fields():
    record = serialize_ui_event(
        ToolResultEvent(timestamp=TS, name="read_file", summary="x" * (MAX_FIELD_CHARS + 50)),
        seq=1,
    )

    assert record.data["summary"].endswith(TRUNCATION_MARKER)
    assert len(record.data["summary"]) == MAX_FIELD_CHARS + len(TRUNCATION_MARKER)
    assert record.data["failed"] is False


def test_store_appends_and_limits_recent_events(tmp_path):
    store = UiEventStore(tmp_path / "ui_events" / "events.jsonl")
    first = store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="a"), seq=1))
    second = store.append(serialize_ui_event(ErrorEvent(timestamp=TS, message="b"), seq=2))

    assert store.recent_events(1) == [second]
    assert store.recent_events(50) == [first, second]


def test_store_recent_events_skips_malformed_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    store = UiEventStore(path)
    good = store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="a"), seq=1))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")

    assert store.recent_events(50) == [good]


def test_store_rotate_on_start_moves_previous_file_and_restarts_seq(tmp_path):
    path = tmp_path / "events.jsonl"
    store = UiEventStore(path)
    store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="old"), seq=store.next_seq()))
    assert store.next_seq() == 2

    store.rotate_on_start()

    assert not path.exists()
    assert (tmp_path / "events.prev.jsonl").exists()
    assert store.next_seq() == 1
    assert store.recent_events(50) == []


def test_store_rotate_on_start_overwrites_older_prev(tmp_path):
    path = tmp_path / "events.jsonl"
    prev = tmp_path / "events.prev.jsonl"
    prev.write_text("stale\n", encoding="utf-8")
    store = UiEventStore(path)
    store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="new"), seq=1))

    store.rotate_on_start()

    assert "stale" not in prev.read_text(encoding="utf-8")
    assert "new" in prev.read_text(encoding="utf-8")


def test_store_read_from_offset_returns_only_new_records(tmp_path):
    store = UiEventStore(tmp_path / "events.jsonl")
    store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="a"), seq=1))
    first_batch, offset = store.read_from_offset(0)
    second = store.append(serialize_ui_event(ErrorEvent(timestamp=TS, message="b"), seq=2))

    next_batch, next_offset = store.read_from_offset(offset)

    assert len(first_batch) == 1
    assert next_batch == [second]
    assert next_offset > offset


def test_store_read_from_offset_resets_when_file_truncated(tmp_path):
    path = tmp_path / "events.jsonl"
    store = UiEventStore(path)
    store.append(serialize_ui_event(WarningEvent(timestamp=TS, message="a"), seq=1))
    _, offset = store.read_from_offset(0)

    # Simulate rotation: a fresh, shorter file replaces the one we were tailing.
    store.rotate_on_start()
    fresh = store.append(serialize_ui_event(ErrorEvent(timestamp=TS, message="b"), seq=1))

    records, new_offset = store.read_from_offset(offset)

    assert records == [fresh]
    assert new_offset == path.stat().st_size


class _RaisingSink:
    def emit(self, event) -> None:
        raise RuntimeError("sink down")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_fanout_isolates_a_raising_sink():
    good = _RecordingSink()
    fanout = FanoutUiSink((_RaisingSink(), good))
    event = WarningEvent(timestamp=TS, message="a")

    fanout.emit(event)

    assert good.events == [event]


def test_export_sink_writes_records_with_monotonic_seq(tmp_path):
    store = UiEventStore(tmp_path / "events.jsonl")
    sink = UiEventExportSink(store)

    sink.emit(WarningEvent(timestamp=TS, message="a"))
    sink.emit(ErrorEvent(timestamp=TS, message="b"))

    records = store.recent_events(50)
    assert [record.seq for record in records] == [1, 2]
    assert [record.type for record in records] == ["warning", "error"]


def test_export_sink_never_raises_when_store_fails(tmp_path, caplog):
    class _BrokenStore(UiEventStore):
        def append(self, record: UiEventRecord) -> UiEventRecord:
            raise OSError("disk full")

    sink = UiEventExportSink(_BrokenStore(tmp_path / "events.jsonl"))

    with caplog.at_level("WARNING"):
        sink.emit(WarningEvent(timestamp=TS, message="a"))
        sink.emit(WarningEvent(timestamp=TS, message="b"))

    # Warn once, then stay silent so a broken export cannot flood the logs.
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_export_sink_never_raises_on_unknown_event(tmp_path):
    sink = UiEventExportSink(UiEventStore(tmp_path / "events.jsonl"))

    sink.emit(object())


def test_console_subagent_names_stay_parseable(tmp_path):
    """Guard the coupling with UiEventConsole's ``{label} {tool}`` name folding."""
    from lincy.agent.ui_event_console import UiEventConsole
    from lincy.llm.schema import ToolCall

    store = UiEventStore(tmp_path / "events.jsonl")
    console = UiEventConsole(UiEventExportSink(store), show_tool_use=True)
    tool_call = ToolCall(id="c1", name="execute_shell", arguments={"command": "ls"})

    console.print_subagent_tool_call("worker-4", tool_call)
    console.print_subagent_tool_result("worker-4", tool_call, "done")
    console.print_tool_call(tool_call)

    records = store.recent_events(50)
    assert [(r.type, r.agent, r.data["name"]) for r in records] == [
        ("tool_call", "worker-4", "execute_shell"),
        ("tool_result", "worker-4", "execute_shell"),
        ("tool_call", None, "execute_shell"),
    ]
