from collections.abc import Callable
from datetime import datetime
from typing import Literal
from typing import Any

from ..llm.schema import ContentPart, Message, ToolCall, make_tool_result_message
from ..session.schema import SessionEntry
from ..timezone_utils import now as tz_now

Role = Literal["user", "assistant", "system", "tool"]


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
        """Drop tool-call records whose results never arrived, and orphans.

        A hard interruption (process kill, crash) can persist an assistant
        tool-call entry without its tool results; provider APIs reject such
        history outright. Remove the result-less tool calls (dropping the
        entry entirely when nothing else remains) plus any tool result whose
        call no longer exists. Returns the number of repaired entries.
        """
        result_ids = {
            entry.message.tool_call_id
            for entry in self._messages
            if entry.role == "tool" and entry.message.tool_call_id
        }

        repaired: list[SessionEntry] = []
        kept_call_ids: set[str] = set()
        changed = 0
        for entry in self._messages:
            msg = entry.message
            if msg.role != "assistant" or not msg.tool_calls:
                repaired.append(entry)
                continue
            kept_calls = [tc for tc in msg.tool_calls if tc.id in result_ids]
            if len(kept_calls) == len(msg.tool_calls):
                kept_call_ids.update(tc.id for tc in msg.tool_calls)
                repaired.append(entry)
                continue
            changed += 1
            if not kept_calls and not msg.content:
                continue
            kept_call_ids.update(tc.id for tc in kept_calls)
            new_msg = msg.model_copy(update={"tool_calls": kept_calls or None})
            repaired.append(entry.model_copy(update={"message": new_msg}))

        final: list[SessionEntry] = []
        for entry in repaired:
            if (
                entry.role == "tool"
                and entry.message.tool_call_id
                and entry.message.tool_call_id not in kept_call_ids
            ):
                changed += 1
                continue
            final.append(entry)

        if changed:
            self._messages = final
        return changed

    def compact(self, preserve_turns: int) -> int:
        """Remove old turns, keeping only the last preserve_turns.

        A turn = one user message + all subsequent non-user messages.
        Returns number of messages removed.
        """
        turns: list[list[SessionEntry]] = []
        current_turn: list[SessionEntry] = []
        for entry in self._messages:
            if entry.role == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(entry)
        if current_turn:
            turns.append(current_turn)

        if len(turns) <= preserve_turns:
            return 0

        kept = [entry for turn in turns[-preserve_turns:] for entry in turn]
        removed = len(self._messages) - len(kept)
        self._messages = kept
        return removed

    def clear(self) -> None:
        self._messages.clear()
