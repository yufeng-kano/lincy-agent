"""Per-turn dynamic prompt blocks: runtime context and decision reminder.

Moved out of ContextBuilder (see src/lincy/context/builder.py) so these
blocks can be applied as a responder-side overlay on the outgoing request's
latest user message (see responder.py) instead of being baked into
ContextBuilder's rendered/frozen conversation messages, where they would
persist forever on every historical user message once frozen by the render
cache.

Kept in a standalone module rather than living directly in responder.py:
staged_planning.py also needs DECISION_REMINDER_LABEL to scrub this block
out of Stage 1 gathering messages, and responder.py already imports
staged_planning.py, so putting these helpers in responder.py would create
a circular import.

Runtime-context time always comes from turn metadata (parse_turn_timing_info),
never a wall-clock read -- see docs/dev/token-only-context-policy.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..session.schema import SessionEntry
from ..timezone_utils import localise as tz_localise
from ..turn_timing import build_turn_timing_notice, parse_turn_timing_info

if TYPE_CHECKING:
    from .note_store import NoteStore

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DECISION_REMINDER_LABEL = "[Decision Reminder]"
_DECISION_REMINDER_TEMPLATE = (
    "Keep {anchors} in mind before acting. Verify constraints, commitments, "
    "blocked state, cooldown, and current risk. Then decide send_message, "
    "schedule_action, or silent wait."
)
_DECISION_REMINDER_WITH_VALUES_TEMPLATE = (
    "Core values to embody:\n{values}\n"
    "Verify constraints from {anchors}, then decide."
)


def build_latest_turn_runtime_context(
    entry: SessionEntry,
    *,
    agent_os_dir: Path | None,
) -> str | None:
    """Build per-turn runtime context using the frozen turn metadata snapshot."""
    parts: list[str] = []
    timing = parse_turn_timing_info(entry)
    if timing is not None and agent_os_dir is not None:
        now_local = tz_localise(timing.processing_started_at)
        day = _DAY_NAMES[now_local.weekday()]
        parts.append(
            now_local.strftime(f"current_local_time: %Y-%m-%d ({day}) %H:%M")
        )
    if agent_os_dir:
        parts.append(f"agent_os_dir: {agent_os_dir}")
    if not parts:
        return None
    return f"[Runtime Context]\n{'\n'.join(parts)}"


def _format_decision_anchor_list(files: list[str]) -> str:
    """Render short file anchors, keeping basenames unless ambiguous."""
    if not files:
        return "key rules"

    counts: dict[str, int] = {}
    basenames = [Path(path).name or path for path in files]
    for name in basenames:
        counts[name] = counts.get(name, 0) + 1

    rendered = [
        name if counts[name] == 1 else path
        for path, name in zip(files, basenames, strict=False)
    ]
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return f"{rendered[0]} and {rendered[1]}"
    return ", ".join(rendered[:-1]) + f", and {rendered[-1]}"


def build_decision_reminder_block(
    *,
    enabled: bool,
    anchor_files: list[str],
    core_values: str | None,
) -> str | None:
    """Build the latest-turn decision reminder text.

    When core values are cached (via inline_section config), inline them
    directly instead of just referencing a file name.
    """
    if not enabled:
        return None
    anchors = _format_decision_anchor_list(anchor_files)
    if core_values:
        body = _DECISION_REMINDER_WITH_VALUES_TEMPLATE.format(
            values=core_values,
            anchors=anchors,
        )
    else:
        body = _DECISION_REMINDER_TEMPLATE.format(anchors=anchors)
    return f"{DECISION_REMINDER_LABEL}\n{body}"


def build_dynamic_turn_overlay_text(
    *,
    entry: SessionEntry,
    agent_os_dir: Path | None,
    decision_reminder_enabled: bool,
    decision_reminder_files: list[str],
    decision_reminder_core_values: str | None,
    note_store: "NoteStore | None",
) -> str:
    """Assemble the responder-only per-turn dynamic overlay text.

    Block order matches ContextBuilder's original latest-turn injection
    order: Runtime Context, Timing Notice, Decision Reminder, Agent Notes.
    The common-ground block (built separately in responder.py) is appended
    after this text by the caller, preserving today's final on-the-wire
    order.
    """
    blocks = [
        build_latest_turn_runtime_context(entry, agent_os_dir=agent_os_dir),
        build_turn_timing_notice(entry),
        build_decision_reminder_block(
            enabled=decision_reminder_enabled,
            anchor_files=decision_reminder_files,
            core_values=decision_reminder_core_values,
        ),
        note_store.format_context_block() if note_store is not None else None,
    ]
    return "\n\n".join(block for block in blocks if block)
