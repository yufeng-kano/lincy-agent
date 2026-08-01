"""Tests for the agent_note tool: CRUD actions and config-driven guardrails.

Guardrail config lives in core/schema.py:AgentNoteToolConfig and
cfgs/agent.yaml tools.agent_note (max_value_chars, max_notes); see
docs/dev/agent-task-system.md for the full contract.
"""

from pathlib import Path

from lincy.agent.note_store import NoteStore
from lincy.tools.builtin import (
    AGENT_NOTE_DEFINITION,
    create_agent_note,
)


class TestDefinition:
    def test_agent_note_schema_defines_batch_update_items(self):
        schema = AGENT_NOTE_DEFINITION.to_json_schema()

        assert "update" not in schema["properties"]["action"]["enum"]
        assert "batch_update" in schema["properties"]["action"]["enum"]
        updates_schema = schema["properties"]["updates"]
        assert updates_schema["type"] == "array"
        assert updates_schema["maxItems"] == 12
        assert updates_schema["items"]["type"] == "object"
        assert "key" in updates_schema["items"]["required"]


class TestBatchUpdate:
    def test_agent_note_batch_update_changes_multiple_notes(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store)

        assert tool(action="create", key="location", value="台北").startswith("OK:")
        assert tool(action="create", key="mood", value="休息").startswith("OK:")

        result = tool(
            action="batch_update",
            updates=[
                {"key": "location", "value": "新竹"},
                {"key": "mood", "value": "專注"},
            ],
        )

        assert result.startswith("OK: batch updated 2/2")
        assert note_store.get("location").value == "新竹"
        assert note_store.get("mood").value == "專注"

    def test_agent_note_batch_update_noop_warns_not_to_repeat(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store)

        assert tool(action="create", key="mood", value="專注").startswith("OK:")
        result = tool(
            action="batch_update",
            updates=[{"key": "mood", "value": "專注"}],
        )

        assert result.startswith("NOOP:")
        assert "Do not call agent_note again" in result


class TestActions:
    def test_agent_note_update_action_is_removed(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store)

        result = tool(action="update", key="mood", value="專注")

        assert result == "Error: unknown action 'update'"


class TestValueLengthGuardrail:
    def test_create_accepts_value_at_exact_limit(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="create", key="k", value="x" * 80)

        assert result.startswith("OK:")
        assert note_store.get("k") is not None

    def test_create_rejects_value_over_limit_with_no_state_change(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="create", key="k", value="x" * 81)

        assert result.startswith("Error: Note value too long (81 > 80 chars)")
        assert "memory/agent/temp-memory.md" in result
        assert "memory/agent/long-term.md" in result
        assert note_store.get("k") is None

    def test_batch_update_rejects_value_over_limit_with_no_state_change(
        self, tmp_path: Path
    ):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)
        assert tool(action="create", key="k", value="short").startswith("OK:")

        result = tool(
            action="batch_update",
            updates=[{"key": "k", "value": "y" * 81}],
        )

        assert result.startswith("Error: note 'k' value too long (81 > 80 chars)")
        assert note_store.get("k").value == "short"

    def test_batch_update_accepts_value_at_exact_limit(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)
        assert tool(action="create", key="k", value="short").startswith("OK:")

        result = tool(
            action="batch_update",
            updates=[{"key": "k", "value": "z" * 80}],
        )

        assert result.startswith("OK:")
        assert note_store.get("k").value == "z" * 80


class TestMaxNotesGuardrail:
    def test_create_rejected_when_at_max_notes(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=2)
        assert tool(action="create", key="a", value="1").startswith("OK:")
        assert tool(action="create", key="b", value="2").startswith("OK:")

        result = tool(action="create", key="c", value="3")

        assert result.startswith("Error: note limit reached (2 notes)")
        assert note_store.get("c") is None
        assert len(note_store.list_all()) == 2


class TestOversizedWarning:
    def test_warning_appears_after_successful_call_when_existing_notes_oversized(
        self, tmp_path: Path
    ):
        note_store = NoteStore(tmp_path)
        # Bypass the tool's own guardrail to seed a pre-existing oversized
        # note, simulating one written before the limit existed/was lowered
        # (see docs/dev/agent-task-system.md: existing oversized values
        # keep working, warning-only).
        note_store.create(key="old_note", value="x" * 100)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="create", key="short", value="ok")

        assert result.startswith("OK:")
        assert "warning: 1 notes exceed 80 chars: old_note(100)" in result
        assert "compress them" in result

    def test_no_warning_when_no_notes_oversized(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="create", key="short", value="ok")

        assert result.startswith("OK:")
        assert "warning:" not in result

    def test_warning_not_appended_on_error_result(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        note_store.create(key="old_note", value="x" * 100)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="remove", key="does-not-exist")

        assert result.startswith("Error:")
        assert "warning:" not in result

    def test_warning_lists_multiple_offenders_sorted_by_length_desc(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        note_store.create(key="short_over", value="a" * 90)
        note_store.create(key="long_over", value="b" * 120)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=12)

        result = tool(action="list")

        assert (
            "warning: 2 notes exceed 80 chars: long_over(120), short_over(90)"
            in result
        )


class TestConfiguredLimits:
    """Non-default config values (as loaded from cfgs/agent.yaml) are respected."""

    def test_non_default_max_value_chars_is_respected(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=20, max_notes=12)

        accepted = tool(action="create", key="k1", value="x" * 20)
        rejected = tool(action="create", key="k2", value="x" * 21)

        assert accepted.startswith("OK:")
        assert rejected.startswith("Error: Note value too long (21 > 20 chars)")

    def test_non_default_max_notes_is_respected(self, tmp_path: Path):
        note_store = NoteStore(tmp_path)
        tool = create_agent_note(note_store, max_value_chars=80, max_notes=1)
        assert tool(action="create", key="a", value="1").startswith("OK:")

        result = tool(action="create", key="b", value="2")

        assert result.startswith("Error: note limit reached (1 notes)")
