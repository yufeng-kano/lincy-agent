"""Session persistence manager.

Stores conversation messages as JSONL files with metadata.
Each session lives in its own directory under sessions/.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from ..timezone_utils import now as tz_now
from pathlib import Path

from ..llm import LLMResponse, Message, ServedCandidate, ToolDefinition
from .debug_store import PendingLLMRequest, SessionDebugStore
from .schema import SessionEntry, SessionMetadata

logger = logging.getLogger(__name__)


def _generate_session_id() -> str:
    """Generate a time-sortable session ID: YYYYMMDD_HHMMSS_<6-hex>."""
    now = tz_now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    suffix = os.urandom(3).hex()
    return f"{timestamp}_{suffix}"


class SessionManager:
    """Manage session directories with meta.json + messages.jsonl."""

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._current_id: str | None = None
        self._current_dir: Path | None = None
        self._current_turn_id: str | None = None
        self._debug_store: SessionDebugStore | None = None

    @property
    def current_session_id(self) -> str | None:
        return self._current_id

    @property
    def current_turn_id(self) -> str | None:
        return self._current_turn_id

    def create(self, user_id: str, display_name: str) -> str:
        """Create a new session directory with meta.json. Returns session_id."""
        session_id = _generate_session_id()
        session_dir = self._sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        now = tz_now()
        meta = SessionMetadata(
            session_id=session_id,
            user_id=user_id,
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        self._write_meta(session_dir, meta)
        self._current_id = session_id
        self._current_dir = session_dir
        self._debug_store = SessionDebugStore(session_dir, session_id)
        self.write_checkpoint([])
        return session_id

    def append_message(self, entry: SessionEntry) -> None:
        """Append a session entry to the current session's JSONL file."""
        if self._current_dir is None:
            return

        jsonl_path = self._current_dir / "messages.jsonl"
        line = entry.model_dump_json() + "\n"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

        # Update meta.json (message_count + updated_at)
        meta = self._read_meta(self._current_dir)
        if meta:
            meta.message_count += 1
            meta.updated_at = tz_now()
            self._write_meta(self._current_dir, meta)

    def load(self, session_id: str) -> list[SessionEntry]:
        """Load entries from a session. Sets it as the current session."""
        session_dir = self._sessions_dir / session_id
        if not session_dir.is_dir():
            raise FileNotFoundError(f"Session not found: {session_id}")

        self._current_id = session_id
        self._current_dir = session_dir
        self._debug_store = SessionDebugStore(session_dir, session_id)

        # Mark resumed session as active
        meta = self._read_meta(session_dir)
        if meta and meta.status != "active":
            meta.status = "active"  # type: ignore[assignment]
            meta.updated_at = tz_now()
            self._write_meta(session_dir, meta)

        jsonl_path = session_dir / "messages.jsonl"
        if not jsonl_path.exists():
            return []

        entries: list[SessionEntry] = []
        decoder = json.JSONDecoder()
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(SessionEntry.model_validate_json(line))
                except Exception:
                    # Line may contain multiple concatenated JSON objects
                    self._parse_concatenated(line, decoder, entries)
        return entries

    def rewrite_messages(self, entries: list[SessionEntry]) -> None:
        """Overwrite the current session's JSONL with the given entries.

        Used after ESC interrupt or double-ESC rollback to keep the persisted
        session consistent with the in-memory conversation state.
        """
        if self._current_dir is None:
            return

        jsonl_path = self._current_dir / "messages.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry.model_dump_json() + "\n")
            f.flush()

        meta = self._read_meta(self._current_dir)
        if meta:
            meta.message_count = len(entries)
            meta.updated_at = tz_now()
            self._write_meta(self._current_dir, meta)
        self.write_checkpoint(entries)

    def finalize(self, status: str) -> None:
        """Mark the current session's status in meta.json."""
        if self._current_dir is None:
            return

        meta = self._read_meta(self._current_dir)
        if meta:
            meta.status = status  # type: ignore[assignment]
            meta.updated_at = tz_now()
            self._write_meta(self._current_dir, meta)

    def start_turn(
        self,
        *,
        channel: str,
        sender: str | None,
        inbound_kind: str,
        input_text: str,
        input_timestamp: datetime | None,
        turn_metadata: dict[str, Any] | None,
    ) -> str | None:
        """Start a debug turn record for the current session."""
        if self._debug_store is None:
            return None
        turn_id = self._debug_store.start_turn(
            channel=channel,
            sender=sender,
            inbound_kind=inbound_kind,
            input_text=input_text,
            input_timestamp=input_timestamp,
            turn_metadata=turn_metadata,
        )
        self._current_turn_id = turn_id
        return turn_id

    def finish_turn(
        self,
        *,
        status: str,
        final_content: str | None,
        failure_category: str | None,
        soft_limit_exceeded: bool,
        turn_messages: list[SessionEntry],
        checkpoint_messages: list[SessionEntry],
    ) -> None:
        """Finalize one debug turn summary and refresh the checkpoint."""
        if self._debug_store is None:
            return
        self._debug_store.finish_turn(
            status=status,
            final_content=final_content,
            failure_category=failure_category,
            soft_limit_exceeded=soft_limit_exceeded,
            turn_messages=turn_messages,
            checkpoint_messages=checkpoint_messages,
        )
        self._current_turn_id = None

    def record_compaction(
        self,
        *,
        source: str,
        trigger: str,
        removed_messages: int,
        fallback: bool,
    ) -> None:
        """Persist a compaction debug event for the current session."""
        if self._debug_store is None:
            return
        self._debug_store.record_compaction(
            source=source,
            trigger=trigger,
            removed_messages=removed_messages,
            fallback=fallback,
        )

    def clear_active_turn(self) -> None:
        """Drop any unfinished active-turn debug state."""
        if self._debug_store is None:
            return
        self._debug_store.clear_active_turn()
        self._current_turn_id = None

    def begin_llm_request(
        self,
        *,
        client_label: str,
        provider: str | None,
        model: str | None,
        call_type: str,
        messages: list[Message],
        temperature: float | None,
        tools: list[ToolDefinition] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> PendingLLMRequest | None:
        """Record one normalized LLM request payload."""
        if self._debug_store is None:
            return None
        return self._debug_store.record_llm_request(
            client_label=client_label,
            provider=provider,
            model=model,
            call_type=call_type,
            messages=messages,
            temperature=temperature,
            tools=tools,
            response_schema=response_schema,
        )

    def complete_llm_response(
        self,
        pending: PendingLLMRequest | None,
        *,
        response: LLMResponse,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Record one normalized tool-capable LLM response."""
        if self._debug_store is None or pending is None:
            return
        self._debug_store.record_llm_response(
            pending,
            response=response,
            latency_ms=latency_ms,
            served=served,
        )

    def complete_llm_text_response(
        self,
        pending: PendingLLMRequest | None,
        *,
        response_text: str,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Record one normalized plain-text LLM response."""
        if self._debug_store is None or pending is None:
            return
        self._debug_store.record_text_response(
            pending,
            response_text=response_text,
            latency_ms=latency_ms,
            served=served,
        )

    def fail_llm_request(
        self,
        pending: PendingLLMRequest | None,
        *,
        error: Exception,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Record one failed LLM request attempt."""
        if self._debug_store is None or pending is None:
            return
        self._debug_store.record_llm_error(
            pending,
            error=error,
            latency_ms=latency_ms,
            served=served,
        )

    def write_checkpoint(self, entries: list[SessionEntry]) -> None:
        """Refresh latest checkpoint from the current transcript."""
        if self._debug_store is None:
            return
        self._debug_store.write_checkpoint(entries)

    def write_render_cache(
        self,
        rendered: list,
        boot_fingerprint: str,
    ) -> None:
        """Persist the render cache alongside the checkpoint."""
        if self._debug_store is None:
            return
        self._debug_store.write_render_cache(rendered, boot_fingerprint)

    def load_render_cache(self, boot_fingerprint: str):
        """Load a previously persisted render cache."""
        if self._debug_store is None:
            return None
        return self._debug_store.load_render_cache(boot_fingerprint)

    def list_recent(
        self,
        user_id: str,
        limit: int = 20,
    ) -> list[SessionMetadata]:
        """List recent sessions for a user, sorted by updated_at descending."""
        results: list[SessionMetadata] = []
        if not self._sessions_dir.exists():
            return results

        for entry in self._sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            meta = self._read_meta(entry)
            if meta is None:
                continue
            if meta.user_id != user_id:
                continue
            results.append(meta)

        results.sort(key=lambda m: m.updated_at, reverse=True)
        return results[:limit]

    @staticmethod
    def _parse_concatenated(
        line: str,
        decoder: json.JSONDecoder,
        entries: list[SessionEntry],
    ) -> None:
        """Try to extract multiple JSON objects from a single line."""
        pos = 0
        recovered = 0
        while pos < len(line):
            while pos < len(line) and line[pos] in " \t":
                pos += 1
            if pos >= len(line):
                break
            try:
                _, end = decoder.raw_decode(line, pos)
                fragment = line[pos:end]
                entries.append(SessionEntry.model_validate_json(fragment))
                recovered += 1
                pos = end
            except Exception:
                logger.warning("Skipping unrecoverable fragment in session JSONL")
                break
        if recovered:
            logger.info("Recovered %d entries from concatenated JSONL line", recovered)

    def _write_meta(self, session_dir: Path, meta: SessionMetadata) -> None:
        meta_path = session_dir / "meta.json"
        meta_path.write_text(
            meta.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    def _read_meta(self, session_dir: Path) -> SessionMetadata | None:
        meta_path = session_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            return SessionMetadata.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
