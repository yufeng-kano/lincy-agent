"""Tests for the per-turn overlay builders (agent/turn_overlay.py).

Pure-function coverage; tests/agent/test_turn_overlay_injection.py covers how
AgentCore stores the overlay on the turn's entry and how later requests
render it.
"""

from pathlib import Path

from lincy.agent.note_store import NoteStore
from lincy.agent.turn_overlay import (
    AGENT_NOTES_SHOWN_METADATA_KEY,
    build_latest_turn_runtime_context,
    build_turn_overlay_metadata,
    diff_agent_notes,
)
from lincy.context.builder import TURN_OVERLAY_METADATA_KEY
from lincy.llm.schema import Message
from lincy.session.schema import SessionEntry


def _entry(metadata: dict | None = None) -> SessionEntry:
    return SessionEntry(message=Message(role="user", content="hello"), metadata=metadata)


def _shown(lines: dict[str, str | None]) -> SessionEntry:
    """A history entry whose overlay showed these note lines."""
    return _entry({AGENT_NOTES_SHOWN_METADATA_KEY: lines})


def _note_store(tmp_path: Path, **notes: str) -> NoteStore:
    store = NoteStore(tmp_path / "state")
    for key, value in notes.items():
        store.create(key=key, value=value)
    return store


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


def test_first_turn_shows_every_note(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹", mood="ok")

    assert diff_agent_notes(store, []) == store.format_context_lines()


def test_notes_already_shown_are_left_out(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹", mood="ok")
    history = [_shown(store.format_context_lines())]

    assert diff_agent_notes(store, history) == {}


def test_changed_note_is_shown_again(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹", mood="ok")
    history = [_shown(store.format_context_lines())]

    store.update(key="location", value="台北")

    assert diff_agent_notes(store, history) == {
        "location": store.format_context_lines()["location"],
    }


def test_removed_note_is_marked_once(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹", mood="ok")
    history = [_shown(store.format_context_lines())]

    store.remove("mood")

    assert diff_agent_notes(store, history) == {"mood": None}
    history.append(_shown({"mood": None}))
    assert diff_agent_notes(store, history) == {}


def test_removed_then_recreated_note_is_shown(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹")
    history = [_shown(store.format_context_lines()), _shown({"location": None})]

    assert diff_agent_notes(store, history) == store.format_context_lines()


def test_note_whose_turn_was_compacted_away_is_shown_again(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹", mood="ok")
    lines = store.format_context_lines()
    # Compaction dropped the turn that showed both notes; the retained turn
    # only showed mood after it changed.
    history = [_shown({"mood": lines["mood"]})]

    assert diff_agent_notes(store, history) == {"location": lines["location"]}


def test_overlay_metadata_orders_blocks_and_records_shown_notes(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹")
    entry = _entry({
        "turn_processing_started_at": "2026-03-12T09:11:00+08:00",
        "turn_processing_delay_seconds": 48660,
        "turn_processing_delay_reason": "scheduled_turn",
        "turn_processing_stale": True,
    })

    metadata = build_turn_overlay_metadata(
        entry=entry,
        agent_os_dir=tmp_path,
        note_store=store,
        history=[],
        common_ground="[Common Ground at Message Time]\n\nscope_id: demo",
    )

    text = metadata[TURN_OVERLAY_METADATA_KEY]
    order = [
        text.index("[Runtime Context]"),
        text.index("[Timing Notice]"),
        text.index("[Agent Notes]"),
        text.index("[Common Ground at Message Time]"),
    ]
    assert order == sorted(order)
    assert text.count("[Agent Notes]") == 1
    assert metadata[AGENT_NOTES_SHOWN_METADATA_KEY] == store.format_context_lines()


def test_overlay_metadata_renders_removed_note(tmp_path: Path):
    store = _note_store(tmp_path, location="新竹")
    history = [_shown({**store.format_context_lines(), "mood": 'mood: "ok" | updated_at 03-12 09:00'})]

    metadata = build_turn_overlay_metadata(
        entry=_entry(None),
        agent_os_dir=None,
        note_store=store,
        history=history,
        common_ground=None,
    )

    assert metadata[TURN_OVERLAY_METADATA_KEY] == "[Agent Notes]\nmood: (removed)"
    assert metadata[AGENT_NOTES_SHOWN_METADATA_KEY] == {"mood": None}


def test_overlay_metadata_empty_when_nothing_applies():
    metadata = build_turn_overlay_metadata(
        entry=_entry(None),
        agent_os_dir=None,
        note_store=None,
        history=[],
        common_ground=None,
    )

    assert metadata == {}
