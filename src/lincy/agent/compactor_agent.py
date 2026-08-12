"""Sub-agent that summarizes older conversation turns for soft-limit compaction.

Same distillation discipline as the memory digest prompts (see
memory/curation/worker_dispatch.py:digest_day_via_worker): preserve lessons
learned, agreements, emotional context, and open follow-ups instead of a flat
compression, and reply in the conversation's own language.
"""

from __future__ import annotations

from ..llm.base import LLMClient
from ..llm.schema import ContentPart, Message
from ..session.schema import SessionEntry


def _content_text(content: str | list[ContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(part.text for part in content if part.type == "text" and part.text)


def _entry_line(entry: SessionEntry) -> str:
    """Render one session entry as a single transcript line for the summarizer.

    Only text content matters for a summary; tool call/result structure is
    reduced to a compact marker so the transcript stays readable.
    """
    message = entry.message
    if message.tool_calls:
        names = ", ".join(call.name for call in message.tool_calls)
        return f"{message.role}: [tool call: {names}]"
    if message.role == "tool":
        content = _content_text(message.content)
        return f"tool ({message.name or 'tool'}): {content}" if content else ""
    content = _content_text(message.content)
    return f"{message.role}: {content}" if content else ""


class CompactorAgent:
    """Sub-agent that distills older conversation turns into one summary."""

    def __init__(self, client: LLMClient, system_prompt: str):
        self.client = client
        self.system_prompt = system_prompt

    def summarize(self, entries: list[SessionEntry]) -> str:
        """Summarize a slice of conversation history into distilled text.

        Latency note: this runs synchronously in the turn's critical path
        (triggered from soft-limit compaction), so cfgs/agent.yaml picks a
        fast model for agents.compactor rather than a deep-thinking one.
        """
        transcript = "\n".join(line for line in (_entry_line(e) for e in entries) if line)
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=f"CONVERSATION_TRANSCRIPT\n{transcript}"),
        ]
        return self.client.chat(messages).strip()
