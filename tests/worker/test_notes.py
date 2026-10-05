import re
import threading

import pytest

from lincy.worker.notes import (
    WorkerNotes,
    compress_worker_notes,
    create_worker_note,
)


def _notes(tmp_path, threshold=1000, max_chars=500) -> WorkerNotes:
    return WorkerNotes(tmp_path / "worker-notes" / "notes.md", threshold, max_chars)


def test_read_missing_file_returns_empty(tmp_path):
    assert _notes(tmp_path).read() == ""


def test_append_writes_dated_bullet_and_creates_dirs(tmp_path):
    notes = _notes(tmp_path)

    notes.append("  example.com: curl gets 403, gui_task works  ")
    notes.append("line one\nline two")

    content = notes.read()
    lines = content.splitlines()
    assert re.fullmatch(
        r"- \[\d{4}-\d{2}-\d{2}\] example\.com: curl gets 403, gui_task works",
        lines[0],
    )
    assert re.fullmatch(r"- \[\d{4}-\d{2}-\d{2}\] line one", lines[1])
    assert lines[2] == "line two"
    assert content.endswith("\n")


def test_append_rejects_empty_text(tmp_path):
    with pytest.raises(ValueError):
        _notes(tmp_path).append("   ")


def test_worker_note_tool_reports_empty_text(tmp_path):
    notes = _notes(tmp_path)
    tool = create_worker_note(notes)

    assert tool(text="  ").startswith("Error")
    assert tool(text="site X needs login") == "Note saved."
    assert "site X needs login" in notes.read()


def test_compress_below_threshold_is_noop(tmp_path):
    notes = _notes(tmp_path)
    notes.append("short")
    calls = []

    assert notes.compress(lambda text: calls.append(text) or "summary") is False
    assert calls == []
    assert "short" in notes.read()


def test_compress_replaces_and_keeps_text_appended_during_summarize(tmp_path):
    notes = _notes(tmp_path)
    notes.path.parent.mkdir(parents=True)
    notes.path.write_text("- old entry\n" * 200, encoding="utf-8")

    def summarize(text: str) -> str:
        assert text.startswith("- old entry")
        notes.append("late lesson")
        return "- merged entry"

    assert notes.compress(summarize) is True
    content = notes.read()
    assert content.startswith("- merged entry\n")
    assert "old entry" not in content
    assert content.rstrip().endswith("late lesson")
    assert list(notes.path.parent.iterdir()) == [notes.path]


def test_compress_failure_leaves_file_untouched(tmp_path):
    notes = _notes(tmp_path)
    notes.path.parent.mkdir(parents=True)
    original = "- old entry\n" * 200
    notes.path.write_text(original, encoding="utf-8")

    def boom(text: str) -> str:
        raise RuntimeError("provider down")

    assert notes.compress(boom) is False
    assert notes.compress(lambda text: "   ") is False
    assert notes.read() == original


def test_compress_skips_when_file_was_rewritten_meanwhile(tmp_path):
    notes = _notes(tmp_path)
    notes.path.parent.mkdir(parents=True)
    notes.path.write_text("- old entry\n" * 200, encoding="utf-8")

    def summarize(text: str) -> str:
        notes.path.write_text("- compressed by another run\n", encoding="utf-8")
        return "- my summary"

    assert notes.compress(summarize) is False
    assert notes.read() == "- compressed by another run\n"


def test_concurrent_compress_summarizes_once(tmp_path):
    notes = _notes(tmp_path)
    notes.path.parent.mkdir(parents=True)
    notes.path.write_text("- old entry\n" * 200, encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_summarize(text: str) -> str:
        calls.append(text)
        entered.set()
        release.wait(timeout=5)
        return "- merged entry"

    results = []
    first = threading.Thread(target=lambda: results.append(notes.compress(slow_summarize)))
    first.start()
    assert entered.wait(timeout=5)

    assert notes.compress(slow_summarize) is False
    release.set()
    first.join(timeout=5)

    assert results == [True]
    assert len(calls) == 1
    assert notes.read().startswith("- merged entry\n")


class _ChatClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.messages = None

    def chat(self, messages, response_schema=None, temperature=None):
        self.messages = messages
        return self.reply


def test_compress_worker_notes_hard_cuts_to_max_chars():
    client = _ChatClient("- " + "x" * 900)

    result = compress_worker_notes(client, "- a\n- b", 500)

    assert len(result) == 500
    assert client.messages[0].role == "system"
    assert "500" in client.messages[0].content
    assert client.messages[1].content == "- a\n- b"
