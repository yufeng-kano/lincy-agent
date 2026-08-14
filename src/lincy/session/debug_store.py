"""Debug-first session logging helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..llm import LLMResponse, Message, ServedCandidate, ToolDefinition
from ..timezone_utils import now as tz_now
from .debug_schema import (
    SessionCheckpoint,
    SessionDebugEvent,
    SessionLLMRequestRecord,
    SessionLLMResponseRecord,
    SessionTurnRecord,
)
from .schema import SessionEntry


def _served_fields(served: ServedCandidate | None) -> dict[str, Any]:
    """Fields naming the failover candidate that actually served one call.

    Empty when unknown (no fallback chain), so readers can tell "unknown" from
    "the primary served it".
    """
    if served is None:
        return {}
    return {
        "served_provider": served.provider,
        "served_model": served.model,
        "served_candidate_index": served.index,
    }


@dataclass
class PendingLLMRequest:
    """Internal correlation handle for one logged LLM request."""

    request_id: str
    turn_id: str | None
    round: int | None
    client_label: str
    provider: str | None
    model: str | None
    call_type: str


@dataclass
class _ActiveTurnState:
    """Mutable in-memory aggregation for the active turn."""

    turn_id: str
    started_at: datetime
    channel: str
    sender: str | None
    inbound_kind: str
    input_text: str
    input_timestamp: datetime | None
    turn_metadata: dict[str, Any] | None
    llm_rounds: int = 0
    usage_available: bool = False
    missing_usage: bool = False
    max_prompt_tokens: int | None = None
    completion_tokens_for_max_prompt: int | None = None
    total_tokens_for_max_prompt: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    compaction_source: str | None = None
    compaction_trigger: str | None = None
    compacted_messages_removed: int = 0
    compaction_fallback: bool = False

    def next_round(self) -> int:
        self.llm_rounds += 1
        return self.llm_rounds

    def record_usage(self, response: LLMResponse) -> None:
        if not response.usage_available:
            self.missing_usage = True
            return
        self.usage_available = True
        self.cache_read_tokens += response.cache_read_tokens
        self.cache_write_tokens += response.cache_write_tokens
        if response.prompt_tokens is None:
            return
        if (
            self.max_prompt_tokens is None
            or response.prompt_tokens >= self.max_prompt_tokens
        ):
            self.max_prompt_tokens = response.prompt_tokens
            self.completion_tokens_for_max_prompt = response.completion_tokens
            self.total_tokens_for_max_prompt = response.total_tokens


class SessionDebugStore:
    """Append-only debug artifacts kept alongside transcript session files."""

    def __init__(self, session_dir: Path, session_id: str) -> None:
        import threading

        self._session_dir = session_dir
        self._session_id = session_id
        self._checkpoints_dir = session_dir / "checkpoints"
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._event_seq = self._count_jsonl_lines(session_dir / "events.jsonl")
        self._request_seq = self._count_jsonl_lines(session_dir / "requests.jsonl")
        self._response_seq = self._count_jsonl_lines(session_dir / "responses.jsonl")
        self._turn_seq = self._count_jsonl_lines(session_dir / "turns.jsonl")
        self._active_turn: _ActiveTurnState | None = None
        self._seq_lock = threading.Lock()

    def start_turn(
        self,
        *,
        channel: str,
        sender: str | None,
        inbound_kind: str,
        input_text: str,
        input_timestamp: datetime | None,
        turn_metadata: dict[str, Any] | None,
    ) -> str:
        """Start a new active turn and append a small timeline event."""
        self._turn_seq += 1
        turn_id = f"turn_{self._turn_seq:06d}"
        active = _ActiveTurnState(
            turn_id=turn_id,
            started_at=tz_now(),
            channel=channel,
            sender=sender,
            inbound_kind=inbound_kind,
            input_text=input_text,
            input_timestamp=input_timestamp,
            turn_metadata=dict(turn_metadata) if turn_metadata is not None else None,
        )
        self._active_turn = active
        self._append_event(
            kind="turn_start",
            turn_id=turn_id,
            data={
                "channel": channel,
                "sender": sender,
                "inbound_kind": inbound_kind,
                "input_chars": len(input_text),
            },
        )
        return turn_id

    def record_llm_request(
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
    ) -> PendingLLMRequest:
        """Persist the normalized request payload and a compact event."""
        with self._seq_lock:
            self._request_seq += 1
            seq = self._request_seq
            turn_id = self._active_turn.turn_id if self._active_turn is not None else None
            round_index = (
                self._active_turn.next_round() if self._active_turn is not None else None
            )
        request_id = f"req_{seq:06d}"
        pending = PendingLLMRequest(
            request_id=request_id,
            turn_id=turn_id,
            round=round_index,
            client_label=client_label,
            provider=provider,
            model=model,
            call_type=call_type,
        )
        request = SessionLLMRequestRecord(
            seq=seq,
            ts=tz_now(),
            session_id=self._session_id,
            turn_id=turn_id,
            request_id=request_id,
            round=round_index,
            client_label=client_label,
            provider=provider,
            model=model,
            call_type=call_type,  # type: ignore[arg-type]
            temperature=temperature,
            response_schema=response_schema,
            messages=messages,
            tools=tools,
        )
        self._append_jsonl("requests.jsonl", request)
        self._append_event(
            kind="llm_request",
            turn_id=turn_id,
            request_id=request_id,
            client_label=client_label,
            data={
                "round": round_index,
                "provider": provider,
                "model": model,
                "call_type": call_type,
                "message_count": len(messages),
                "tool_count": len(tools or []),
            },
        )
        return pending

    def record_llm_response(
        self,
        pending: PendingLLMRequest,
        *,
        response: LLMResponse,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Persist one normalized tool-capable LLM response."""
        with self._seq_lock:
            if self._active_turn is not None and pending.turn_id == self._active_turn.turn_id:
                self._active_turn.record_usage(response)
            self._response_seq += 1
            resp_seq = self._response_seq
        served_fields = _served_fields(served)
        record = SessionLLMResponseRecord(
            seq=resp_seq,
            ts=tz_now(),
            session_id=self._session_id,
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            round=pending.round,
            client_label=pending.client_label,
            provider=pending.provider,
            model=pending.model,
            call_type=pending.call_type,  # type: ignore[arg-type]
            latency_ms=latency_ms,
            response=response,
            **served_fields,
        )
        self._append_jsonl("responses.jsonl", record)
        self._append_event(
            kind="llm_response",
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            client_label=pending.client_label,
            data={
                "round": pending.round,
                **served_fields,
                "finish_reason": response.finish_reason,
                "tool_call_count": len(response.tool_calls),
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
                "cache_read_tokens": response.cache_read_tokens,
                "cache_write_tokens": response.cache_write_tokens,
                "usage_available": response.usage_available,
                "latency_ms": latency_ms,
            },
        )

    def record_text_response(
        self,
        pending: PendingLLMRequest,
        *,
        response_text: str,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Persist one plain-text LLM response."""
        with self._seq_lock:
            self._response_seq += 1
            resp_seq = self._response_seq
        served_fields = _served_fields(served)
        record = SessionLLMResponseRecord(
            seq=resp_seq,
            ts=tz_now(),
            session_id=self._session_id,
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            round=pending.round,
            client_label=pending.client_label,
            provider=pending.provider,
            model=pending.model,
            call_type=pending.call_type,  # type: ignore[arg-type]
            latency_ms=latency_ms,
            response_text=response_text,
            **served_fields,
        )
        self._append_jsonl("responses.jsonl", record)
        self._append_event(
            kind="llm_response",
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            client_label=pending.client_label,
            data={
                "round": pending.round,
                **served_fields,
                "response_chars": len(response_text),
                "latency_ms": latency_ms,
            },
        )

    def record_llm_error(
        self,
        pending: PendingLLMRequest,
        *,
        error: Exception,
        latency_ms: int,
        served: ServedCandidate | None = None,
    ) -> None:
        """Persist one LLM failure tied to a normalized request."""
        with self._seq_lock:
            self._response_seq += 1
            resp_seq = self._response_seq
        served_fields = _served_fields(served)
        record = SessionLLMResponseRecord(
            seq=resp_seq,
            ts=tz_now(),
            session_id=self._session_id,
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            round=pending.round,
            client_label=pending.client_label,
            provider=pending.provider,
            model=pending.model,
            call_type=pending.call_type,  # type: ignore[arg-type]
            latency_ms=latency_ms,
            error=f"{type(error).__name__}: {error}",
            **served_fields,
        )
        self._append_jsonl("responses.jsonl", record)
        self._append_event(
            kind="llm_error",
            turn_id=pending.turn_id,
            request_id=pending.request_id,
            client_label=pending.client_label,
            data={
                "round": pending.round,
                **served_fields,
                "error_type": type(error).__name__,
                "error": str(error),
                "latency_ms": latency_ms,
            },
        )

    def finish_turn(
        self,
        *,
        status: str,
        final_content: str | None,
        failure_category: str | None,
        soft_limit_exceeded: bool,
        turn_messages: Iterable[SessionEntry],
        checkpoint_messages: list[SessionEntry],
    ) -> None:
        """Write one turn summary row and refresh the latest checkpoint."""
        active = self._active_turn
        if active is None:
            self.write_checkpoint(checkpoint_messages)
            return

        finished_at = tz_now()
        turn_messages_list = list(turn_messages)
        tool_names = self._collect_tool_names(turn_messages_list)
        turn_record = SessionTurnRecord(
            turn_id=active.turn_id,
            ts_started=active.started_at,
            ts_finished=finished_at,
            session_id=self._session_id,
            channel=active.channel,
            sender=active.sender,
            inbound_kind=active.inbound_kind,
            input_timestamp=active.input_timestamp,
            input_text=active.input_text,
            turn_metadata=active.turn_metadata,
            status=status,  # type: ignore[arg-type]
            failure_category=failure_category,
            llm_rounds=active.llm_rounds,
            usage_available=active.usage_available,
            missing_usage=active.missing_usage,
            max_prompt_tokens=active.max_prompt_tokens,
            completion_tokens_for_max_prompt=active.completion_tokens_for_max_prompt,
            total_tokens_for_max_prompt=active.total_tokens_for_max_prompt,
            cache_read_tokens=active.cache_read_tokens,
            cache_write_tokens=active.cache_write_tokens,
            soft_limit_exceeded=soft_limit_exceeded,
            compaction_source=active.compaction_source,
            compaction_trigger=active.compaction_trigger,
            compacted_messages_removed=active.compacted_messages_removed,
            compaction_fallback=active.compaction_fallback,
            final_content=final_content,
            tool_names=tool_names,
            turn_message_count=len(turn_messages_list),
        )
        self._append_jsonl("turns.jsonl", turn_record)
        self._append_event(
            kind="turn_end",
            turn_id=active.turn_id,
            data={
                "status": status,
                "failure_category": failure_category,
                "llm_rounds": active.llm_rounds,
                "max_prompt_tokens": active.max_prompt_tokens,
                "cache_read_tokens": active.cache_read_tokens,
                "cache_write_tokens": active.cache_write_tokens,
                "tool_names": tool_names,
                "final_content_chars": len(final_content or ""),
                "soft_limit_exceeded": soft_limit_exceeded,
                "compaction_source": active.compaction_source,
                "compaction_trigger": active.compaction_trigger,
                "compacted_messages_removed": active.compacted_messages_removed,
                "compaction_fallback": active.compaction_fallback,
            },
        )
        self.write_checkpoint(checkpoint_messages)
        self._active_turn = None

    def record_compaction(
        self,
        *,
        source: str,
        trigger: str,
        removed_messages: int,
        fallback: bool,
    ) -> None:
        """Append a compaction event and attach it to the active turn when present."""
        active = self._active_turn
        if active is not None:
            active.compaction_source = source
            active.compaction_trigger = trigger
            active.compacted_messages_removed += removed_messages
            active.compaction_fallback = active.compaction_fallback or fallback
        self._append_event(
            kind="compaction",
            turn_id=active.turn_id if active is not None else None,
            data={
                "source": source,
                "trigger": trigger,
                "removed_messages": removed_messages,
                "fallback": fallback,
            },
        )

    def write_checkpoint(self, messages: list[SessionEntry]) -> None:
        """Overwrite the latest checkpoint with the current transcript."""
        checkpoint = SessionCheckpoint(
            session_id=self._session_id,
            saved_at=tz_now(),
            messages=messages,
        )
        path = self._checkpoints_dir / "latest.json"
        path.write_text(checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8")
        self._append_event(
            kind="checkpoint",
            data={
                "path": str(path.relative_to(self._session_dir)),
                "message_count": len(messages),
            },
        )

    def write_render_cache(
        self,
        rendered: list[Message],
        boot_fingerprint: str,
    ) -> None:
        """Persist the render cache alongside the checkpoint.

        File format (JSONL):
          Line 0: header  {"version": 1, "boot_fingerprint": "...", "count": N}
          Lines 1..N: one rendered Message JSON per line

        Written atomically via temp + rename.
        """
        import json
        import tempfile

        path = self._checkpoints_dir / "render_cache.jsonl"
        header = json.dumps({
            "version": 1,
            "boot_fingerprint": boot_fingerprint,
            "count": len(rendered),
        })
        fd, tmp = tempfile.mkstemp(
            dir=self._checkpoints_dir, suffix=".tmp",
        )
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(header + "\n")
                for msg in rendered:
                    f.write(msg.model_dump_json() + "\n")
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load_render_cache(
        self,
        boot_fingerprint: str,
    ) -> list[Message] | None:
        """Load a previously persisted render cache.

        Returns *None* when the file is missing, version mismatches,
        or boot fingerprint changed (boot files were modified).
        """
        import json

        path = self._checkpoints_dir / "render_cache.jsonl"
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines:
                return None
            header = json.loads(lines[0])
            if header.get("version") != 1:
                return None
            if header.get("boot_fingerprint") != boot_fingerprint:
                return None
            count = header.get("count", 0)
            rendered: list[Message] = []
            for line in lines[1 : count + 1]:
                rendered.append(Message.model_validate_json(line))
            return rendered
        except Exception:
            return None

    def clear_active_turn(self) -> None:
        """Drop any active-turn state without writing a turn summary."""
        self._active_turn = None

    def _append_event(
        self,
        *,
        kind: str,
        data: dict[str, Any],
        turn_id: str | None = None,
        request_id: str | None = None,
        client_label: str | None = None,
    ) -> None:
        self._event_seq += 1
        event = SessionDebugEvent(
            seq=self._event_seq,
            ts=tz_now(),
            session_id=self._session_id,
            turn_id=turn_id,
            request_id=request_id,
            kind=kind,  # type: ignore[arg-type]
            client_label=client_label,
            data=data,
        )
        self._append_jsonl("events.jsonl", event)

    def _append_jsonl(self, name: str, model: Any) -> None:
        path = self._session_dir / name
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(model.model_dump_json() + "\n")
            handle.flush()

    @staticmethod
    def _count_jsonl_lines(path: Path) -> int:
        if not path.exists():
            return 0
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    @staticmethod
    def _collect_tool_names(entries: Iterable[SessionEntry]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.tool_calls:
                for tool_call in entry.tool_calls:
                    if tool_call.name not in seen:
                        names.append(tool_call.name)
                        seen.add(tool_call.name)
            if entry.role == "tool" and entry.name and entry.name not in seen:
                names.append(entry.name)
                seen.add(entry.name)
        return names
