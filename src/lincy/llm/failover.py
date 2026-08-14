"""Agent-level LLM failover across multiple provider clients."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import re
import threading
import time
from typing import Any, Iterator, Sequence, TypeVar

import httpx

from .base import LLMClient
from .http_error import (
    classify_http_status_error,
    extract_http_status_error_detail,
    is_transient_error,
    parse_retry_after_seconds,
)
from .schema import ContentPart, LLMResponse, Message, ToolDefinition

T = TypeVar("T")

logger = logging.getLogger(__name__)

_FAILOVER_ERROR_PATTERNS = (
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"capacity", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"requires? .{0,40}subscription", re.IGNORECASE),
    re.compile(r"upgrade .{0,40}access", re.IGNORECASE),
)


@dataclass(frozen=True)
class FailoverCandidate:
    """One concrete client in a fallback chain."""

    key: str
    label: str
    client: LLMClient
    supports_vision: bool = True
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ServedCandidate:
    """Which candidate of a fallback chain actually handled one call."""

    provider: str | None
    model: str | None
    label: str
    index: int

    @property
    def is_fallback(self) -> bool:
        return self.index > 0


# Telemetry needs the winning candidate, but the failover client sits *below*
# the session debug wrapper and cannot reach it through the return value.
_SERVED_CANDIDATE: ContextVar[ServedCandidate | None] = ContextVar(
    "llm_served_candidate",
    default=None,
)


class ServedCandidateProbe:
    """Read the candidate that served the call running in this scope."""

    def get(self) -> ServedCandidate | None:
        return _SERVED_CANDIDATE.get()


@contextmanager
def observe_served_candidate() -> Iterator[ServedCandidateProbe]:
    """Scope one LLM call so its serving candidate can be read afterwards.

    Yields a probe whose ``get()`` returns the candidate that handled the call
    (or produced the raised error), and ``None`` when the client is not a
    failover chain.
    """

    token = _SERVED_CANDIDATE.set(None)
    try:
        yield ServedCandidateProbe()
    finally:
        _SERVED_CANDIDATE.reset(token)


class _CooldownRegistry:
    """Process-local cooldowns shared by all failover wrappers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deadlines: dict[str, float] = {}

    def deadline(self, key: str) -> float | None:
        now = time.monotonic()
        with self._lock:
            deadline = self._deadlines.get(key)
            if deadline is not None and deadline <= now:
                self._deadlines.pop(key, None)
                return None
            return deadline

    def mark(self, key: str, cooldown_seconds: float) -> None:
        if cooldown_seconds <= 0:
            return
        deadline = time.monotonic() + cooldown_seconds
        with self._lock:
            current = self._deadlines.get(key)
            if current is None or deadline > current:
                self._deadlines[key] = deadline

    def clear(self) -> None:
        with self._lock:
            self._deadlines.clear()


_COOLDOWNS = _CooldownRegistry()


def reset_failover_cooldowns() -> None:
    """Clear shared failover cooldowns (tests / debugging)."""

    _COOLDOWNS.clear()


def order_values_by_cooldown(items: Sequence[tuple[str, T]]) -> list[T]:
    """Order values by shared failover cooldowns (ready first, then soonest)."""

    ready: list[T] = []
    cooling: list[tuple[float, T]] = []
    for key, value in items:
        deadline = _COOLDOWNS.deadline(key)
        if deadline is None:
            ready.append(value)
            continue
        cooling.append((deadline, value))
    cooling.sort(key=lambda item: item[0])
    return ready + [value for _deadline, value in cooling]


def preferred_candidate_supports_vision(
    chain: Sequence[tuple[str, bool]],
) -> bool:
    """Return whether the currently preferred failover candidate supports vision."""

    ordered = order_values_by_cooldown(chain)
    return bool(ordered[0]) if ordered else False


def messages_contain_images(messages: Sequence[Message]) -> bool:
    """Return True when any message content includes an image part."""

    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, ContentPart) and part.type == "image":
                return True
            if getattr(part, "type", None) == "image":
                return True
    return False


def llm_failover_key(config: Any) -> str:
    """Return a stable provider/account-level cooldown key.

    Rate limits usually apply to one credential / endpoint bucket, not to one
    model name. Different models on the same Claude Code/OpenRouter account
    should therefore share the same cooldown.
    """

    payload = {
        "provider": getattr(config, "provider", None),
        "base_url": getattr(config, "base_url", None),
        "api_key": getattr(config, "api_key", None),
        "api_key_env": getattr(config, "api_key_env", None),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


_extract_http_error_detail = extract_http_status_error_detail
_parse_retry_after_seconds = parse_retry_after_seconds


def _failover_cooldown_seconds(
    exc: Exception,
    default_cooldown_seconds: int,
) -> float:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        retry_after = _parse_retry_after_seconds(
            exc.response.headers.get("Retry-After")
        )
        if retry_after is not None:
            return max(float(default_cooldown_seconds), retry_after)
    return float(default_cooldown_seconds)


def _should_failover(exc: Exception) -> bool:
    if is_transient_error(exc) or (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response is not None
        and exc.response.status_code == 429
    ):
        return True
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if classify_http_status_error(exc) != "provider-api":
        return False
    # The shared extractor prefers message/detail before error.code, so
    # availability phrases remain matchable when both are present.
    return any(pattern.search(_extract_http_error_detail(exc)) for pattern in _FAILOVER_ERROR_PATTERNS)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else "unknown"
        detail = _extract_http_error_detail(exc)
        return f"http {status}" + (f" ({detail})" if detail else "")
    return exc.__class__.__name__


class FailoverLLMClient:
    """Wrap multiple clients and fail over on quota / availability failures."""

    def __init__(
        self,
        candidates: list[FailoverCandidate],
        *,
        cooldown_seconds: int,
        label: str | None = None,
    ) -> None:
        self._candidates = tuple(candidates)
        self._cooldown_seconds = max(0, cooldown_seconds)
        self._label = (label or "").strip()

    def chat(
        self,
        messages: list[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        def _invoke(client: LLMClient) -> str:
            return client.chat(
                messages,
                response_schema=response_schema,
                temperature=temperature,
            )

        return self._run_with_failover(_invoke, messages)

    def chat_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        temperature: float | None = None,
    ) -> LLMResponse:
        def _invoke(client: LLMClient) -> LLMResponse:
            return client.chat_with_tools(
                messages,
                tools,
                temperature=temperature,
            )

        return self._run_with_failover(_invoke, messages)

    def _run_with_failover(self, invoke, messages: list[Message]):
        candidates = self._ordered_candidates()
        last_error: Exception | None = None
        has_images = messages_contain_images(messages)
        attempted = False

        for index, candidate in enumerate(candidates):
            if has_images and not candidate.supports_vision:
                logger.warning(
                    "%sSkipping %s: model lacks vision but prompt contains images",
                    _log_prefix(self._label),
                    candidate.label,
                )
                continue

            attempted = True
            try:
                result = invoke(candidate.client)
            except Exception as exc:
                last_error = exc
                _SERVED_CANDIDATE.set(self._served(candidate))
                if not _should_failover(exc):
                    raise

                cooldown = _failover_cooldown_seconds(
                    exc,
                    self._cooldown_seconds,
                )
                _COOLDOWNS.mark(candidate.key, cooldown)
                remaining = any(
                    later.supports_vision or not has_images
                    for later in candidates[index + 1 :]
                )
                if not remaining:
                    raise

                logger.warning(
                    "%sFailing over from %s after %s; cooling down for %.0fs",
                    _log_prefix(self._label),
                    candidate.label,
                    _format_error(exc),
                    cooldown,
                )
            else:
                _SERVED_CANDIDATE.set(self._served(candidate))
                return result

        if last_error is not None:
            raise last_error
        if has_images and not attempted:
            raise RuntimeError(
                "No vision-capable LLM available for a prompt that contains images"
            )
        raise RuntimeError("Failover client has no candidates")

    def _served(self, candidate: FailoverCandidate) -> ServedCandidate:
        # Index in the *configured* chain, not in the cooldown-ordered attempt
        # list, so 0 always means "the primary profile served this request".
        return ServedCandidate(
            provider=candidate.provider,
            model=candidate.model,
            label=candidate.label,
            index=self._candidates.index(candidate),
        )

    def _ordered_candidates(self) -> list[FailoverCandidate]:
        return order_values_by_cooldown(
            [(candidate.key, candidate) for candidate in self._candidates]
        )


def with_llm_failover(
    candidates: list[FailoverCandidate],
    *,
    cooldown_seconds: int,
    label: str | None = None,
) -> LLMClient:
    """Wrap a list of clients with generic quota/availability failover."""

    if len(candidates) <= 1:
        return candidates[0].client
    return FailoverLLMClient(
        candidates,
        cooldown_seconds=cooldown_seconds,
        label=label,
    )


def _log_prefix(label: str) -> str:
    if not label:
        return ""
    return f"[{label}] "
