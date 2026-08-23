"""Per-scope common-ground cache for time-anchored interpretation."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from ..timezone_utils import now as tz_now
from pathlib import Path

from pydantic import BaseModel, Field

from ..llm.schema import Message, ToolCall, make_tool_result_message

logger = logging.getLogger(__name__)

_CACHE_VERSION = 1
_COMMON_GROUND_TOOL_NAME = "_load_common_ground_at_message_time"


class SharedEntry(BaseModel):
    rev: int
    ts: str
    channel: str
    recipient: str | None = None
    body: str


class SharedScopeState(BaseModel):
    rev: int = 0
    entries: list[SharedEntry] = Field(default_factory=list)


class SharedStateCache(BaseModel):
    version: int = _CACHE_VERSION
    scopes: dict[str, SharedScopeState] = Field(default_factory=dict)


@dataclass
class SharedStateLoadResult:
    store: "SharedStateStore"
    loaded: bool
    cache_missing: bool = False
    cache_corrupt: bool = False


class SharedStateStore:
    """Mutable shared-state cache keyed by conversation scope."""

    def __init__(
        self,
        cache_path: Path,
        cache: SharedStateCache | None = None,
        *,
        persist_enabled: bool = True,
    ) -> None:
        self._cache_path = cache_path
        self._cache = cache or SharedStateCache()
        self._lock = threading.Lock()
        self.persist_enabled = persist_enabled

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def get_current_rev(self, scope_id: str) -> int:
        with self._lock:
            scope = self._cache.scopes.get(scope_id)
            return scope.rev if scope else 0

    def record_shared_outbound(
        self,
        *,
        scope_id: str,
        channel: str,
        recipient: str | None,
        body: str,
        ts: datetime | None = None,
    ) -> int:
        """Append a successful outbound send to a scope and increment rev."""
        timestamp = (ts or tz_now()).isoformat()
        with self._lock:
            scope = self._cache.scopes.setdefault(scope_id, SharedScopeState())
            scope.rev += 1
            scope.entries.append(
                SharedEntry(
                    rev=scope.rev,
                    ts=timestamp,
                    channel=channel,
                    recipient=recipient,
                    body=body,
                )
            )
            return scope.rev

    def save(self) -> None:
        """Persist cache atomically."""
        if not self.persist_enabled:
            return
        with self._lock:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(self._cache.model_dump_json(indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._cache_path)

    def build_common_ground_text(
        self,
        *,
        scope_id: str,
        upto_rev: int,
        current_rev: int,
        max_entries: int,
        max_chars: int,
        max_entry_chars: int,
    ) -> str | None:
        """Build common-ground block text for the current turn."""
        with self._lock:
            scope = self._cache.scopes.get(scope_id)
            all_entries = list(scope.entries) if scope else []

        visible = [e for e in all_entries if e.rev <= upto_rev]
        omitted_older_total = max(0, len(visible) - max_entries)
        if omitted_older_total:
            visible = visible[-max_entries:]

        def _clip(text: str, limit: int) -> str:
            if limit <= 0 or len(text) <= limit:
                return text
            if limit <= 3:
                return text[:limit]
            return text[: limit - 3] + "..."

        lines = [f"- rev {e.rev}: {_clip(e.body, max_entry_chars)}" for e in visible]
        omitted_lines = omitted_older_total

        def _render(lines_to_use: list[str], omitted: int) -> str:
            visible_header = (
                "The user had already been told in this conversation when they "
                "sent the current message:"
            )
            body_lines = lines_to_use or ["- [No previously shared messages recorded in this scope]"]
            if omitted > 0:
                body_lines = [f"- ... ({omitted} earlier shared messages omitted)"] + body_lines
            return (
                "[Common Ground at Message Time]\n\n"
                f"scope_id: {scope_id}\n"
                f"message_time_shared_rev: {upto_rev}\n"
                f"turn_start_shared_rev: {current_rev}\n\n"
                f"{visible_header}\n"
                + "\n".join(body_lines)
                + "\n\nInterpret ambiguous references in the current message based on the above shared context.\n"
                "Do not use information shared later in this same conversation to reinterpret the user's earlier wording.\n"
                "If still ambiguous, ask a clarifying question before sending/quoting/relaying."
            )

        text = _render(lines, omitted_lines)
        while len(text) > max_chars and lines:
            lines.pop(0)
            omitted_lines += 1
            text = _render(lines, omitted_lines)
        if len(text) > max_chars:
            # Fallback: hard cap the rendered block to protect the request.
            text = text[: max(0, max_chars - 3)] + "..."
        return text

    def build_common_ground_synthetic_messages(
        self,
        *,
        scope_id: str,
        upto_rev: int,
        current_rev: int,
        max_entries: int,
        max_chars: int,
        max_entry_chars: int,
        tool_call_id: str = "cg_anchor_0",
    ) -> tuple[Message, Message] | None:
        """Build a synthetic assistant+tool pair for common-ground context."""
        text = self.build_common_ground_text(
            scope_id=scope_id,
            upto_rev=upto_rev,
            current_rev=current_rev,
            max_entries=max_entries,
            max_chars=max_chars,
            max_entry_chars=max_entry_chars,
        )
        if not text:
            return None
        call_msg = Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id=tool_call_id,
                    name=_COMMON_GROUND_TOOL_NAME,
                    arguments={
                        "scope_id": scope_id,
                        "message_time_shared_rev": upto_rev,
                    },
                )
            ],
        )
        result_msg = make_tool_result_message(
            tool_call_id=tool_call_id,
            name=_COMMON_GROUND_TOOL_NAME,
            content=text,
        )
        return call_msg, result_msg

    @classmethod
    def load_or_init(cls, cache_path: Path) -> SharedStateLoadResult:
        if not cache_path.exists():
            return SharedStateLoadResult(
                store=cls(cache_path, SharedStateCache()),
                loaded=False,
                cache_missing=True,
            )
        try:
            cache = SharedStateCache.model_validate_json(cache_path.read_text(encoding="utf-8"))
            if cache.version != _CACHE_VERSION:
                logger.warning(
                    "shared_state cache version mismatch: got %s expected %s; starting empty",
                    cache.version,
                    _CACHE_VERSION,
                )
                return SharedStateLoadResult(
                    store=cls(cache_path, SharedStateCache()),
                    loaded=False,
                    cache_corrupt=True,
                )
            return SharedStateLoadResult(store=cls(cache_path, cache), loaded=True)
        except Exception:
            logger.warning("Failed to load shared_state cache; starting empty", exc_info=True)
            return SharedStateLoadResult(
                store=cls(cache_path, SharedStateCache()),
                loaded=False,
                cache_corrupt=True,
            )


def load_or_init(cache_path: Path) -> SharedStateLoadResult:
    """Convenience wrapper used by CLI wiring."""
    return SharedStateStore.load_or_init(cache_path)
