"""HTTP error classification helpers for LLM provider calls."""

from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import re

import httpx

from ..timezone_utils import now as tz_now
from .schema import MalformedFunctionCallError

_REQUEST_FORMAT_PATTERNS = (
    re.compile(r"missing a thought_signature", re.IGNORECASE),
    re.compile(r"function call is missing", re.IGNORECASE),
    re.compile(r"\bmissing\b.*\b(function|field|parameter|argument|part)\b", re.IGNORECASE),
    re.compile(r"\binvalid\b.*\b(function|field|parameter|argument|payload|history|part)\b", re.IGNORECASE),
    re.compile(r"\bmalformed\b", re.IGNORECASE),
    re.compile(r"\bunexpected\b.*\bfield\b", re.IGNORECASE),
)
_TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504, 529})
_RATE_LIMIT_STATUS_CODE = 429
_TRANSPORT_EXCEPTIONS = (
    httpx.TimeoutException,
    TimeoutError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def extract_http_error_detail(text: str) -> str:
    """Extract a short textual detail from a raw HTTP response body."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return " ".join(cleaned.split())
    return _extract_error_detail_from_payload(payload) or " ".join(cleaned.split())


def _extract_error_detail_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    # Prefer human-readable top-level detail over nested error codes.
    for key in ("message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def extract_http_status_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Extract normalized error detail using the shared HTTP precedence."""
    return extract_http_error_detail(exc.response.text) if exc.response is not None else ""


def parse_retry_after_seconds(raw: str | None) -> float | None:
    """Parse a Retry-After header value to a non-negative delay."""
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - tz_now()).total_seconds())


def is_rate_limit_error(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code == _RATE_LIMIT_STATUS_CODE


def is_transient_error(exc: Exception, *, include_parse_errors: bool = False) -> bool:
    """Classify retryable transport and availability failures."""
    if isinstance(exc, _TRANSPORT_EXCEPTIONS):
        return True
    if include_parse_errors and isinstance(exc, MalformedFunctionCallError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response is not None and exc.response.status_code in _TRANSIENT_STATUS_CODES


def format_http_error(status: int | str, body: str = "") -> str:
    detail = extract_http_error_detail(body)
    return f"HTTP {status}: {detail}" if detail else f"HTTP {status}"


def classify_http_status_error(exc: httpx.HTTPStatusError) -> str | None:
    if exc.response is None:
        return None
    status = exc.response.status_code
    if status == 400:
        return "request-format" if any(pattern.search(extract_http_status_error_detail(exc)) for pattern in _REQUEST_FORMAT_PATTERNS) else "provider-api"
    if status in {401, 403, 404, 409, 422}:
        return "provider-api"
    return None


def format_http_status_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code if exc.response is not None else "unknown"
    category = classify_http_status_error(exc)
    detail = extract_http_status_error_detail(exc)
    prefix = f"HTTP {status}" + (f" ({category})" if category else "")
    return f"{prefix}: {detail}" if detail else prefix
