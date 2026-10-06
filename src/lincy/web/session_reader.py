"""Incremental JSONL reader for session debug files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from lincy.session.debug_schema import (
    SessionLLMResponseRecord,
    SessionTurnRecord,
)
from lincy.session.schema import SessionMetadata

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]{6}$")


@dataclass
class FileReadState:
    """Tracks incremental read position for one JSONL file."""

    byte_offset: int = 0


@dataclass
class SessionFiles:
    """Cached read states for a single session's JSONL files."""

    session_dir: Path
    meta: SessionMetadata | None = None
    meta_mtime: float = 0.0
    turns_state: FileReadState = field(default_factory=FileReadState)
    responses_state: FileReadState = field(default_factory=FileReadState)


def read_new_lines(path: Path, state: FileReadState) -> list[dict]:
    """Read complete new JSON lines from *path* starting at *state.byte_offset*.

    Leaves an unterminated trailing line unread so an append in progress is
    retried after its writer finishes. Returns parsed dicts for complete lines.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    if size <= state.byte_offset:
        return []

    results: list[dict] = []
    with open(path, "rb") as fh:
        fh.seek(state.byte_offset)
        for raw_line in fh:
            if not raw_line.endswith(b"\n"):
                break
            state.byte_offset += len(raw_line)
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON line in %s", path)
    return results


def read_meta(session_dir: Path) -> SessionMetadata | None:
    """Read and parse meta.json from a session directory."""
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            return SessionMetadata.model_validate_json(fh.read())
    except Exception:
        logger.warning("Failed to parse meta.json in %s", session_dir, exc_info=True)
        return None


def parse_turn_record(raw: dict) -> SessionTurnRecord | None:
    try:
        return SessionTurnRecord.model_validate(raw)
    except Exception:
        logger.warning("Failed to parse turn record: %s", raw.get("turn_id"))
        return None


def parse_response_record(raw: dict) -> SessionLLMResponseRecord | None:
    try:
        return SessionLLMResponseRecord.model_validate(raw)
    except Exception:
        logger.warning("Failed to parse response record: %s", raw.get("request_id"))
        return None


def discover_sessions(sessions_dir: Path) -> list[str]:
    """Return session IDs found in *sessions_dir*, sorted by name (time-sortable)."""
    if not sessions_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in sessions_dir.iterdir()
        if entry.is_dir() and _SESSION_ID_RE.match(entry.name)
    )
