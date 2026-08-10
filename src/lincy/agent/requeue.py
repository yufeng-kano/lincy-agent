"""Queue retry and scheduled-turn requeue policy."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

import httpx
from pydantic import ValidationError

from ..core.schema import is_in_quiet_hours
from ..llm.http_error import classify_http_status_error
from ..llm.schema import ContextLengthExceededError, MalformedFunctionCallError
from ..timezone_utils import get_tz, now as tz_now
from .schema import InboundMessage

TurnFailureCategory = Literal[
    "request-format", "provider-api", "transport", "provider-response",
    "context-length", "other",
]
TURN_FAILURE_REQUEUE_COUNT_KEY = "turn_failure_requeue_count"
TURN_FAILURE_FIRST_FAILED_AT_KEY = "turn_failure_first_failed_at"
PROACTIVE_YIELD_REEVALUATE_DELAY = timedelta(minutes=2)


def classify_turn_failure(error: Exception) -> TurnFailureCategory:
    if isinstance(error, ContextLengthExceededError):
        return "context-length"
    if isinstance(error, (httpx.TimeoutException, TimeoutError, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return "transport"
    if isinstance(error, (MalformedFunctionCallError, ValidationError)):
        return "provider-response"
    if isinstance(error, httpx.HTTPStatusError):
        category = classify_http_status_error(error)
        if category in {"request-format", "provider-api"}:
            return category
        status = error.response.status_code if error.response is not None else None
        return "transport" if status in {429, 500, 502, 503, 504, 529} else "provider-api"
    return "other"


def should_requeue_failed_turn(category: TurnFailureCategory | None, *, requeue_non_retryable: bool = False) -> bool:
    if category in {None, "transport", "provider-response"}:
        return True
    return requeue_non_retryable and category in {"request-format", "provider-api", "context-length", "other"}


def coerce_non_negative_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default


class RequeuePolicy:
    """Apply requeue decisions against an AgentCore-like runtime."""

    def __init__(self, core) -> None:
        self._core = core

    def failed_inbound_retry_config(self) -> tuple[int, int, bool]:
        app_cfg = getattr(self._core.config, "app", None)
        if app_cfg is None:
            return 0, 0, False
        return (
            coerce_non_negative_int(getattr(app_cfg, "turn_failure_requeue_limit", 0), 0),
            coerce_non_negative_int(getattr(app_cfg, "turn_failure_requeue_delay_seconds", 0), 0),
            bool(getattr(app_cfg, "requeue_non_retryable_turn_failures", False)),
        )

    def requeue_failed_inbound(self, msg: InboundMessage, receipt) -> bool:
        queue = self._core._queue
        if queue is None:
            return False
        limit, delay, _ = self.failed_inbound_retry_config()
        retries = coerce_non_negative_int(msg.metadata.get(TURN_FAILURE_REQUEUE_COUNT_KEY), 0)
        if retries >= limit:
            return False
        retry_at = tz_now() + timedelta(seconds=delay * (retries + 1))
        retry = InboundMessage(
            channel=msg.channel, content=msg.content, priority=msg.priority, sender=msg.sender,
            metadata=dict(msg.metadata), timestamp=msg.timestamp, not_before=retry_at,
        )
        retry.metadata[TURN_FAILURE_REQUEUE_COUNT_KEY] = retries + 1
        retry.metadata.setdefault(TURN_FAILURE_FIRST_FAILED_AT_KEY, tz_now().isoformat())
        if receipt is None:
            queue.put(retry)
        else:
            queue.requeue_active(receipt, retry)
        self._core.console.print_warning(
            f"Brain turn failed; re-enqueued inbound retry {retries + 1}/{limit} at {retry_at.isoformat()}."
        )
        return True

    def requeue_yielded_scheduled_turn(self, msg: InboundMessage, receipt, *, scope_id: str) -> bool:
        queue = self._core._queue
        reason = msg.metadata.get("scheduled_reason")
        if queue is None or not isinstance(reason, str) or not reason.strip():
            return False
        reeval_at = tz_now() + PROACTIVE_YIELD_REEVALUATE_DELAY
        metadata = dict(msg.metadata)
        metadata["yielded_scope_id"] = scope_id
        count = metadata.get("yield_reschedule_count", 0)
        metadata["yield_reschedule_count"] = count + 1 if isinstance(count, int) and count >= 0 else 1
        display = reeval_at.astimezone(get_tz()).strftime("%Y-%m-%d %H:%M")
        retry = InboundMessage(
            channel="system",
            content=("[SCHEDULED]\n" f"Reason: {reason}\n" f"Scheduled at: {display}\n\n" "A newer inbound for the same conversation arrived before delivery. Reevaluate whether action is still needed."),
            priority=msg.priority, sender="system", metadata=metadata,
            timestamp=reeval_at, not_before=reeval_at,
        )
        if receipt is None:
            queue.put(retry)
        else:
            queue.requeue_active(receipt, retry)
        return True
