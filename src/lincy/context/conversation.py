from collections.abc import Callable
from datetime import datetime
from typing import Literal
from typing import Any

from ..llm.schema import ContentPart, Message, ToolCall, make_tool_result_message
from ..session.schema import SessionEntry
from ..timezone_utils import now as tz_now

Role = Literal["user", "assistant", "system", "tool"]


def split_turns(entries: list[Any]) -> list[list[Any]]:
    """Group entries into user-started turns, retaining preamble entries."""
    turns: list[list[Any]] = []
    current_turn: list[Any] = []
    for entry in entries:
        if entry.role == "user" and current_turn:
            turns.append(current_turn)
            current_turn = []
        current_turn.append(entry)
    if current_turn:
        turns.append(current_turn)
    return turns


class Conversation:
    """Stores conversation history as SessionEntry objects."""

    def __init__(
        self,
        on_message: Callable[[SessionEntry], None] | None = None,
    ):
        self._messages: list[SessionEntry] = []
        self._on_message = on_message

    def add(
        self,
        role: Role,
        content: str,
        *,
        channel: str | None = None,
        sender: str | None = None,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        msg = Message(
            role=role,
            content=content,
            timestamp=timestamp or tz_now(),
        )
        entry = SessionEntry(message=msg, channel=channel, sender=sender, metadata=metadata)
        self._messages.append(entry)
        if self._on_message is not None:
            self._on_message(entry)

    def add_assistant_with_tools(
        self,
        content: str | None,
        tool_calls: list[ToolCall],
        *,
        reasoning_content: str | None = None,
        reasoning_details: list[dict] | None = None,
        channel: str | None = None,
    ) -> None:
        """Add an assistant message that includes tool calls."""
        msg = Message(
            role="assistant",
            content=content,
            reasoning_content=reasoning_content,
            reasoning_details=reasoning_details,
            tool_calls=tool_calls,
            timestamp=tz_now(),
        )
        entry = SessionEntry(message=msg, channel=channel)
        self._messages.append(entry)
        if self._on_message is not None:
            self._on_message(entry)

    def add_tool_result(
        self, tool_call_id: str, name: str, result: str | list[ContentPart],
    ) -> None:
        """Add a tool result message."""
        msg = make_tool_result_message(
            tool_call_id=tool_call_id,
            name=name,
            content=result,
            timestamp=tz_now(),
        )
        entry = SessionEntry(message=msg)
        self._messages.append(entry)
        if self._on_message is not None:
            self._on_message(entry)

    def get_messages(self) -> list[SessionEntry]:
        return list(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def replace_messages(self, entries: list[SessionEntry]) -> None:
        """Replace the stored history without emitting callbacks."""
        self._messages = list(entries)

    def truncate_to(self, length: int) -> int:
        """Keep only the first *length* messages and return how many were removed."""
        if length < 0:
            raise ValueError("length must be >= 0")
        if length >= len(self._messages):
            return 0
        removed = len(self._messages) - length
        self._messages = self._messages[:length]
        return removed

    def set_on_message(
        self,
        on_message: Callable[[SessionEntry], None] | None,
    ) -> None:
        """Update the append callback used for future messages."""
        self._on_message = on_message

    def remove_dangling_tool_calls(self) -> int:
        """Drop tool-call records without an adjacent matching result, and orphans.

        A hard interruption can persist an assistant tool-call entry without
        its results. A result in a later turn does not repair that entry:
        provider APIs require tool results to immediately follow the assistant
        tool-call turn. Returns the number of repaired entries.
        """
        result_indexes_by_call: dict[int, set[int]] = {}
        kept_calls_by_index: dict[int, list[ToolCall]] = {}

        for index, entry in enumerate(self._messages):
            msg = entry.message
            if msg.role != "assistant" or not msg.tool_calls:
                continue
            call_ids = {call.id for call in msg.tool_calls}
            result_ids: set[str] = set()
            result_indexes: set[int] = set()
            for result_index in range(index + 1, len(self._messages)):
                result_entry = self._messages[result_index]
                if result_entry.role != "tool":
                    break
                result_id = result_entry.message.tool_call_id
                if result_id in call_ids:
                    result_ids.add(result_id)
                    result_indexes.add(result_index)
            kept_calls_by_index[index] = [
                call for call in msg.tool_calls if call.id in result_ids
            ]
            result_indexes_by_call[index] = result_indexes

        permitted_result_indexes = {
            result_index
            for indexes in result_indexes_by_call.values()
            for result_index in indexes
        }
        repaired: list[SessionEntry] = []
        changed = 0
        for index, entry in enumerate(self._messages):
            msg = entry.message
            if msg.role == "tool":
                if index not in permitted_result_indexes:
                    changed += 1
                    continue
                repaired.append(entry)
                continue
            if msg.role != "assistant" or not msg.tool_calls:
                repaired.append(entry)
                continue

            kept_calls = kept_calls_by_index[index]
            if len(kept_calls) == len(msg.tool_calls):
                repaired.append(entry)
                continue
            changed += 1
            if not kept_calls and not msg.content:
                continue
            repaired.append(
                entry.model_copy(
                    update={"message": msg.model_copy(update={"tool_calls": kept_calls or None})}
                )
            )

        if changed:
            self._messages = repaired
        return changed

    def compact(self, preserve_turns: int) -> int:
        """Remove old turns, keeping only the last preserve_turns.

        A turn = one user message + all subsequent non-user messages.
        Returns number of messages removed.
        """
        turns = split_turns(self._messages)

        if len(turns) <= preserve_turns:
            return 0

        kept = [entry for turn in turns[-preserve_turns:] for entry in turn]
        removed = len(self._messages) - len(kept)
        self._messages = kept
        return removed

    def clear(self) -> None:
        self._messages.clear()
