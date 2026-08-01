"""Tests for NoteStore's prompt-context formatting.

Runtime injection of [Agent Notes] into requests moved to the responder
overlay (see agent/turn_overlay.py); these tests exercise
NoteStore.format_context_block() directly rather than through
ContextBuilder.build().
"""

import json
from pathlib import Path

from lincy.agent.note_store import NoteStore


def test_format_context_block_uses_stable_short_timestamp(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "notes.json").write_text(
        json.dumps({
            "notes": {
                "location": {
                    "value": "新竹",
                    "triggers": ["到了"],
                    "description": "使用者目前位置",
                    "updated_at": "2026-03-29T14:00:00+08:00",
                }
            }
        }),
        encoding="utf-8",
    )

    store = NoteStore(state_dir)
    block = store.format_context_block()

    assert block is not None
    assert "[Agent Notes]" in block
    assert 'location: "新竹" | updated_at 03-29 14:00' in block
    assert "2026-03-29" not in block
    assert "ago" not in block


def test_format_context_block_includes_source_tag_when_present(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "notes.json").write_text(
        json.dumps({
            "notes": {
                "meeting_context": {
                    "value": "2026-04-11 14:00-15:00 | 專題會議 [工作]",
                    "triggers": [],
                    "description": "Manually captured meeting context",
                    "source_app": "calendar",
                    "source_label": "manual_capture",
                    "updated_at": "2026-04-11T09:30:00+08:00",
                }
            }
        }),
        encoding="utf-8",
    )

    store = NoteStore(state_dir)
    block = store.format_context_block()

    assert block is not None
    assert 'meeting_context: "2026-04-11 14:00-15:00 | 專題會議 [工作]"' in block
    assert "source calendar:manual_capture" in block
    assert "| updated_at 04-11 09:30" in block


def test_format_context_block_returns_none_when_no_notes(tmp_path: Path):
    store = NoteStore(tmp_path / "state")
    assert store.format_context_block() is None
