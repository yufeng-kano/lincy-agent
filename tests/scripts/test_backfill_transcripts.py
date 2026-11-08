"""Tests for scripts/backfill_transcripts.py."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lincy.llm.schema import Message, ToolCall
from lincy.session.schema import SessionEntry

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_transcripts.py"
_spec = importlib.util.spec_from_file_location("backfill_transcripts", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(backfill)

TZ = timezone(timedelta(hours=8))


def _msg(role: str, content, **extra) -> dict:
    payload = {"role": role, "content": content, "tool_calls": [], "tool_call_id": None, "name": None, "timestamp": None}
    payload.update(extra)
    return payload


def _record(seq: int, ts: str, messages: list[dict], *, label: str = "brain") -> str:
    return json.dumps({
        "seq": seq, "ts": ts, "session_id": "s", "request_id": f"req_{seq}", "client_label": label,
        "call_type": "chat_with_tools", "messages": messages, "tools": None,
    }, ensure_ascii=False)


def _session(tmp_path: Path) -> Path:
    session = tmp_path / "session" / "brain" / "20261105_030000_abc123"
    session.mkdir(parents=True)
    boot = [
        _msg("system", "rules"),
        _msg("assistant", None, tool_calls=[{"id": "boot_ctx_0_0", "name": "read_startup_context", "arguments": {}}]),
        _msg("tool", "<file>index</file>", tool_call_id="boot_ctx_0_0", name="read_startup_context"),
        _msg("assistant", None, tool_calls=[{"id": "pinned_ctx_0_0", "name": "read_pinned_context", "arguments": {}}]),
        _msg("tool", "<file>pinned</file>", tool_call_id="pinned_ctx_0_0", name="read_pinned_context"),
    ]
    turn1_user = _msg(
        "user",
        "[2026-11-05 (Thu) 14:44] [discord, from 毓峰] 我去拿藥了\n\n"
        "[Runtime Context]\ncurrent_local_time: 2026-11-05 (Thu) 14:44\n\n[Recall]\n- old fact",
    )
    turn1_call = _msg("assistant", None, tool_calls=[{"id": "t1", "name": "send_message", "arguments": {"body": "太好了"}}])
    turn1_result = _msg("tool", "sent", tool_call_id="t1", name="send_message")
    turn1_final = _msg("assistant", "done")
    sync_reminder = _msg("user", "[MEMORY SYNC - STATE REFRESH]\nrefresh now")
    sync_call = _msg("assistant", None, tool_calls=[{"id": "m1", "name": "memory_edit", "arguments": {"requests": []}}])
    sync_result = _msg("tool", "ok", tool_call_id="m1", name="memory_edit")
    summary = _msg("assistant", "[Conversation summary before compaction]\n- he took the medicine")
    turn2_user = _msg("user", "[2026-11-05 (Thu) 18:00] [discord, from 毓峰] 晚餐吃麥當勞")
    records = [
        _record(1, "2026-11-05T14:44:05+08:00", boot + [turn1_user]),
        _record(2, "2026-11-05T14:44:20+08:00", boot + [turn1_user, turn1_call, turn1_result]),
        _record(3, "2026-11-05T14:45:00+08:00", boot + [turn1_user, turn1_call, turn1_result, turn1_final, sync_reminder]),
        _record(4, "2026-11-05T14:45:30+08:00", boot + [turn1_user, turn1_call, turn1_result, turn1_final, sync_reminder, sync_call, sync_result]),
        _record(5, "2026-11-05T18:00:10+08:00", boot + [summary, turn2_user]),
        _record(6, "2026-11-05T18:00:20+08:00", [_msg("system", "recall prompt"), _msg("user", "Question: x")], label="recall"),
    ]
    (session / "requests.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    # messages.jsonl after compaction: summary + the last turn with its final reply.
    final = SessionEntry(
        message=Message(role="assistant", content="好吃嗎", timestamp=datetime(2026, 11, 5, 18, 0, 40, tzinfo=TZ)),
    )
    user2 = SessionEntry(
        message=Message(role="user", content="晚餐吃麥當勞", timestamp=datetime(2026, 11, 5, 18, 0, 0, tzinfo=TZ)),
        channel="discord", sender="毓峰", metadata={"turn_overlay": "[Runtime Context]\n..."},
    )
    summary_entry = SessionEntry(message=Message(role="assistant", content="[Conversation summary before compaction]\n- x", timestamp=datetime(2026, 11, 5, 17, 59, tzinfo=TZ)))
    (session / "messages.jsonl").write_text(
        "\n".join(e.model_dump_json() for e in (summary_entry, user2, final)) + "\n", encoding="utf-8",
    )
    return session


def test_strip_rendering_recovers_raw_text_and_routing():
    raw, stamp, channel, sender = backfill.strip_rendering(
        "[2026-11-05 (Thu) 14:44] [discord, from Name] hello\n\n[Runtime Context]\nx\n\n[Agent Notes]\ny"
    )
    assert (raw, stamp, channel, sender) == ("hello", "2026-11-05 14:44", "discord", "Name")
    assert backfill.strip_rendering("[2026-11-05 (Thu) 09:00] [system] [HEARTBEAT]\nhi")[:3] == ("[HEARTBEAT]\nhi", "2026-11-05 09:00", "system")
    assert backfill.strip_rendering("plain") == ("plain", None, None, None)


def test_build_entries_unions_requests_and_messages_in_time_order(tmp_path: Path):
    session = _session(tmp_path)

    entries, stats = backfill.build_transcript_entries(session, tz=TZ)

    rendered = [(e.role, (e.content if isinstance(e.content, str) else None), e.message.tool_call_id) for e in entries]
    assert rendered == [
        ("user", "我去拿藥了", None),
        ("assistant", None, None),
        ("tool", "sent", "t1"),
        ("assistant", "done", None),
        ("assistant", None, None),
        ("tool", "ok", "m1"),
        ("user", "晚餐吃麥當勞", None),
        ("assistant", "好吃嗎", None),
    ]
    first = entries[0]
    assert first.channel == "discord" and first.sender == "毓峰"
    assert first.timestamp == datetime(2026, 11, 5, 14, 44, tzinfo=TZ)
    assert first.metadata == {"backfilled_from": "requests.jsonl"}
    assert entries[1].timestamp == datetime.fromisoformat("2026-11-05T14:44:20+08:00")
    # The turn-2 user entry comes from messages.jsonl (it keeps its overlay metadata).
    assert entries[6].metadata == {"turn_overlay": "[Runtime Context]\n..."}
    assert stats == {"records": 6, "from_messages": 2, "from_requests": 6, "total": 8}


def test_backfill_session_writes_skips_and_forces(tmp_path: Path, capsys):
    session = _session(tmp_path)

    report = backfill.backfill_session(session, tz=TZ, dry_run=True, force=False)
    assert report.endswith("(dry-run)")
    assert not (session / "transcript.jsonl").exists()

    report = backfill.backfill_session(session, tz=TZ, dry_run=False, force=False)
    assert report.endswith("(written)")
    lines = (session / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    assert all(SessionEntry.model_validate_json(line) for line in lines)

    assert backfill.backfill_session(session, tz=TZ, dry_run=False, force=False).endswith("(transcript.jsonl exists)")
    assert backfill.backfill_session(session, tz=TZ, dry_run=False, force=True).endswith("(written)")

    assert backfill.main([str(tmp_path), "--dry-run", "--force", "--tz", "+08:00"]) == 0
    assert "20261105_030000_abc123: records=6" in capsys.readouterr().out
