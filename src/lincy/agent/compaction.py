"""Context compaction operations kept outside the agent orchestration module."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Literal

from ..context.conversation import split_turns
from ..llm.schema import Message
from ..session.schema import SessionEntry

logger = logging.getLogger(__name__)

CompactionSource = Literal["compactor", "local", "local_fallback"]
_RENDERED_STATIC_METADATA_KEY = "rendered_static"


@dataclass(frozen=True)
class ContextCompactionResult:
    """One compaction attempt outcome."""

    changed: bool
    removed_messages: int = 0
    source: CompactionSource | None = None
    trigger: str | None = None
    fallback: bool = False

    @property
    def source_label(self) -> str | None:
        labels = {
            "compactor": "compactor agent",
            "local_fallback": "local fallback",
            "local": "local",
        }
        return labels.get(self.source)


class ContextCompactor:
    """Perform compaction against an AgentCore-like runtime."""

    def __init__(self, core) -> None:
        self._core = core

    def record_result(self, result: ContextCompactionResult) -> None:
        if result.source is None or result.trigger is None:
            return
        logger.info(
            "Context compacted via %s (trigger=%s, removed=%d, fallback=%s)",
            result.source,
            result.trigger,
            result.removed_messages,
            result.fallback,
        )
        if self._core.session_mgr is not None:
            self._core.session_mgr.record_compaction(
                source=result.source,
                trigger=result.trigger,
                removed_messages=result.removed_messages,
                fallback=result.fallback,
            )

    def compact_local(
        self, preserve_turns: int, *, trigger: str, fallback: bool = False,
    ) -> ContextCompactionResult:
        removed = self._core.conversation.compact(preserve_turns)
        source: CompactionSource = "local_fallback" if fallback else "local"
        if removed <= 0:
            return ContextCompactionResult(False, source=source, trigger=trigger, fallback=fallback)
        self._core.builder.clear_render_cache()
        if self._core.session_mgr is not None:
            self._core.session_mgr.rewrite_messages(self._core.conversation.get_messages())
        return ContextCompactionResult(True, removed, source, trigger, fallback)

    def compact_via_compactor_agent(
        self, preserve_turns: int, *, trigger: str, fallback: bool = False,
    ) -> ContextCompactionResult:
        """Tier 2: LLM-summarize turns older than preserve_turns into one entry.

        Keeps the most recent preserve_turns turns verbatim (same window
        compact_local would keep) and replaces everything older with a
        single distilled summary message, so history is not lost outright.
        ``fallback`` mirrors compact_local's flag: True when this tier only
        ran after a higher tier failed.
        """
        agent = getattr(self._core, "compactor_agent", None)
        if agent is None:
            return ContextCompactionResult(changed=False)
        previous = self._core.conversation.get_messages()
        turns = split_turns(previous)
        if len(turns) <= preserve_turns:
            return ContextCompactionResult(
                False, source="compactor", trigger=trigger, fallback=fallback,
            )
        old_entries = [entry for turn in turns[:-preserve_turns] for entry in turn]
        kept_entries = [entry for turn in turns[-preserve_turns:] for entry in turn]
        summary = agent.summarize(old_entries)
        if not summary:
            raise ValueError("compactor agent returned an empty summary")
        summary_entry = SessionEntry(
            message=Message(
                role="assistant",
                content=f"[Conversation summary before compaction]\n{summary}",
            ),
            metadata={_RENDERED_STATIC_METADATA_KEY: True},
        )
        new_entries = [summary_entry, *kept_entries]
        self._core.conversation.replace_messages(new_entries)
        self._core.builder.clear_render_cache()
        if self._core.session_mgr is not None:
            self._core.session_mgr.rewrite_messages(new_entries)
        return ContextCompactionResult(
            changed=True,
            removed_messages=max(len(previous) - len(new_entries), 0),
            source="compactor",
            trigger=trigger,
            fallback=fallback,
        )

    def compact(self, *, preserve_turns: int, trigger: str) -> ContextCompactionResult:
        # Tier 1: compactor agent summarization.
        higher_tier_failed = False
        if getattr(self._core, "compactor_agent", None) is not None:
            try:
                result = self.compact_via_compactor_agent(
                    preserve_turns, trigger=trigger, fallback=higher_tier_failed,
                )
                if result.changed:
                    self.record_result(result)
                return result
            except Exception as error:
                logger.warning(
                    "Compactor agent failed during %s; falling back to local compact: %s",
                    trigger,
                    error,
                )
                higher_tier_failed = True

        # Tier 2: last-resort deterministic message drop.
        result = self.compact_local(preserve_turns, trigger=trigger, fallback=higher_tier_failed)
        if result.changed:
            self.record_result(result)
        return result

    def apply_soft_prompt_compaction(self) -> None:
        state = self._core._latest_token_status
        if not state.usage_available or state.prompt_tokens is None:
            return
        if state.prompt_tokens <= self._core._soft_max_prompt_tokens:
            return
        result = self.compact(
            preserve_turns=self._core.config.context.preserve_turns,
            trigger="soft_limit",
        )
        if not result.changed:
            return
        via = f" via {result.source_label}" if result.source_label else ""
        details = (
            f"compacted {result.removed_messages} messages"
            if result.removed_messages > 0 else "compacted context"
        )
        self._core.console.print_warning(
            "Soft token limit exceeded "
            f"({state.prompt_tokens:,}/{self._core._soft_max_prompt_tokens:,}); "
            f"{details}{via}.",
            indent=2,
        )

    def run_manual_compact(self) -> ContextCompactionResult:
        result = self.compact(preserve_turns=self._core.builder.preserve_turns, trigger="manual")
        if result.changed and self._core.session_mgr is not None:
            self._core.session_mgr.finalize("compacted")
            self._core.session_mgr.create(self._core.user_id, self._core.display_name)
            self._core.conversation.set_on_message(self._core.session_mgr.append_message)
        return result
