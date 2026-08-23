"""Per-turn dynamic prompt blocks: runtime context, timing notice, agent notes.

Moved out of ContextBuilder (see src/lincy/context/builder.py) so these
blocks can be applied as a responder-side overlay on the outgoing request's
latest user message (see responder.py) instead of being baked into
ContextBuilder's rendered/frozen conversation messages, where they would
persist forever on every historical user message once frozen by the render
cache.

Kept in a standalone module rather than living directly in responder.py so
the block assembly stays testable on its own and responder.py stays focused
on the tool loop.

Runtime-context time always comes from turn metadata (parse_turn_timing_info),
never a wall-clock read -- see docs/dev/token-only-context-policy.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..session.schema import SessionEntry
from ..timezone_utils import format_local_stamp
from ..turn_timing import build_turn_timing_notice, parse_turn_timing_info

if TYPE_CHECKING:
    from .note_store import NoteStore


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


def build_dynamic_turn_overlay_text(
    *,
    entry: SessionEntry,
    agent_os_dir: Path | None,
    note_store: "NoteStore | None",
) -> str:
    """Assemble the responder-only per-turn dynamic overlay text.

    Block order matches ContextBuilder's original latest-turn injection
    order: Runtime Context, Timing Notice, Agent Notes. The common-ground
    block (built separately in responder.py) is appended after this text
    by the caller, preserving today's final on-the-wire order.
    """
    blocks = [
        build_latest_turn_runtime_context(entry, agent_os_dir=agent_os_dir),
        build_turn_timing_notice(entry),
        note_store.format_context_block() if note_store is not None else None,
    ]
    return "\n\n".join(block for block in blocks if block)
