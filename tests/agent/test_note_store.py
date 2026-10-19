"""Tests for NoteStore's prompt-line formatting.

The per-turn overlay (agent/turn_overlay.py) injects these lines, only for
notes the conversation does not already show as current.
"""

import json
from pathlib import Path

from lincy.agent.note_store import NoteStore


def test_format_context_lines_use_stable_short_timestamp(tmp_path: Path):
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

    assert store.format_context_lines() == {
        "location": 'location: "新竹" | updated_at 03-29 14:00',
    }


def test_format_context_lines_leave_out_source_tag(tmp_path: Path):
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

    assert store.format_context_lines() == {
        "meeting_context": (
            'meeting_context: "2026-04-11 14:00-15:00 | 專題會議 [工作]"'
            " | updated_at 04-11 09:30"
        ),
    }
    # The source stays reachable through the tool's list action.
    assert "source: calendar:manual_capture" in store.format_list_detail()


def test_format_context_lines_empty_when_no_notes(tmp_path: Path):
    store = NoteStore(tmp_path / "state")
    assert store.format_context_lines() == {}
