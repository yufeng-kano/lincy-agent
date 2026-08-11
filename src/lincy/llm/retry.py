"""Retry wrapper for transient LLM client failures."""

from typing import Any, Callable, TypeVar
import logging
import random
import time

import httpx
from pydantic import ValidationError

from .base import LLMClient
from .http_error import (
    classify_http_status_error,
    is_rate_limit_error,
    is_transient_error,
    parse_retry_after_seconds,
)
from .schema import LLMResponse, Message, ToolDefinition

T = TypeVar("T")
_429_BACKOFF_SCHEDULE = (5.0, 10.0, 20.0, 30.0, 30.0)
_TRANSIENT_BACKOFF_SCHEDULE = (0.5, 1.0, 5.0, 15.0, 30.0, 60.0)

logger = logging.getLogger(__name__)


class RetryingLLMClient:
    """Wrap an LLM client and retry transient errors."""

    def __init__(
        self,
        client: LLMClient,
        transient_retries: int,
        rate_limit_retries: int = 0,
        *,
        label: str | None = None,
    ):
        self._client = client
        self._transient_retries = max(0, transient_retries)
        self._rate_limit_retries = max(0, rate_limit_retries)
        self._label = (label or "").strip()

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        return self._run_with_retry(
            lambda: self._client.chat(messages, response_schema=response_schema, temperature=temperature)
        )

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        return self._run_with_retry(
            lambda: self._client.chat_with_tools(messages, tools, temperature=temperature)
        )

    def _run_with_retry(self, fn: Callable[[], T]) -> T:
        transient_attempts = 0
        rate_limit_attempts = 0
        while True:
            try:
                return fn()
            except Exception as exc:
                if _is_429_error(exc):
                    if rate_limit_attempts >= self._rate_limit_retries:
                        raise
                    sleep_secs = _429_sleep_seconds(exc, rate_limit_attempts)
                    rate_limit_attempts += 1
                    logger.debug(
                        "%s429 retry %d/%d, sleeping %.1fs",
                        _retry_log_prefix(self._label),
                        rate_limit_attempts,
                        self._rate_limit_retries,
                        sleep_secs,
                    )
                    time.sleep(sleep_secs)
                    continue

                if _is_retryable_exception(exc):
                    if transient_attempts >= self._transient_retries:
                        raise
                    transient_attempts += 1
                    sleep_secs = _transient_sleep_seconds(exc, transient_attempts - 1)
                    logger.debug(
                        "%stransient retry %d/%d after %s, sleeping %.1fs",
                        _retry_log_prefix(self._label),
                        transient_attempts,
                        self._transient_retries,
                        _retry_reason(exc),
                        sleep_secs,
                    )
                    if sleep_secs > 0:
                        time.sleep(sleep_secs)
                    continue

                raise


def with_llm_retry(
    client: LLMClient,
    transient_retries: int,
    rate_limit_retries: int = 0,
    *,
    label: str | None = None,
) -> LLMClient:
    """Return a client wrapped with transient + rate-limit retry behavior."""
    if transient_retries <= 0 and rate_limit_retries <= 0:
        return client
    return RetryingLLMClient(
        client,
        transient_retries,
        rate_limit_retries,
        label=label,
    )


def _is_429_error(exc: Exception) -> bool:
    """Return True if the exception is an HTTP 429 rate limit error."""
    return is_rate_limit_error(exc)


def _is_retryable_exception(exc: Exception) -> bool:
    """Return True for transient failures handled by retry policy."""
    return is_transient_error(exc, include_parse_errors=True) or isinstance(exc, ValidationError)


_parse_retry_after_seconds = parse_retry_after_seconds


def _429_sleep_seconds(exc: Exception, attempt: int) -> float:
    """Compute sleep seconds for a 429 error using fixed backoff schedule."""
    schedule_secs = _429_BACKOFF_SCHEDULE[min(attempt, len(_429_BACKOFF_SCHEDULE) - 1)]

    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        retry_after = _parse_retry_after_seconds(
            exc.response.headers.get("Retry-After")
        )
        if retry_after is not None:
            return max(retry_after, schedule_secs)

    return schedule_secs


def _transient_sleep_seconds(exc: Exception, attempt: int) -> float:
    """Compute retry wait seconds for retryable non-429 errors.

    Uses a bounded jitter between the previous and current schedule bucket
    to avoid synchronized retries while keeping interactive latency bounded.
    """
    idx = min(attempt, len(_TRANSIENT_BACKOFF_SCHEDULE) - 1)
    schedule_secs = _TRANSIENT_BACKOFF_SCHEDULE[idx]

    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        retry_after = _parse_retry_after_seconds(
            exc.response.headers.get("Retry-After")
        )
        if retry_after is not None:
            return max(retry_after, schedule_secs)

    if idx == 0:
        return schedule_secs

    prev_schedule_secs = _TRANSIENT_BACKOFF_SCHEDULE[idx - 1]
    if schedule_secs <= prev_schedule_secs:
        return schedule_secs
    return random.uniform(prev_schedule_secs, schedule_secs)


def _retry_reason(exc: Exception) -> str:
    """Human-readable short label for retry logs."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else "unknown"
        category = classify_http_status_error(exc)
        if category:
            return f"http {status} ({category})"
        return f"http {status}"
    return exc.__class__.__name__


def _retry_log_prefix(label: str) -> str:
    """Prefix retry log lines with the client label when available."""
    if not label:
        return ""
    return f"[{label}] "
