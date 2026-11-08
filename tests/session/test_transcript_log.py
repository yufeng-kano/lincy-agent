"""Tests for the append-only transcript.jsonl kept next to messages.jsonl."""

from datetime import datetime, timezone
from pathlib import Path

from lincy.llm.schema import Message
from lincy.session.manager import SessionManager
from lincy.session.schema import SessionEntry


def _entry(text: str, *, ts: datetime | None = None, metadata: dict | None = None) -> SessionEntry:
    return SessionEntry(
        message=Message(role="user", content=text, timestamp=ts or datetime.now(timezone.utc)),
        channel="cli",
        sender="alice",
        metadata=metadata,
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


def test_same_message_re_added_with_a_new_overlay_is_logged_once(tmp_path: Path):
    mgr = SessionManager(tmp_path)
    sid = mgr.create("alice", "Alice")
    session_dir = tmp_path / sid
    ts = datetime(2026, 11, 5, 14, 44, tzinfo=timezone.utc)

    mgr.append_message(_entry("same words", ts=ts, metadata={"turn_overlay": "[Recall]\n- a"}))
    # An overflow retry re-adds the entry with a recomputed overlay.
    mgr.append_message(_entry("same words", ts=ts, metadata={"turn_overlay": "[Recall]\n- b"}))
    assert len(_lines(session_dir / "messages.jsonl")) == 2
    assert len(_lines(session_dir / "transcript.jsonl")) == 1

    # The same words sent at another time are a new message.
    mgr.append_message(_entry("same words", ts=ts.replace(minute=45)))
    assert len(_lines(session_dir / "transcript.jsonl")) == 2

    # A new session starts with a clean slate.
    sid2 = mgr.create("alice", "Alice")
    mgr.append_message(_entry("same words", ts=ts))
    assert len(_lines(tmp_path / sid2 / "transcript.jsonl")) == 1
