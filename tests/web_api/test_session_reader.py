from __future__ import annotations

from pathlib import Path

from chat_web_api.session_reader import (
    FileReadState,
    parse_response_record,
    read_new_lines,
)


def test_read_new_lines_retries_trailing_partial_line(tmp_path: Path):
    path = tmp_path / "responses.jsonl"
    state = FileReadState()
    path.write_text('{"turn_id":"a"}\n{"turn_id":"b","x":', encoding="utf-8")

    assert read_new_lines(path, state) == [{"turn_id": "a"}]
    assert state.byte_offset == len('{"turn_id":"a"}\n'.encode())

    with open(path, "a", encoding="utf-8") as fh:
        fh.write('1}\n{"turn_id":"c"}\n')

    assert read_new_lines(path, state) == [
        {"turn_id": "b", "x": 1},
        {"turn_id": "c"},
    ]


def _legacy_response_row() -> dict:
    """A responses.jsonl row written before served-provider recording existed."""
    return {
        "seq": 1,
        "ts": "2026-08-14T09:00:00+08:00",
        "session_id": "20260814_090000_abcdef",
        "turn_id": "turn_000001",
        "request_id": "req_000001",
        "round": 1,
        "client_label": "brain",
        "provider": "kano_proxy",
        "model": "lincy-brain-agent",
        "call_type": "chat_with_tools",
        "latency_ms": 1200,
        "response": {"content": "ok", "tool_calls": []},
    }


def test_parse_response_record_accepts_rows_without_served_fields():
    record = parse_response_record(_legacy_response_row())

    assert record is not None
    assert record.provider == "kano_proxy"
    assert record.served_provider is None
    assert record.served_model is None
    assert record.served_candidate_index is None


def test_parse_response_record_reads_served_fields():
    raw = _legacy_response_row() | {
        "served_provider": "heyroute",
        "served_model": "deepseek-v3",
        "served_candidate_index": 1,
    }

    record = parse_response_record(raw)

    assert record is not None
    assert record.served_provider == "heyroute"
    assert record.served_model == "deepseek-v3"
    assert record.served_candidate_index == 1
