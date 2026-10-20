"""Per-turn overlay: runtime context, timing notice, agent notes, common ground.

The overlay is computed once when a turn's user entry is added (see
AgentCore._add_turn_user_entry) and stored in that entry's metadata.
ContextBuilder renders it after the message on every later request too, so
history stays append-only: an earlier user message is never rewritten, which
keeps the prompt cache prefix and replayed thinking blocks valid.

[Agent Notes] carries only notes that are new, changed, or removed compared
with what the retained conversation already shows. That shown state is folded
from the retained entries' metadata, so once compaction drops the turns that
showed a note, the next turn shows it again.

Runtime-context time always comes from turn metadata (parse_turn_timing_info),
never a wall-clock read -- see docs/dev/token-only-context-policy.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..context.builder import TURN_OVERLAY_METADATA_KEY
from ..session.schema import SessionEntry
from ..timezone_utils import format_local_stamp
from ..turn_timing import build_turn_timing_notice, parse_turn_timing_info

if TYPE_CHECKING:
    from .note_store import NoteStore

# Note lines this turn's overlay showed, by key; None marks a removed note.
AGENT_NOTES_SHOWN_METADATA_KEY = "agent_notes_shown"


def build_latest_turn_runtime_context(
    entry: SessionEntry,
    *,
    agent_os_dir: Path | None,
) -> str | None:
    """Build per-turn runtime context using the frozen turn metadata snapshot."""
    parts: list[str] = []
    timing = parse_turn_timing_info(entry)
    if timing is not None and agent_os_dir is not None:
        parts.append(
            f"current_local_time: {format_local_stamp(timing.processing_started_at)}"
        )
    if agent_os_dir:
        parts.append(f"agent_os_dir: {agent_os_dir}")
    if not parts:
        return None
    return f"[Runtime Context]\n{'\n'.join(parts)}"


def diff_agent_notes(
    note_store: "NoteStore",
    history: list[SessionEntry],
) -> dict[str, str | None]:
    """Return lines for notes the history does not show as current.

    New or changed notes map to their line; notes the history still shows
    but the store no longer has map to None.
    """
    shown: dict[str, str | None] = {}
    for entry in history:
        shown.update((entry.metadata or {}).get(AGENT_NOTES_SHOWN_METADATA_KEY) or {})
    current = note_store.format_context_lines()
    delta: dict[str, str | None] = {
        key: line for key, line in current.items() if shown.get(key) != line
    }
    for key, line in shown.items():
        if line is not None and key not in current:
            delta[key] = None
    return delta


def build_turn_overlay_metadata(
    *,
    entry: SessionEntry,
    agent_os_dir: Path | None,
    note_store: "NoteStore | None",
    history: list[SessionEntry],
    common_ground: str | None,
) -> dict[str, object]:
    """Return the metadata that stores this turn's overlay on its user entry.

    Block order: Runtime Context, Timing Notice, Agent Notes, common ground.
    ``history`` is the conversation before this entry. Returns {} when no
    block has anything to say.
    """
    notes_delta = diff_agent_notes(note_store, history) if note_store is not None else {}
    notes_block = None
    if notes_delta:
        notes_block = "[Agent Notes]\n" + "\n".join(
            line if line is not None else f"{key}: (removed)"
            for key, line in notes_delta.items()
        )
    blocks = [
        build_latest_turn_runtime_context(entry, agent_os_dir=agent_os_dir),
        build_turn_timing_notice(entry),
        notes_block,
        common_ground,
    ]
    metadata: dict[str, object] = {}
    text = "\n\n".join(block for block in blocks if block)
    if text:
        metadata[TURN_OVERLAY_METADATA_KEY] = text
    if notes_delta:
        metadata[AGENT_NOTES_SHOWN_METADATA_KEY] = notes_delta
    return metadata
