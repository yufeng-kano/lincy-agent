"""Helpers for separating event time from processing time in queue turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .session.schema import SessionEntry
from .timezone_utils import format_local_stamp

TURN_PROCESSING_STARTED_AT_KEY = "turn_processing_started_at"
TURN_PROCESSING_DELAY_SECONDS_KEY = "turn_processing_delay_seconds"
TURN_PROCESSING_DELAY_REASON_KEY = "turn_processing_delay_reason"
TURN_PROCESSING_STALE_KEY = "turn_processing_stale"

TURN_DELAY_REASON_FAILED_RETRY = "failed_retry"
TURN_DELAY_REASON_SCHEDULED_TURN = "scheduled_turn"
TURN_DELAY_REASON_YIELDED_SCHEDULED_TURN = "yielded_scheduled_turn"
TURN_DELAY_REASON_QUEUE_BACKLOG = "queue_backlog"

_STALE_DELAY_THRESHOLD = timedelta(minutes=5)
_FAILED_RETRY_COUNT_KEY = "turn_failure_requeue_count"


def _coerce_non_negative_int(value: object) -> int:
    """Best-effort int coercion for timing-related metadata."""
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _parse_datetime(value: object) -> datetime | None:
    """Parse ISO datetimes from metadata when present."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_delay(delay_seconds: int) -> str:
    """Render a short human-readable delay duration."""
    seconds = max(0, int(delay_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _infer_delay_reason(
    *,
    channel: str,
    metadata: dict[str, Any],
    delay: timedelta,
) -> str | None:
    """Classify why a turn is being processed after its source event."""
    retry_count = _coerce_non_negative_int(metadata.get(_FAILED_RETRY_COUNT_KEY))
    if retry_count > 0:
        return TURN_DELAY_REASON_FAILED_RETRY

    if channel == "system" and isinstance(metadata.get("yielded_scope_id"), str):
        if delay >= _STALE_DELAY_THRESHOLD:
            return TURN_DELAY_REASON_YIELDED_SCHEDULED_TURN
        return None

    if channel == "system" and isinstance(metadata.get("scheduled_reason"), str):
        if delay >= _STALE_DELAY_THRESHOLD:
            return TURN_DELAY_REASON_SCHEDULED_TURN
        return None

    if delay >= _STALE_DELAY_THRESHOLD:
        return TURN_DELAY_REASON_QUEUE_BACKLOG

    return None


def _is_stale_turn(
    *,
    channel: str,
    metadata: dict[str, Any],
    delay: timedelta,
) -> bool:
    """Return True when a delayed turn needs stale-message safeguards."""
    retry_count = _coerce_non_negative_int(metadata.get(_FAILED_RETRY_COUNT_KEY))
    if retry_count > 0 and delay >= timedelta(minutes=1):
        return True
    if channel == "system" and isinstance(metadata.get("scheduled_reason"), str):
        return delay >= _STALE_DELAY_THRESHOLD
    return delay >= _STALE_DELAY_THRESHOLD


def build_turn_timing_metadata(
    *,
    channel: str,
    metadata: dict[str, Any],
    event_timestamp: datetime,
    processing_started_at: datetime,
) -> dict[str, Any]:
    """Annotate inbound metadata with processing-time facts for the current turn."""
    turn_metadata = dict(metadata)
    delay = processing_started_at - event_timestamp
    if delay.total_seconds() < 0:
        delay = timedelta(0)

    turn_metadata[TURN_PROCESSING_STARTED_AT_KEY] = processing_started_at.isoformat()
    turn_metadata[TURN_PROCESSING_DELAY_SECONDS_KEY] = int(delay.total_seconds())

    reason = _infer_delay_reason(
        channel=channel,
        metadata=turn_metadata,
        delay=delay,
    )
    if reason is not None:
        turn_metadata[TURN_PROCESSING_DELAY_REASON_KEY] = reason
    else:
        turn_metadata.pop(TURN_PROCESSING_DELAY_REASON_KEY, None)

    if _is_stale_turn(
        channel=channel,
        metadata=turn_metadata,
        delay=delay,
    ):
        turn_metadata[TURN_PROCESSING_STALE_KEY] = True
    else:
        turn_metadata.pop(TURN_PROCESSING_STALE_KEY, None)

    return turn_metadata


@dataclass(frozen=True)
class TurnTimingInfo:
    """Parsed timing annotations for the latest inbound turn."""

    event_timestamp: datetime | None
    processing_started_at: datetime
    delay_seconds: int
    delay_reason: str | None
    stale: bool
    retry_count: int


def parse_turn_timing_info(entry: SessionEntry) -> TurnTimingInfo | None:
    """Return timing annotations stored on a session entry, if any."""
    metadata = entry.metadata or {}
    processing_started_at = _parse_datetime(metadata.get(TURN_PROCESSING_STARTED_AT_KEY))
    if processing_started_at is None:
        return None

    delay_seconds = _coerce_non_negative_int(
        metadata.get(TURN_PROCESSING_DELAY_SECONDS_KEY)
    )
    delay_reason = metadata.get(TURN_PROCESSING_DELAY_REASON_KEY)
    if not isinstance(delay_reason, str) or not delay_reason.strip():
        delay_reason = None
    stale = bool(metadata.get(TURN_PROCESSING_STALE_KEY))
    retry_count = _coerce_non_negative_int(metadata.get(_FAILED_RETRY_COUNT_KEY))
    return TurnTimingInfo(
        event_timestamp=entry.timestamp,
        processing_started_at=processing_started_at,
        delay_seconds=delay_seconds,
        delay_reason=delay_reason,
        stale=stale,
        retry_count=retry_count,
    )


def build_turn_timing_notice(entry: SessionEntry) -> str | None:
    """Render a system notice for delayed or replayed turns."""
    info = parse_turn_timing_info(entry)
    if info is None:
        return None

    needs_notice = info.retry_count > 0 or info.delay_reason is not None or info.stale
    if not needs_notice:
        return None

    lines = [
        "[Timing Notice]",
        f"Current processing time: {format_local_stamp(info.processing_started_at)}",
    ]
    if info.event_timestamp is not None:
        lines.append(f"Original event time: {format_local_stamp(info.event_timestamp)}")
    if info.delay_seconds > 0:
        lines.append(f"Observed delay: {_format_delay(info.delay_seconds)}")

    if info.delay_reason == TURN_DELAY_REASON_FAILED_RETRY:
        lines.append("Reason: This inbound is being retried after an earlier brain failure.")
    elif info.delay_reason == TURN_DELAY_REASON_SCHEDULED_TURN:
        lines.append("Reason: This scheduled turn is being processed after its intended time.")
    elif info.delay_reason == TURN_DELAY_REASON_YIELDED_SCHEDULED_TURN:
        lines.append("Reason: This scheduled turn was yielded and is now being reevaluated.")
    elif info.delay_reason == TURN_DELAY_REASON_QUEUE_BACKLOG:
        lines.append("Reason: This turn sat in the queue before processing.")

    if info.stale:
        lines.append(
            "This turn is stale. Reevaluate all time-sensitive actions against the current processing time."
        )
        lines.append(
            "Do not send stale wake-up, sleep, meal, medication, or schedule reminder wording."
        )
    else:
        lines.append(
            "This turn is delayed. Reevaluate any time-sensitive wording against the current processing time."
        )
        lines.append(
            "Recheck wake-up, sleep, meal, medication, or schedule reminder wording against the current processing time."
        )
    return "\n".join(lines)
