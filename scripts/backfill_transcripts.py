"""Rebuild session/brain/<id>/transcript.jsonl from the requests.jsonl debug dumps.

Compaction rewrites messages.jsonl, so for sessions created before
transcript.jsonl existed the only complete record of the conversation is the
history embedded in every logged LLM request. This script replays those
requests, keeps the first appearance of each message, merges in whatever
messages.jsonl still holds (it has channel, sender and exact timestamps), and
writes the union as SessionEntry lines ordered by time.

Run once on the production host before upgrading:

    uv run python scripts/backfill_transcripts.py <agent_os_dir> --dry-run
    uv run python scripts/backfill_transcripts.py <agent_os_dir>

Sessions that already have transcript.jsonl are skipped unless --force.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from lincy.llm.content import content_to_text
from lincy.llm.schema import Message
from lincy.session.schema import SessionEntry

BACKFILL_MARKER = {"backfilled_from": "requests.jsonl"}

# "[2026-11-08 (Sun) 02:26] [discord, from Name] text" as rendered by ContextBuilder.
_STAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \([A-Za-z]{3}\) (\d{2}:\d{2})\] ")
_TAG_RE = re.compile(r"^\[([^\[\],]+?)(?:, from ([^\]]+))?\] ")
_OVERLAY_MARKERS = (
    "\n\n[Runtime Context]",
    "\n\n[Timing Notice]",
    "\n\n[Agent Notes]",
    "\n\n[Recall]",
    "\n\n[Common Ground",
)
_SKIPPED_USER_PREFIXES = (
    "[MEMORY SYNC",
    "[ARTIFACT REGISTRY SYNC]",
    "FINALIZATION STEP",
    "[SYSTEM] Your previous response",
)
_COMPACTION_SUMMARY_PREFIX = "[Conversation summary before compaction]"
_SYNTHETIC_TOOLS = {"read_startup_context", "read_pinned_context"}


def strip_rendering(text: str) -> tuple[str, str | None, str | None, str | None]:
    """Undo the prompt rendering of a user message.

    Returns (raw_text, stamp, channel, sender); stamp is 'YYYY-MM-DD HH:MM'.
    """
    stamp = channel = sender = None
    match = _STAMP_RE.match(text)
    if match:
        stamp = f"{match.group(1)} {match.group(2)}"
        text = text[match.end():]
    match = _TAG_RE.match(text)
    if match:
        channel, sender = match.group(1), match.group(2)
        text = text[match.end():]
    cut = min((i for i in (text.find(m) for m in _OVERLAY_MARKERS) if i >= 0), default=-1)
    if cut >= 0:
        text = text[:cut]
    return text.rstrip(), stamp, channel, sender


def _entry_key(entry: SessionEntry) -> tuple:
    text = content_to_text(entry.content).strip()
    tool_ids = tuple(call.id for call in entry.message.tool_calls or [])
    return (entry.role, text, tool_ids, entry.message.tool_call_id)


def _is_skipped(message: Message) -> bool:
    if message.role == "system":
        return True
    text = content_to_text(message.content).lstrip()
    if message.role == "assistant":
        calls = message.tool_calls or []
        if calls and all(call.name in _SYNTHETIC_TOOLS for call in calls):
            return True
        if text.startswith(_COMPACTION_SUMMARY_PREFIX):
            return True
    if message.role == "user" and text.startswith(_SKIPPED_USER_PREFIXES):
        return True
    return False


def _request_entries(session_dir: Path, tz: tzinfo) -> tuple[list[SessionEntry], int]:
    """Replay requests.jsonl; return first-appearance entries and the record count."""
    path = session_dir / "requests.jsonl"
    entries: list[SessionEntry] = []
    seen: set[tuple] = set()
    kept_call_ids: set[str] = set()
    records = 0
    if not path.is_file():
        return entries, records
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records += 1
            if record.get("client_label") != "brain":
                continue
            record_ts = _parse_ts(record.get("ts"))
            for raw in record.get("messages") or []:
                try:
                    message = Message.model_validate(raw)
                except ValueError:
                    continue
                if _is_skipped(message):
                    continue
                entry = _to_entry(message, record_ts, tz)
                if entry is None:
                    continue
                if entry.role == "tool":
                    # Synthetic results (boot files, pinned files, common
                    # ground) have no kept assistant call to answer.
                    if entry.message.tool_call_id not in kept_call_ids:
                        continue
                key = _entry_key(entry)
                if key in seen:
                    continue
                seen.add(key)
                kept_call_ids.update(call.id for call in entry.message.tool_calls or [])
                entries.append(entry)
    return entries, records


def _to_entry(message: Message, record_ts: datetime | None, tz: tzinfo) -> SessionEntry | None:
    channel = sender = None
    timestamp = message.timestamp or record_ts
    content: Any = message.content
    if message.role == "user" and isinstance(content, str):
        content, stamp, channel, sender = strip_rendering(content)
        if stamp:
            timestamp = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        if not content:
            return None
    return SessionEntry(
        message=Message(
            role=message.role,
            content=content,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
            name=message.name,
            timestamp=timestamp,
        ),
        channel=channel,
        sender=sender,
        metadata=dict(BACKFILL_MARKER),
    )


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _messages_entries(session_dir: Path) -> list[SessionEntry]:
    path = session_dir / "messages.jsonl"
    entries: list[SessionEntry] = []
    if not path.is_file():
        return entries
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = SessionEntry.model_validate_json(line)
            except ValueError:
                continue
            if _is_skipped(entry.message):
                continue
            entries.append(entry)
    return entries


def build_transcript_entries(session_dir: Path, *, tz: tzinfo) -> tuple[list[SessionEntry], dict[str, int]]:
    """Union of request-derived and messages.jsonl entries, ordered by time."""
    from_messages = _messages_entries(session_dir)
    from_requests, records = _request_entries(session_dir, tz)
    seen = {_entry_key(entry) for entry in from_messages}
    merged = list(from_messages)
    added = 0
    for entry in from_requests:
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
        added += 1
    # Stable sort: entries without a timestamp keep their relative position
    # at the end, which only happens for malformed records.
    merged.sort(key=lambda e: (e.timestamp is None, e.timestamp or datetime.max.replace(tzinfo=tz)))
    stats = {
        "records": records,
        "from_messages": len(from_messages),
        "from_requests": added,
        "total": len(merged),
    }
    return merged, stats


def backfill_session(session_dir: Path, *, tz: tzinfo, dry_run: bool, force: bool) -> str:
    target = session_dir / "transcript.jsonl"
    if target.exists() and not force:
        return f"{session_dir.name}: skipped (transcript.jsonl exists)"
    entries, stats = build_transcript_entries(session_dir, tz=tz)
    summary = (
        f"{session_dir.name}: records={stats['records']} "
        f"from_messages={stats['from_messages']} from_requests={stats['from_requests']} "
        f"total={stats['total']}"
    )
    if dry_run:
        return f"{summary} (dry-run)"
    if not entries:
        return f"{summary} (nothing to write)"
    tmp = session_dir / "transcript.jsonl.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(entry.model_dump_json() + "\n")
    os.replace(tmp, target)
    return f"{summary} (written)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agent_os_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    parser.add_argument("--force", action="store_true", help="rebuild sessions that already have transcript.jsonl")
    parser.add_argument(
        "--tz", default=None,
        help="UTC offset of the rendered timestamps, e.g. +08:00 (default: system local time)",
    )
    args = parser.parse_args(argv)

    tz = _resolve_tz(args.tz)
    brain_dir = args.agent_os_dir.expanduser() / "session" / "brain"
    if not brain_dir.is_dir():
        print(f"error: {brain_dir} is not a directory", file=sys.stderr)
        return 1
    for session_dir in sorted(p for p in brain_dir.iterdir() if p.is_dir()):
        print(backfill_session(session_dir, tz=tz, dry_run=args.dry_run, force=args.force), flush=True)
    return 0


def _resolve_tz(spec: str | None) -> tzinfo:
    if spec is None:
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        return local
    parsed = datetime.fromisoformat(f"2000-01-01T00:00:00{spec}")
    assert parsed.tzinfo is not None
    return parsed.tzinfo


if __name__ == "__main__":
    raise SystemExit(main())
