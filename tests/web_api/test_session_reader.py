from __future__ import annotations

from pathlib import Path

from chat_web_api.session_reader import FileReadState, read_new_lines


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
