"""Context compaction operations kept outside the agent orchestration module."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Literal

from ..session.schema import SessionEntry

logger = logging.getLogger(__name__)

CompactionSource = Literal["codex_remote", "local", "local_fallback"]
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
            "codex_remote": "codex remote",
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

    def compact_remote(self, *, trigger: str) -> ContextCompactionResult:
        client = getattr(self._core, "conversation_compaction_client", None)
        if client is None:
            return ContextCompactionResult(changed=False)
        rendered = self._core.builder.build(self._core.conversation)
        compacted = client.compact_messages(rendered, tools=self._core.registry.get_definitions())
        if not compacted:
            return ContextCompactionResult(False, source="codex_remote", trigger=trigger)
        previous = self._core.conversation.get_messages()
        entries = [
            SessionEntry(message=message, metadata={_RENDERED_STATIC_METADATA_KEY: True})
            for message in compacted
        ]
        self._core.conversation.replace_messages(entries)
        self._core.builder.clear_render_cache()
        if self._core.session_mgr is not None:
            self._core.session_mgr.rewrite_messages(entries)
        return ContextCompactionResult(
            changed=entries != previous,
            removed_messages=max(len(previous) - len(entries), 0),
            source="codex_remote",
            trigger=trigger,
        )

    def compact(self, *, preserve_turns: int, trigger: str) -> ContextCompactionResult:
        if getattr(self._core, "conversation_compaction_client", None) is not None:
            try:
                result = self.compact_remote(trigger=trigger)
            except Exception as error:
                logger.warning(
                    "Codex remote compaction failed during %s; falling back to local compact: %s",
                    trigger,
                    error,
                )
                result = self.compact_local(preserve_turns, trigger=trigger, fallback=True)
        else:
            result = self.compact_local(preserve_turns, trigger=trigger)
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
