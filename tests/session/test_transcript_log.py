"""Tests for the append-only transcript.jsonl kept next to messages.jsonl."""

from datetime import datetime, timezone
from pathlib import Path

from lincy.llm.schema import Message
from lincy.session.manager import SessionManager
from lincy.session.schema import SessionEntry


def _entry(text: str) -> SessionEntry:
    return SessionEntry(
        message=Message(role="user", content=text, timestamp=datetime.now(timezone.utc)),
        channel="cli",
        sender="alice",
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_append_writes_both_files_and_rewrite_only_touches_messages(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    sid = mgr.create("alice", "Alice")
    session_dir = tmp_path / sid

    mgr.append_message(_entry("one"))
    mgr.append_message(_entry("two"))
    assert len(_lines(session_dir / "messages.jsonl")) == 2
    assert _lines(session_dir / "transcript.jsonl") == _lines(session_dir / "messages.jsonl")

    mgr.rewrite_messages([_entry("summary of one and two")])
    assert len(_lines(session_dir / "messages.jsonl")) == 1
    transcript = _lines(session_dir / "transcript.jsonl")
    assert len(transcript) == 2
    assert '"one"' in transcript[0] and '"two"' in transcript[1]


def test_replayed_history_skips_transcript(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    sid = mgr.create("alice", "Alice")
    session_dir = tmp_path / sid

    mgr.append_message(_entry("carried over"), transcript=False)
    mgr.append_message(_entry("fresh"))

    assert len(_lines(session_dir / "messages.jsonl")) == 2
    transcript = _lines(session_dir / "transcript.jsonl")
    assert len(transcript) == 1
    assert '"fresh"' in transcript[0]
