"""Tests for the per-turn dynamic prompt block builders.

These blocks moved out of ContextBuilder (see src/lincy/context/builder.py)
into standalone functions the responder applies as an overlay (see
agent/responder.py:_build_dynamic_turn_overlay). Covered here as pure
functions; tests/agent/test_turn_overlay_injection.py covers the responder
wiring end to end.
"""

from pathlib import Path

from lincy.agent.note_store import NoteStore
from lincy.agent.turn_overlay import (
    DECISION_REMINDER_LABEL,
    build_decision_reminder_block,
    build_dynamic_turn_overlay_text,
    build_latest_turn_runtime_context,
)
from lincy.llm.schema import Message
from lincy.session.schema import SessionEntry


def _entry(metadata: dict | None = None) -> SessionEntry:
    return SessionEntry(message=Message(role="user", content="hello"), metadata=metadata)


def test_runtime_context_includes_local_time_and_agent_os_dir(tmp_path: Path):
    entry = _entry({"turn_processing_started_at": "2026-03-12T09:11:00+08:00"})

    block = build_latest_turn_runtime_context(entry, agent_os_dir=tmp_path)

    assert block is not None
    assert block.startswith("[Runtime Context]\n")
    assert "current_local_time: 2026-03-12 (Thu) 09:11" in block
    assert f"agent_os_dir: {tmp_path}" in block


def test_runtime_context_without_timing_metadata_still_shows_agent_os_dir(tmp_path: Path):
    entry = _entry(None)

    block = build_latest_turn_runtime_context(entry, agent_os_dir=tmp_path)

    assert block is not None
    assert "current_local_time" not in block
    assert f"agent_os_dir: {tmp_path}" in block


def test_runtime_context_none_without_agent_os_dir():
    entry = _entry({"turn_processing_started_at": "2026-03-12T09:11:00+08:00"})

    assert build_latest_turn_runtime_context(entry, agent_os_dir=None) is None


def test_decision_reminder_disabled_returns_none():
    block = build_decision_reminder_block(
        enabled=False,
        anchor_files=["memory/agent/long-term.md"],
        core_values=None,
    )
    assert block is None


def test_decision_reminder_generic_template_without_core_values():
    block = build_decision_reminder_block(
        enabled=True,
        anchor_files=["memory/agent/long-term.md"],
        core_values=None,
    )

    assert block is not None
    assert block.startswith(f"{DECISION_REMINDER_LABEL}\n")
    assert "Keep long-term.md in mind before acting." in block


def test_decision_reminder_inlines_core_values_when_present():
    block = build_decision_reminder_block(
        enabled=True,
        anchor_files=["memory/agent/long-term.md"],
        core_values="- 主動想著老公這個人\n- 回覆前先想他現在怎麼了",
    )

    assert block is not None
    assert "Core values to embody:" in block
    assert "主動想著老公這個人" in block
    assert "Verify constraints from long-term.md, then decide." in block


def test_dynamic_overlay_text_joins_blocks_in_order(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    note_store = NoteStore(state_dir)
    note_store.create(key="location", value="新竹")
    entry = _entry({"turn_processing_started_at": "2026-03-12T09:11:00+08:00"})

    text = build_dynamic_turn_overlay_text(
        entry=entry,
        agent_os_dir=tmp_path,
        decision_reminder_enabled=True,
        decision_reminder_files=["memory/agent/long-term.md"],
        decision_reminder_core_values=None,
        note_store=note_store,
    )

    runtime_idx = text.index("[Runtime Context]")
    decision_idx = text.index(DECISION_REMINDER_LABEL)
    notes_idx = text.index("[Agent Notes]")
    assert runtime_idx < decision_idx < notes_idx
    assert text.count("[Runtime Context]") == 1
    assert text.count("[Agent Notes]") == 1


def test_dynamic_overlay_text_empty_when_nothing_applies():
    entry = _entry(None)

    text = build_dynamic_turn_overlay_text(
        entry=entry,
        agent_os_dir=None,
        decision_reminder_enabled=False,
        decision_reminder_files=[],
        decision_reminder_core_values=None,
        note_store=None,
    )

    assert text == ""
