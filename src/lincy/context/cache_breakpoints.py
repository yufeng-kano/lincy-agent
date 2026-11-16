"""Prompt-cache breakpoint helpers shared by brain and subagent loops.

Breakpoint providers (Anthropic-style ``cache_control``) need:
1. a stable system-tier marker with the configured TTL
2. a conversation-tier marker advanced to the latest eligible message
   before each tool-loop request
3. for append-only sub-agent loops, one more marker on the latest tool
   result, so the growing tool output is read from cache instead of resent

OpenAI-style providers use request-level retention / cache keys instead and
should leave ``cache_control`` unset.
"""

from __future__ import annotations

import logging

from ..llm.schema import ContentPart, Message

logger = logging.getLogger(__name__)

BREAKPOINT_CACHE_PROVIDERS = frozenset(
    {"openrouter", "anthropic", "heyroute", "kano_proxy"}
)
BREAKPOINT_MAX_TTL = {
    "openrouter": "1h",
    "anthropic": "1h",
    "heyroute": "1h",
    "kano_proxy": "1h",
}
# Providers whose adapter renders a breakpoint on a tool_result block.
# Heyroute shares the adapter but is unverified, so it stays out.
TOOL_RESULT_BREAKPOINT_PROVIDERS = frozenset({"anthropic", "kano_proxy"})
# A tool-result breakpoint is read by the next request of the same loop,
# seconds later: the default 5-minute TTL avoids the 2x write price of 1h.
TOOL_RESULT_CACHE_CONTROL = {"type": "ephemeral"}
# Project-wide TTL tokens. Keep in sync with CacheConfig.ttl Literal.
ALLOWED_CACHE_TTLS = frozenset({"ephemeral", "1h", "24h"})
_TTL_ORDER = {"ephemeral": 0, "1h": 1, "24h": 2}


def _require_allowed_cache_ttl(ttl: str) -> str:
    if ttl not in ALLOWED_CACHE_TTLS:
        allowed = ", ".join(sorted(ALLOWED_CACHE_TTLS))
        raise ValueError(
            f"unsupported cache.ttl {ttl!r}; expected one of: {allowed}"
        )
    return ttl


def resolve_breakpoint_cache_ttl(
    *,
    provider: str,
    enabled: bool,
    configured_ttl: str,
) -> str | None:
    """Return clamped TTL for breakpoint providers, else None.

    Raises ValueError for unknown TTL tokens so invalid config fails early
    instead of being forwarded to the provider.
    """
    _require_allowed_cache_ttl(configured_ttl)
    if not enabled:
        return None
    if provider not in BREAKPOINT_CACHE_PROVIDERS:
        return None
    max_ttl = BREAKPOINT_MAX_TTL.get(provider, "ephemeral")
    if _TTL_ORDER[configured_ttl] > _TTL_ORDER[max_ttl]:
        logger.warning(
            "cache.ttl %r exceeds %s provider max %r, clamped",
            configured_ttl,
            provider,
            max_ttl,
        )
        return max_ttl
    return configured_ttl


def build_cache_control(ttl: str | None) -> dict[str, str] | None:
    """Build Anthropic-style cache_control from a resolved TTL."""
    if not ttl:
        return None
    _require_allowed_cache_ttl(ttl)
    ctrl: dict[str, str] = {"type": "ephemeral"}
    if ttl != "ephemeral":
        ctrl["ttl"] = ttl
    return ctrl


def extract_prompt_cache_control(messages: list[Message]) -> dict[str, str] | None:
    """Reuse the configured prompt-cache marker without changing builder defaults."""
    for message in messages:
        if message.cache_control is not None:
            return dict(message.cache_control)
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            if part.type == "text" and part.cache_control is not None:
                return dict(part.cache_control)
    return None


def message_has_text_content(message: Message) -> bool:
    """Return True when a message can carry a text cache breakpoint."""
    if isinstance(message.content, str):
        return bool(message.content)
    if isinstance(message.content, list):
        return any(part.type == "text" and part.text for part in message.content)
    return False


def clear_non_system_cache_control(
    message: Message,
    *,
    cache_control: dict[str, str],
) -> Message:
    """Remove the conversation breakpoint from non-system content blocks."""
    if message.role == "tool" and message.cache_control is not None:
        message = message.model_copy(update={"cache_control": None})
    if message.role == "system" or not isinstance(message.content, list):
        return message

    changed = False
    parts: list[ContentPart] = []
    for part in message.content:
        if part.cache_control == cache_control:
            parts.append(part.model_copy(update={"cache_control": None}))
            changed = True
        else:
            parts.append(part)
    if not changed:
        return message
    return message.model_copy(update={"content": parts})


def apply_cache_control_to_message(
    message: Message,
    *,
    cache_control: dict[str, str],
) -> Message:
    """Attach the conversation breakpoint to the latest text-bearing message."""
    if isinstance(message.content, str):
        return message.model_copy(update={
            "content": [ContentPart(
                type="text",
                text=message.content,
                cache_control=cache_control,
            )],
        })
    if isinstance(message.content, list):
        parts = list(message.content)
        for index in range(len(parts) - 1, -1, -1):
            part = parts[index]
            if part.type == "text" and part.text:
                parts[index] = part.model_copy(update={"cache_control": cache_control})
                return message.model_copy(update={"content": parts})
    return message


def advance_cache_breakpoint(
    messages: list[Message],
    *,
    include_tool_results: bool = False,
) -> list[Message]:
    """Advance the conversation-tier breakpoint for a tool-loop request.

    Leaves the caller's message list untouched: returns a request-local copy
    with the conversation marker on the latest eligible text-bearing message.

    ``include_tool_results`` adds a second, 5-minute marker on the latest
    tool result when it is newer than that message. The conversation marker
    stays where it was, so an expired or ignored tool-result marker costs
    nothing that was cached before. Only for loops whose history is
    append-only and whose provider is in ``TOOL_RESULT_BREAKPOINT_PROVIDERS``:
    a history rewritten between requests pays the write and never reads it.
    """
    cache_control = extract_prompt_cache_control(messages)
    if cache_control is None:
        return messages

    target_index: int | None = None
    tool_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "system":
            continue
        if message.role == "assistant" and message.tool_calls:
            continue
        if not message_has_text_content(message):
            continue
        if message.role == "tool":
            if include_tool_results and tool_index is None:
                tool_index = index
            continue
        target_index = index
        break

    if target_index is None and tool_index is None:
        return messages

    updated = [
        clear_non_system_cache_control(message, cache_control=cache_control)
        for message in messages
    ]
    if target_index is not None:
        updated[target_index] = apply_cache_control_to_message(
            updated[target_index],
            cache_control=cache_control,
        )
    if tool_index is not None:
        # The adapter puts a tool message's marker on its tool_result block.
        # It always follows the conversation marker, which satisfies the
        # API rule that longer TTLs come first.
        updated[tool_index] = updated[tool_index].model_copy(
            update={"cache_control": dict(TOOL_RESULT_CACHE_CONTROL)}
        )
    return updated
