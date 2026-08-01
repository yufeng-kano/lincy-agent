"""Analyze the brain agent's latest prompt composition from session debug files.

Segments the last brain-labeled request in ``requests.jsonl`` into the same
prompt regions ``ContextBuilder.build()`` produces (see
``src/lincy/context/builder.py``): system prompt, boot files, pinned context,
conversation history, and the current turn. Token counts are estimates
calibrated against the turn's real ``max_prompt_tokens`` from ``turns.jsonl``
when available.

This module is pure (no FastAPI imports) and does no caching of its own:
``requests.jsonl`` holds full message payloads and can be 10MB+, so callers
must invoke this on demand per request, never store its input in a
long-lived cache (see docs/dev/web-dashboard.md).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .session_reader import discover_sessions

# Token estimation constants. ASCII-ish text is assumed to average this many
# chars per token; CJK text is much denser and its real rate is solved from
# the turn's reported prompt tokens (see analyze_latest_brain_request). The
# fixed fallback is only used when no reported total is available to
# calibrate against.
_ASCII_CHARS_PER_TOKEN = 3.6
_FIXED_CJK_TOKENS_PER_CHAR = 1.5
_CJK_RATE_MIN = 0.5
_CJK_RATE_MAX = 3.0

# Raw JSON lines are compact (model_dump_json(), no spaces) in production but
# a plain json.dumps() (spaced) in hand-written test fixtures; check both so
# the cheap pre-filter works either way without parsing the whole line.
_BRAIN_LABEL_MARKERS = ('"client_label":"brain"', '"client_label": "brain"')

# Mirrors ContextBuilder._read_file_sections / _build_tool_boot_messages,
# which both wrap injected file content as <file path="...">...</file>.
_FILE_TAG_RE = re.compile(r'<file path="([^"]+)">\n?(.*?)\n?</file>', re.S)

# Per-turn dynamic blocks ContextBuilder.build() appends to the latest user
# message (in the order builder.py appends them), plus the common-ground
# block injected upstream in shared_state.py. Order here does not matter:
# positions are found by substring search and re-sorted by where they
# actually landed in the text.
_LATEST_TURN_MARKERS = (
    ("[Runtime Context]", "runtime_context", "[Runtime Context]"),
    ("[Timing Notice]", "timing_notice", "[Timing Notice]"),
    ("[Decision Reminder]", "decision_reminder", "[Decision Reminder]"),
    ("[Agent Notes]", "agent_notes", "[Agent Notes]"),
    ("[Common Ground at Message Time]", "common_ground", "[Common Ground at Message Time]"),
)

_TOOL_BOOT_NAME = "read_startup_context"
_PINNED_TOOL_NAME = "read_pinned_context"

# Segments with no natural per-item breakdown: the whole segment is one block.
_NO_ITEM_SEGMENTS = frozenset({"system_prompt", "history"})

_SEGMENT_ORDER = (
    "tool_definitions",
    "system_prompt",
    "boot_core_rules",
    "boot_tool_files",
    "pinned_context",
    "history",
    "current_turn",
)

_SEGMENT_LABELS = {
    "tool_definitions": "Tool definitions",
    "system_prompt": "System prompt",
    "boot_core_rules": "Core rules (boot files)",
    "boot_tool_files": "Boot tool files",
    "pinned_context": "Pinned context",
    "history": "Conversation history",
    "current_turn": "Current turn",
}


@dataclass
class _Item:
    key: str
    label: str
    cjk: int
    other: int


@dataclass
class _Segment:
    key: str
    items: list[_Item]


def _char_counts(text: str) -> tuple[int, int]:
    """Split *text* into (cjk_chars, other_chars) for token estimation."""
    cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
    return cjk, len(text) - cjk


def _msg_text(message: dict) -> str:
    """Extract the text payload of a normalized message, incl. tool calls.

    Mirrors the prompt content an LLM actually sees: string or multimodal
    list content, plus each tool_call serialized exactly as it appears on
    the wire (id/name/arguments/... all count toward prompt tokens).
    """
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict):
            parts.append(json.dumps(tool_call, ensure_ascii=False))
    return "".join(parts)


def _tool_call_names(message: dict) -> list[str]:
    return [
        tc.get("name")
        for tc in (message.get("tool_calls") or [])
        if isinstance(tc, dict)
    ]


def _residual_text(text: str, spans: list[tuple[int, int]]) -> str:
    """Return the parts of *text* not covered by any (start, end) span."""
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _classify_boot_core_rules(text: str) -> list[_Item]:
    """Split the "[Core Rules]" system message into per-file items.

    Any text not covered by a <file> block (the "[Core Rules]" header and
    the blank lines joining blocks) becomes a small call_overhead item so
    item tokens always foot exactly to the segment total.
    """
    items: list[_Item] = []
    spans: list[tuple[int, int]] = []
    for match in _FILE_TAG_RE.finditer(text):
        path = match.group(1)
        cjk, other = _char_counts(match.group(0))
        items.append(_Item(path, path, cjk, other))
        spans.append((match.start(), match.end()))
    residual = _residual_text(text, spans)
    if residual:
        cjk, other = _char_counts(residual)
        if cjk or other:
            items.append(_Item("call_overhead", "Tool call overhead", cjk, other))
    return items


def _classify_tool_boot_segment(
    messages: list[dict], start: int, tool_name: str,
) -> tuple[list[_Item] | None, int]:
    """Classify one synthetic tool-call/result block (boot files or pinned context).

    Returns (items, next_index); items is None when *start* is not the
    start of this pattern (segment genuinely absent, e.g. no pins registered).
    """
    n = len(messages)
    if start >= n or messages[start].get("role") != "assistant":
        return None, start
    call_message = messages[start]
    names = _tool_call_names(call_message)
    if not names or any(name != tool_name for name in names):
        return None, start

    # Arguments key is "file" as of builder.py's _build_tool_boot_messages /
    # _build_pinned_context_messages; accept "path" too in case that changes,
    # since the tool result content also carries the same path in its own
    # <file path="..."> wrapper regardless.
    path_by_call_id: dict[str, str] = {}
    for tool_call in call_message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        args = tool_call.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {}
        args = args or {}
        path = args.get("path") or args.get("file")
        call_id = tool_call.get("id")
        if path and call_id:
            path_by_call_id[call_id] = path

    items: list[_Item] = []
    index = start + 1
    while index < n and messages[index].get("role") == "tool":
        result_message = messages[index]
        path = path_by_call_id.get(result_message.get("tool_call_id")) or "(unknown)"
        cjk, other = _char_counts(_msg_text(result_message))
        items.append(_Item(path, path, cjk, other))
        index += 1

    # The call message's own JSON envelope (id/name/arguments/... per tool
    # call) is real prompt content but isn't attributable to any one file.
    overhead_text = _msg_text(call_message)
    if overhead_text:
        cjk, other = _char_counts(overhead_text)
        items.append(_Item("call_overhead", "Tool call overhead", cjk, other))

    return items, index


def _classify_current_turn_user_message(message: dict) -> list[_Item]:
    """Split the latest user message into its base text and dynamic blocks."""
    text = _msg_text(message)
    positions = sorted(
        (text.find(marker), key, label)
        for marker, key, label in _LATEST_TURN_MARKERS
        if text.find(marker) >= 0
    )
    base_end = positions[0][0] if positions else len(text)

    items: list[_Item] = []
    cjk, other = _char_counts(text[:base_end])
    if cjk or other:
        items.append(_Item("user_message", "User message", cjk, other))

    for i, (pos, key, label) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        cjk, other = _char_counts(text[pos:end])
        items.append(_Item(key, label, cjk, other))

    return items


def _classify(messages: list[dict], tools: list) -> dict[str, _Segment]:
    """Walk the brain message list once, in prompt order, mirroring builder.build().

    Positions before the conversation proper are a fixed sequence (system
    prompt, then "[Core Rules]", then boot-tool-file pair, then pinned-context
    pair); each step only consumes messages when its pattern actually matches,
    so a genuinely absent segment (e.g. no pinned files) is simply skipped.
    """
    segments: dict[str, _Segment] = {}
    n = len(messages)
    i = 0

    if i < n and messages[i].get("role") == "system":
        text = _msg_text(messages[i])
        if not text.startswith("[Core Rules]"):
            cjk, other = _char_counts(text)
            segments["system_prompt"] = _Segment("system_prompt", [_Item("_direct", "", cjk, other)])
            i += 1

    if i < n and messages[i].get("role") == "system":
        text = _msg_text(messages[i])
        if text.startswith("[Core Rules]"):
            items = _classify_boot_core_rules(text)
            if items:
                segments["boot_core_rules"] = _Segment("boot_core_rules", items)
            i += 1

    items, i = _classify_tool_boot_segment(messages, i, _TOOL_BOOT_NAME)
    if items:
        segments["boot_tool_files"] = _Segment("boot_tool_files", items)

    items, i = _classify_tool_boot_segment(messages, i, _PINNED_TOOL_NAME)
    if items:
        segments["pinned_context"] = _Segment("pinned_context", items)

    last_user_idx = None
    for idx in range(i, n):
        if messages[idx].get("role") == "user":
            last_user_idx = idx

    if last_user_idx is not None:
        if last_user_idx > i:
            cjk_total = other_total = 0
            for idx in range(i, last_user_idx):
                cjk, other = _char_counts(_msg_text(messages[idx]))
                cjk_total += cjk
                other_total += other
            segments["history"] = _Segment("history", [_Item("_direct", "", cjk_total, other_total)])

        current_turn_items = _classify_current_turn_user_message(messages[last_user_idx])

        if last_user_idx + 1 < n:
            cjk_total = other_total = 0
            for idx in range(last_user_idx + 1, n):
                cjk, other = _char_counts(_msg_text(messages[idx]))
                cjk_total += cjk
                other_total += other
            current_turn_items.append(_Item("tool_loop", "Tool loop (this turn)", cjk_total, other_total))

        if current_turn_items:
            segments["current_turn"] = _Segment("current_turn", current_turn_items)
    elif i < n:
        # No user message at all past the boot messages: treat the rest as
        # history rather than dropping it silently (defensive; real brain
        # requests always have a triggering user message).
        cjk_total = other_total = 0
        for idx in range(i, n):
            cjk, other = _char_counts(_msg_text(messages[idx]))
            cjk_total += cjk
            other_total += other
        segments["history"] = _Segment("history", [_Item("_direct", "", cjk_total, other_total)])

    if tools:
        text = json.dumps(tools, ensure_ascii=False)
        cjk, other = _char_counts(text)
        segments["tool_definitions"] = _Segment(
            "tool_definitions",
            [_Item("tool_schemas", f"{len(tools)} tool schemas", cjk, other)],
        )

    return segments


def _apportion(raw_values: list[float], target_total: int) -> list[int]:
    """Round non-negative floats to ints that sum exactly to *target_total*.

    Largest-remainder method. Independent per-value rounding can drift a
    token or two off target when summed; the UI foots segment/item rows
    against each other and against the reported total, so a table where
    the rows don't add up would look broken.
    """
    n = len(raw_values)
    if n == 0:
        return []
    floors = [int(v) for v in raw_values]
    fracs = [v - f for v, f in zip(raw_values, floors)]
    remainder = target_total - sum(floors)
    order = sorted(range(n), key=lambda idx: fracs[idx], reverse=remainder >= 0)
    step = 1 if remainder >= 0 else -1
    result = list(floors)
    for k in range(abs(remainder)):
        result[order[k % n]] += step
    return result


def _last_brain_request(requests_path: Path) -> dict | None:
    """Stream requests.jsonl and return the last brain-labeled request line.

    Lines carry the full message payload and can be 10MB+, so this avoids
    json.loads on lines we don't care about (other client labels, e.g.
    worker-N): a cheap substring check finds candidates and only the last
    one is actually parsed.
    """
    if not requests_path.exists():
        return None
    last_raw: str | None = None
    with open(requests_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if any(marker in line for marker in _BRAIN_LABEL_MARKERS):
                last_raw = line
    if last_raw is None:
        return None
    try:
        return json.loads(last_raw)
    except json.JSONDecodeError:
        return None


def _find_latest_brain_request(sessions_dir: Path) -> tuple[str | None, dict | None]:
    """Return (session_id, request) for the newest session with a brain request."""
    for session_id in reversed(discover_sessions(sessions_dir)):
        request = _last_brain_request(sessions_dir / session_id / "requests.jsonl")
        if request is not None:
            return session_id, request
    return None, None


def _find_turn_max_prompt_tokens(turns_path: Path, turn_id: str) -> int | None:
    """Look up the reported max_prompt_tokens for one turn from turns.jsonl."""
    if not turns_path.exists():
        return None
    with open(turns_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if turn_id not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("turn_id") != turn_id:
                continue
            tokens = record.get("max_prompt_tokens")
            if isinstance(tokens, int):
                return tokens
    return None


def _raw_tokens(item: _Item, cjk_rate: float) -> float:
    return item.other / _ASCII_CHARS_PER_TOKEN + item.cjk * cjk_rate


def analyze_latest_brain_request(sessions_dir: Path, soft_max_prompt_tokens: int) -> dict:
    """Segment the newest brain request's prompt and estimate its token composition.

    Reads requests.jsonl / turns.jsonl directly (never cached: see module
    docstring). Returns a JSON-safe dict; on any expected failure mode
    (no sessions, no brain request) returns {"available": False, "reason": ...}
    instead of raising, matching the /api/claude-accounts convention.
    """
    session_id, request = _find_latest_brain_request(sessions_dir)
    if session_id is None or request is None:
        return {"available": False, "reason": "No brain request found in any session."}

    messages = request.get("messages") or []
    tools = request.get("tools") or []
    turn_id = request.get("turn_id")

    segments_by_key = _classify(messages, tools)
    ordered_keys = [key for key in _SEGMENT_ORDER if key in segments_by_key]
    if not ordered_keys:
        return {"available": False, "reason": "Latest brain request has no classifiable content."}

    reported = None
    if turn_id:
        reported = _find_turn_max_prompt_tokens(sessions_dir / session_id / "turns.jsonl", turn_id)

    total_cjk = sum(
        item.cjk for key in ordered_keys for item in segments_by_key[key].items
    )
    total_other = sum(
        item.other for key in ordered_keys for item in segments_by_key[key].items
    )

    calibrated = False
    cjk_rate = _FIXED_CJK_TOKENS_PER_CHAR
    if reported is not None:
        if total_cjk > 0:
            solved = (reported - total_other / _ASCII_CHARS_PER_TOKEN) / total_cjk
            if _CJK_RATE_MIN <= solved <= _CJK_RATE_MAX:
                cjk_rate = solved
                calibrated = True
        else:
            # No CJK text at all: the ASCII-only estimate is trusted directly
            # (there's no rate to validate), and the apportion step below
            # still forces the total to foot exactly to the reported figure.
            calibrated = True

    segment_raw = [
        sum(_raw_tokens(item, cjk_rate) for item in segments_by_key[key].items)
        for key in ordered_keys
    ]
    segment_tokens = (
        _apportion(segment_raw, reported)
        if calibrated
        else [round(v) for v in segment_raw]
    )

    segments_out = []
    for key, seg_total in zip(ordered_keys, segment_tokens):
        segment = segments_by_key[key]
        if key in _NO_ITEM_SEGMENTS:
            segments_out.append(
                {"key": key, "label": _SEGMENT_LABELS[key], "tokens": seg_total, "items": []}
            )
            continue
        item_raw = [_raw_tokens(item, cjk_rate) for item in segment.items]
        item_tokens = _apportion(item_raw, seg_total)
        segments_out.append({
            "key": key,
            "label": _SEGMENT_LABELS[key],
            "tokens": seg_total,
            "items": [
                {"key": item.key, "label": item.label, "tokens": tokens}
                for item, tokens in zip(segment.items, item_tokens)
            ],
        })

    return {
        "available": True,
        "session_id": session_id,
        "turn_id": turn_id,
        "request_id": request.get("request_id"),
        "request_ts": request.get("ts"),
        "round": request.get("round"),
        "reported_prompt_tokens": reported,
        "calibrated": calibrated,
        "total_tokens": sum(segment_tokens),
        "soft_max_prompt_tokens": soft_max_prompt_tokens,
        "message_count": len(messages),
        "tool_count": len(tools),
        "segments": segments_out,
    }
