"""Analyze persisted main-turn tool effects for ctx eviction decisions."""

from __future__ import annotations

from dataclasses import dataclass
import json

from ..llm.content import content_to_text
from ..session.schema import SessionEntry


@dataclass(slots=True)
class TurnEffects:
    """Observed side effects from the main responder turn."""

    had_send_message: bool = False
    had_schedule_mutation: bool = False
    had_memory_edit_applied: bool = False
    had_task_mutation: bool = False
    had_note_mutation: bool = False
    had_listen_mutation: bool = False

    @property
    def is_scheduled_noop(self) -> bool:
        """Return True when a scheduled turn had no durable/user-visible effect."""
        return not (
            self.had_send_message
            or self.had_schedule_mutation
            or self.had_memory_edit_applied
            or self.had_task_mutation
            or self.had_note_mutation
            or self.had_listen_mutation
        )


def analyze_turn_effects(
    turn_messages: list[SessionEntry],
    *,
    had_send_message: bool,
) -> TurnEffects:
    """Analyze main responder conversation entries for durable turn effects.

    Only inspects messages persisted to the main conversation (assistant tool
    calls + tool results). Side-channel memory sync tool calls are not included
    because they never mutate the main conversation history.
    """
    effects = TurnEffects(had_send_message=had_send_message)
    result_by_call_id = _build_tool_result_map(turn_messages)

    for msg in turn_messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tool_call in msg.tool_calls:
            result_msg = result_by_call_id.get(tool_call.id)
            if tool_call.name == "schedule_action":
                action = tool_call.arguments.get("action")
                if action not in {"batch_add", "batch_remove"}:
                    continue
                if result_msg is None:
                    continue
                if _is_successful_schedule_action_result(result_msg):
                    effects.had_schedule_mutation = True
                    continue
            if tool_call.name == "memory_edit":
                if result_msg is None:
                    continue
                if _memory_edit_result_has_applied_item(result_msg):
                    effects.had_memory_edit_applied = True
            if tool_call.name == "agent_task":
                action = tool_call.arguments.get("action")
                if action in {"create", "complete", "update", "remove"} and result_msg is not None:
                    if _is_ok_tool_result(result_msg):
                        effects.had_task_mutation = True
            if tool_call.name == "agent_note":
                action = tool_call.arguments.get("action")
                if (
                    action in {"create", "batch_update", "remove"}
                    and result_msg is not None
                ):
                    if _is_ok_tool_result(result_msg):
                        effects.had_note_mutation = True
            # Changing which Discord channels it follows is the agent's own
            # decision; keep the turn so it remembers making it.
            if (
                tool_call.name == "discord_listen"
                and tool_call.arguments.get("action") == "set"
                and result_msg is not None
                and content_to_text(result_msg.content).strip().startswith("{")
            ):
                effects.had_listen_mutation = True

    return effects


def _build_tool_result_map(turn_messages: list[SessionEntry]) -> dict[str, SessionEntry]:
    """Index tool result messages by tool_call_id (latest wins)."""
    result: dict[str, SessionEntry] = {}
    for msg in turn_messages:
        if msg.role != "tool" or not msg.tool_call_id:
            continue
        result[msg.tool_call_id] = msg
    return result


def _is_successful_schedule_action_result(message: SessionEntry) -> bool:
    """Return True when a schedule_action tool result indicates success."""
    if message.role != "tool" or message.name != "schedule_action":
        return False
    content = content_to_text(message.content).strip()
    if not content:
        return False
    return content.startswith("OK:")


def _is_ok_tool_result(message: SessionEntry) -> bool:
    """Return True when a tool result starts with 'OK:'."""
    if message.role != "tool":
        return False
    text = content_to_text(message.content).strip()
    return text.startswith("OK:")


def _memory_edit_result_has_applied_item(message: SessionEntry) -> bool:
    """Return True when memory_edit result contains an applied item."""
    if message.role != "tool" or message.name != "memory_edit":
        return False
    content = content_to_text(message.content).strip()
    if not content or not content.startswith("{"):
        return False
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    applied = payload.get("applied")
    if not isinstance(applied, list):
        return False
    for item in applied:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "applied":
            return True
    return False
