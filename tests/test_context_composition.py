"""Tests for chat_web_api.context_composition (pure prompt-segmentation logic)."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from chat_web_api.context_composition import analyze_latest_brain_request

SESSION_ID = "20260801_120000_abcdef"
TURN_ID = "turn_000001"
REQUEST_ID = "req_000002"

_EXPECTED_SEGMENT_ORDER = [
    "tool_definitions",
    "system_prompt",
    "boot_core_rules",
    "boot_tool_files",
    "pinned_context",
    "history",
    "current_turn",
]


def _build_messages() -> list[dict]:
    """One synthetic brain request mirroring ContextBuilder.build()'s layout."""
    messages: list[dict] = []

    # System prompt (messages[0]).
    messages.append({
        "role": "system",
        "content": (
            "You are Lincy, a private life-agent. "
            "請使用繁體中文回覆所有與記憶相關的操作，並保持簡潔直接的語氣。"
        ),
    })

    # "[Core Rules]" boot files, embedded inline as <file> blocks.
    messages.append({
        "role": "system",
        "content": (
            "[Core Rules]\n\n"
            '<file path="memory/agent/persona.md">\n'
            "# Persona\n人設：假閨蜜，說話直接、不拐彎抹角。永遠使用繁體中文回覆。\n"
            "</file>\n\n"
            '<file path="memory/agent/rules.md">\n'
            "# Rules\nAlways double-check scheduling conflicts before confirming a reminder.\n"
            "</file>"
        ),
    })

    # read_startup_context: synthetic tool-call + paired tool-result messages.
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "boot_ctx_0_0",
                "name": "read_startup_context",
                "arguments": {"file": "memory/agent/index.md"},
                "thought_signature": None,
                "provider_call_index": None,
                "provider_roundtrip": None,
            },
            {
                "id": "boot_ctx_0_1",
                "name": "read_startup_context",
                "arguments": {"file": "memory/agent/temp-memory.md"},
                "thought_signature": None,
                "provider_call_index": None,
                "provider_roundtrip": None,
            },
        ],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "boot_ctx_0_0",
        "name": "read_startup_context",
        "content": '<file path="memory/agent/index.md">\n# Agent 記憶索引\n- persona\n- rules\n</file>',
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "boot_ctx_0_1",
        "name": "read_startup_context",
        "content": (
            '<file path="memory/agent/temp-memory.md">\n'
            "# 暫存記憶\n只放近期工作上下文，超過一週自動歸檔。今天要記得跟毓峰確認下週行程安排。\n"
            "</file>"
        ),
    })

    # read_pinned_context: same synthetic pattern.
    messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "pinned_ctx_0_0",
                "name": "read_pinned_context",
                "arguments": {"file": "memory/people/yufeng/basic-info.md"},
                "thought_signature": None,
                "provider_call_index": None,
                "provider_roundtrip": None,
            },
            {
                "id": "pinned_ctx_0_1",
                "name": "read_pinned_context",
                "arguments": {"file": "memory/agent/mine.md"},
                "thought_signature": None,
                "provider_call_index": None,
                "provider_roundtrip": None,
            },
        ],
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "pinned_ctx_0_0",
        "name": "read_pinned_context",
        "content": '<file path="memory/people/yufeng/basic-info.md">\n# 毓峰基本資料\n生日、聯絡方式等基本資訊。\n</file>',
    })
    messages.append({
        "role": "tool",
        "tool_call_id": "pinned_ctx_0_1",
        "name": "read_pinned_context",
        "content": '<file path="memory/agent/mine.md">\n# 澪希的自由筆記\n這是我自己的觀察與想法紀錄。\n</file>',
    })

    # Conversation history: two turns before the current one.
    messages.append({
        "role": "user",
        "content": "[2026-07-31 (Fri) 20:00] [discord, from 毓峰] 晚安，先睡了",
    })
    messages.append({"role": "assistant", "content": "好，晚安，記得吃藥。"})

    # Latest user message: base text + [Runtime Context] + [Agent Notes].
    messages.append({
        "role": "user",
        "content": (
            "[2026-08-01 (Sat) 09:00] [discord, from 毓峰] 早安，今天天氣如何？\n\n"
            "[Runtime Context]\n"
            "current_local_time: 2026-08-01 (Sat) 09:00\n"
            "agent_os_dir: /Users/yufeng/AgentOS\n\n"
            "[Agent Notes]\n"
            'bedtime_med: "已服藥" | updated_at 2026-08-01 08:30'
        ),
    })

    # One message after the latest user message: this turn's tool loop.
    messages.append({"role": "assistant", "content": "早安！今天台北多雲，氣溫約28度。"})

    return messages


def _build_tools() -> list[dict]:
    return [
        {
            "name": "send_message",
            "description": "Send a message to the user.",
            "parameters": {"content": {"type": "string", "description": "Message body"}},
            "required": ["content"],
        },
        {
            "name": "agent_note",
            "description": "Record a short-lived note about the user's state.",
            "parameters": {"key": {"type": "string", "description": "Note key"}},
            "required": ["key"],
        },
        {
            "name": "memory_edit",
            "description": "Edit a long-term memory file.",
            "parameters": {"instruction": {"type": "string", "description": "Edit instruction"}},
            "required": ["instruction"],
        },
    ]


def _fixture_char_totals(messages: list[dict], tools: list[dict]) -> tuple[int, int]:
    """Independently re-derive (cjk_chars, other_chars) for the whole request.

    Deliberately not imported from context_composition: this only exists to
    pick a self-consistent max_prompt_tokens fixture value for turns.jsonl,
    not to assert anything about the module under test.
    """

    def counts(text: str) -> tuple[int, int]:
        cjk = sum(1 for ch in text if unicodedata.east_asian_width(ch) in ("W", "F"))
        return cjk, len(text) - cjk

    total_cjk = total_other = 0
    for message in messages:
        text = ""
        content = message.get("content")
        if isinstance(content, str):
            text = content
        for tool_call in message.get("tool_calls") or []:
            text += json.dumps(tool_call, ensure_ascii=False)
        cjk, other = counts(text)
        total_cjk += cjk
        total_other += other
    cjk, other = counts(json.dumps(tools, ensure_ascii=False))
    total_cjk += cjk
    total_other += other
    return total_cjk, total_other


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _request_record(messages: list[dict], tools: list[dict]) -> dict:
    return {
        "seq": 2,
        "ts": "2026-08-01T09:00:05+08:00",
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "request_id": REQUEST_ID,
        "round": 1,
        "client_label": "brain",
        "provider": "claude_code",
        "model": "claude-opus-5",
        "call_type": "chat_with_tools",
        "temperature": None,
        "response_schema": None,
        "messages": messages,
        "tools": tools,
    }


def _non_brain_record() -> dict:
    return {
        "seq": 1,
        "ts": "2026-08-01T08:59:00+08:00",
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "request_id": "req_000001",
        "round": 1,
        "client_label": "worker-1",
        "provider": "claude_code",
        "model": "claude-opus-5",
        "call_type": "chat_with_tools",
        "temperature": None,
        "response_schema": None,
        "messages": [{"role": "user", "content": "irrelevant worker call"}],
        "tools": [],
    }


def test_analyze_latest_brain_request_calibrated(tmp_path: Path):
    messages = _build_messages()
    tools = _build_tools()
    session_dir = tmp_path / SESSION_ID
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "requests.jsonl",
        [_non_brain_record(), _request_record(messages, tools)],
    )

    total_cjk, total_other = _fixture_char_totals(messages, tools)
    target_cjk_rate = 1.2  # within the module's [0.5, 3.0] plausibility clamp
    reported = round(total_other / 3.6 + total_cjk * target_cjk_rate)
    _write_jsonl(session_dir / "turns.jsonl", [{
        "turn_id": TURN_ID,
        "ts_started": "2026-08-01T09:00:00+08:00",
        "ts_finished": "2026-08-01T09:00:06+08:00",
        "session_id": SESSION_ID,
        "channel": "discord",
        "sender": "毓峰",
        "inbound_kind": "message",
        "input_text": "早安",
        "status": "completed",
        "llm_rounds": 1,
        "max_prompt_tokens": reported,
    }])

    result = analyze_latest_brain_request(tmp_path, soft_max_prompt_tokens=400_000)

    assert result["available"] is True
    assert result["session_id"] == SESSION_ID
    assert result["turn_id"] == TURN_ID
    assert result["request_id"] == REQUEST_ID
    assert result["round"] == 1
    assert result["message_count"] == len(messages)
    assert result["tool_count"] == len(tools)

    segment_keys = [seg["key"] for seg in result["segments"]]
    assert segment_keys == _EXPECTED_SEGMENT_ORDER

    assert result["calibrated"] is True
    assert result["reported_prompt_tokens"] == reported
    assert result["total_tokens"] == reported
    segment_sum = sum(seg["tokens"] for seg in result["segments"])
    assert abs(segment_sum - reported) <= 1

    by_key = {seg["key"]: seg for seg in result["segments"]}

    boot_core_rules_items = {item["key"] for item in by_key["boot_core_rules"]["items"]}
    assert "memory/agent/persona.md" in boot_core_rules_items
    assert "memory/agent/rules.md" in boot_core_rules_items

    boot_tool_files_items = {item["key"] for item in by_key["boot_tool_files"]["items"]}
    assert "memory/agent/index.md" in boot_tool_files_items
    assert "memory/agent/temp-memory.md" in boot_tool_files_items

    pinned_context_items = {item["key"] for item in by_key["pinned_context"]["items"]}
    assert "memory/people/yufeng/basic-info.md" in pinned_context_items
    assert "memory/agent/mine.md" in pinned_context_items

    current_turn_items = {item["key"] for item in by_key["current_turn"]["items"]}
    assert {"user_message", "runtime_context", "agent_notes", "tool_loop"} <= current_turn_items

    # system_prompt / history have no per-item breakdown by design.
    assert by_key["system_prompt"]["items"] == []
    assert by_key["history"]["items"] == []
    assert by_key["history"]["tokens"] > 0

    tool_definitions_items = by_key["tool_definitions"]["items"]
    assert len(tool_definitions_items) == 1
    assert tool_definitions_items[0]["label"] == "3 tool schemas"


def test_analyze_latest_brain_request_without_turn_record_is_uncalibrated(tmp_path: Path):
    messages = _build_messages()
    tools = _build_tools()
    session_dir = tmp_path / SESSION_ID
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "requests.jsonl",
        [_non_brain_record(), _request_record(messages, tools)],
    )
    # No turns.jsonl written at all: nothing to calibrate against.

    result = analyze_latest_brain_request(tmp_path, soft_max_prompt_tokens=400_000)

    assert result["available"] is True
    assert result["calibrated"] is False
    assert result["reported_prompt_tokens"] is None
    assert result["total_tokens"] == sum(seg["tokens"] for seg in result["segments"])
    assert result["total_tokens"] > 0


def test_analyze_latest_brain_request_empty_sessions_dir(tmp_path: Path):
    empty_dir = tmp_path / "empty_sessions"
    empty_dir.mkdir()

    result = analyze_latest_brain_request(empty_dir, soft_max_prompt_tokens=400_000)

    assert result["available"] is False
    assert isinstance(result["reason"], str) and result["reason"]
