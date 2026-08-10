"""agent_note tool: structured key-value state tracking.

The agent uses this to create, batch-update, list, and remove notes
that track real-time user state. Each note can have trigger phrases
that cause the system to prompt the agent to review the note when
matching text appears in a user message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from ...llm.schema import ToolDefinition, ToolParameter
from .batch_helpers import format_source_result, normalize_batch_items, validate_source_fields

if TYPE_CHECKING:
    from ...agent.note_store import NoteStore

logger = logging.getLogger(__name__)

AGENT_NOTE_DEFINITION = ToolDefinition(
    name="agent_note",
    description=(
        "Manage structured notes for tracking user state (location, schedule, etc.). "
        "'create' adds one note with optional trigger phrases, "
        "'batch_update' changes one or more existing notes in one call, "
        "'list' shows all notes, "
        "'remove' deletes a note. "
        "Use batch_update for note updates even when updating only one key. "
        "Within one conversation turn, batch related updates instead of making "
        "multiple agent_note calls. The list action is read-only; do not repeat "
        "the same list call in consecutive tool rounds. "
        "Triggers are phrases that, when found in a user message, prompt you to "
        "review and update the note."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["create", "batch_update", "list", "remove"],
        ),
        "key": ToolParameter(
            type="string",
            description="Note key (required for create/remove). Use short, descriptive keys like 'location', 'mood', 'schedule_today'.",
        ),
        "value": ToolParameter(
            type="string",
            description="Note value (required for create). For updates, put value inside updates[].",
        ),
        "triggers": ToolParameter(
            type="array",
            description=(
                "Trigger phrases (optional). When a user message contains any of "
                "these substrings, you'll be prompted to review and update this note. "
                "Example: [\"arrived\", \"got home\", \"heading out\"]"
            ),
            items={"type": "string"},
        ),
        "description": ToolParameter(
            type="string",
            description="Optional description of what this note tracks.",
        ),
        "source_app": ToolParameter(
            type="string",
            description="Optional external source namespace, e.g. 'calendar' or 'reminders'.",
        ),
        "source_id": ToolParameter(
            type="string",
            description="Optional external source item id, such as an event uid or reminder id.",
        ),
        "source_label": ToolParameter(
            type="string",
            description="Optional source label for summary notes, e.g. 'next_event' or 'today_focus'.",
        ),
        "updates": ToolParameter(
            type="array",
            description=(
                "Batch update list for action='batch_update' (max 12). "
                "Use this for note updates even when there is only one item. "
                "Each item updates one existing note and may include key, value, "
                "triggers, description, source_app, source_id, source_label."
            ),
            json_schema={
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                        "triggers": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "description": {"type": "string"},
                        "source_app": {"type": "string"},
                        "source_id": {"type": "string"},
                        "source_label": {"type": "string"},
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
            },
        ),
    },
    required=["action"],
)


def create_agent_note(
    note_store: NoteStore,
    *,
    max_value_chars: int = 80,
    max_notes: int = 12,
) -> Callable[..., str]:
    """Create an agent_note function bound to a note store.

    max_value_chars / max_notes are guardrails (see
    core/schema.py:AgentNoteToolConfig, cfgs/agent.yaml tools.agent_note):
    they reject new writes that would make a note too long or the store
    too large, but never touch existing oversized values already on disk
    (see _append_oversized_warning).
    """

    def agent_note(
        action: str,
        key: str | None = None,
        value: str | None = None,
        triggers: list[str] | None = None,
        description: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
        updates: list[dict[str, Any]] | str | None = None,
    ) -> str:
        source_error = validate_source_fields(
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
        )
        if source_error:
            return source_error
        if action == "create":
            result = _handle_create(
                key,
                value,
                triggers,
                description,
                source_app,
                source_id,
                source_label,
            )
        elif action == "batch_update":
            result = _handle_batch_update(updates)
        elif action == "list":
            result = _handle_list()
        elif action == "remove":
            result = _handle_remove(key)
        else:
            return f"Error: unknown action '{action}'"
        return _append_oversized_warning(result)

    def _handle_create(
        key: str | None,
        value: str | None,
        triggers: list[str] | None,
        description: str | None,
        source_app: str | None,
        source_id: str | None,
        source_label: str | None,
    ) -> str:
        if not key:
            return "Error: 'key' is required for create"
        if value is None:
            return "Error: 'value' is required for create"
        if len(value) > max_value_chars:
            return _value_too_long_error(len(value), max_value_chars)
        if len(note_store.list_all()) >= max_notes:
            return (
                f"Error: note limit reached ({max_notes} notes). Consolidate "
                "related notes into one, or remove an existing note "
                "(agent_note remove) before creating a new one."
            )

        result = note_store.create(
            key=key,
            value=value,
            triggers=triggers,
            description=description,
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
        )
        if isinstance(result, str):
            return result
        parts = [f"OK: created note '{key}'"]
        source = format_source_result(
            result.source_app,
            result.source_label,
            result.source_id,
        )
        if source:
            parts.append(f"source: {source}")
        if result.triggers:
            parts.append(f"triggers: {result.triggers}")
        return " | ".join(parts)

    def _handle_batch_update(
        updates: list[dict[str, Any]] | str | None,
    ) -> str:
        normalized = _normalize_batch_updates(updates)
        if isinstance(normalized, str):
            return normalized

        validation_error = _validate_batch_updates(
            normalized,
            max_value_chars=max_value_chars,
        )
        if validation_error:
            return validation_error
        missing = [
            str(item["key"])
            for item in normalized
            if note_store.get(str(item["key"])) is None
        ]
        if missing:
            return "Error: note(s) not found: " + ", ".join(missing)

        results: list[str] = []
        changed_count = 0
        for item in normalized:
            note = note_store.get(str(item["key"]))
            assert note is not None
            before = _note_snapshot(note)
            updated = note_store.update(
                key=str(item["key"]),
                value=item.get("value"),
                triggers=item.get("triggers"),
                description=item.get("description"),
                source_app=item.get("source_app"),
                source_id=item.get("source_id"),
                source_label=item.get("source_label"),
            )
            assert updated is not None
            changed = before != _note_snapshot(updated)
            if changed:
                changed_count += 1
            status = "changed" if changed else "unchanged"
            source = format_source_result(
                updated.source_app,
                updated.source_label,
                updated.source_id,
            )
            suffix = f" source={source}" if source else ""
            results.append(f"{item['key']}:{status}{suffix}")

        detail = ", ".join(results)
        if changed_count == 0:
            return (
                "NOOP: batch_update made no note changes. Do not call "
                "agent_note again in this turn; finish if no user-visible "
                f"action remains. Results: {detail}"
            )
        return (
            f"OK: batch updated {changed_count}/{len(normalized)} note(s). "
            f"Results: {detail}"
        )

    def _handle_list() -> str:
        return note_store.format_list_detail()

    def _handle_remove(key: str | None) -> str:
        if not key:
            return "Error: 'key' is required for remove"
        if not note_store.remove(key):
            return f"Error: note '{key}' not found"
        return f"OK: removed note '{key}'"

    def _append_oversized_warning(result: str) -> str:
        """Append a warning line listing existing notes over max_value_chars.

        Runs after any successful (non-error) agent_note call. Never
        injected into the [Agent Notes] prompt block itself (see
        NoteStore.format_context_block) -- this is tool-result-only
        feedback so the agent learns about it without bloating every turn.
        """
        if result.lstrip().startswith("Error:"):
            return result
        offenders = sorted(
            (
                (note.key, len(note.value))
                for note in note_store.list_all()
                if len(note.value) > max_value_chars
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if not offenders:
            return result
        detail = ", ".join(f"{key}({length})" for key, length in offenders)
        return (
            f"{result}\n"
            f"warning: {len(offenders)} notes exceed {max_value_chars} chars: "
            f"{detail} — compress them"
        )

    return agent_note


def _value_too_long_error(
    length: int,
    max_value_chars: int,
    *,
    key: str | None = None,
) -> str:
    """Reject message that teaches where long content actually belongs."""
    subject = f"note '{key}' value" if key else "Note value"
    return (
        f"Error: {subject} too long ({length} > {max_value_chars} chars). "
        "Notes hold current-state conclusions only. Write process details "
        "to memory/agent/temp-memory.md and durable lessons to "
        "memory/agent/long-term.md, then retry with a short value."
    )


def _note_snapshot(note: Any) -> tuple[object, ...]:
    return (
        note.value,
        tuple(note.triggers),
        note.description,
        note.source_app,
        note.source_id,
        note.source_label,
    )


def _normalize_batch_updates(
    updates: list[dict[str, Any]] | str | None,
) -> list[dict[str, Any]] | str:
    """Normalize batch update items using the shared scaffold."""
    result = normalize_batch_items(updates, "updates", 12)
    if isinstance(result, str):
        return result.replace("'updates' must be an array", "'updates' must be an array for batch_update").replace(
            "each updates item", "each batch_update item"
        )
    return result


def _validate_batch_updates(
    updates: list[dict[str, Any]],
    *,
    max_value_chars: int,
) -> str | None:
    allowed_keys = {
        "key",
        "value",
        "triggers",
        "description",
        "source_app",
        "source_id",
        "source_label",
    }
    seen: set[str] = set()
    for item in updates:
        extra = sorted(set(item) - allowed_keys)
        if extra:
            return (
                "Error: invalid batch_update item keys: "
                + ", ".join(extra)
            )
        key = item.get("key")
        if not isinstance(key, str) or not key.strip():
            return "Error: each batch_update item requires non-empty 'key'"
        if key in seen:
            return f"Error: duplicate batch_update key '{key}'"
        seen.add(key)
        value = item.get("value")
        if isinstance(value, str) and len(value) > max_value_chars:
            return _value_too_long_error(len(value), max_value_chars, key=key)
        triggers = item.get("triggers")
        if triggers is not None and (
            not isinstance(triggers, list)
            or not all(isinstance(trigger, str) for trigger in triggers)
        ):
            return f"Error: triggers for note '{key}' must be a string array"
        source_error = validate_source_fields(
            source_app=item.get("source_app"),
            source_id=item.get("source_id"),
            source_label=item.get("source_label"),
        )
        if source_error:
            return source_error
    return None
