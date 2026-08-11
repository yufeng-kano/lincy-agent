"""Tests for memory editor planner structured-output fallback."""

from __future__ import annotations

from lincy.core.schema import (
    ClaudeCodeConfig,
    CopilotConfig,
    DeepSeekConfig,
    DeepSeekThinkingConfig,
)
from lincy.memory.editor.planner import MemoryEditPlanner
from lincy.memory.editor.schema import MemoryEditRequest


class _PlannerClient:
    """Minimal chat client stub that records planner calls."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, response_schema=None, temperature=None):  # noqa: ANN001,ARG002
        self.calls.append(
            {
                "messages": messages,
                "response_schema": response_schema,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected planner call")
        return self._responses.pop(0)


def _request() -> MemoryEditRequest:
    return MemoryEditRequest(
        request_id="r1",
        target_path="memory/agent/recent.md",
        instruction="Append today's summary.",
    )


def test_provider_response_schema_capabilities_match_adapter_support():
    assert ClaudeCodeConfig(model="claude-sonnet-4-6").supports_response_schema() is False
    assert CopilotConfig(model="gpt-4.1").supports_response_schema() is True
    assert DeepSeekConfig(
        model="deepseek-v4-flash",
        thinking=DeepSeekThinkingConfig(enabled=False),
    ).supports_response_schema() is False


def test_memory_edit_planner_uses_native_response_schema_when_supported():
    client = _PlannerClient([
        (
            '{"status":"ok","operations":['
            '{"kind":"append_entry","payload_text":"- note"}'
            "]}"
        )
    ])
    planner = MemoryEditPlanner(
        client,
        "You are the memory editor.",
        supports_response_schema=True,
    )

    plan = planner.plan(
        request=_request(),
        as_of="2026-03-14T22:00:00+08:00",
        turn_id="turn-1",
        file_exists=True,
        file_content="# recent\n",
    )

    assert plan.status == "ok"
    assert len(plan.operations) == 1
    assert client.calls[0]["response_schema"] is not None
    user_payload = client.calls[0]["messages"][-1].content
    assert '"content_available": true' in user_payload


def test_memory_edit_planner_falls_back_to_text_json_when_schema_is_unsupported():
    client = _PlannerClient([
        (
            '{"status":"ok","operations":['
            '{"kind":"append_entry","payload_text":"- note"}'
            "]}"
        )
    ])
    planner = MemoryEditPlanner(
        client,
        "You are the memory editor.",
        supports_response_schema=False,
    )

    plan = planner.plan(
        request=_request(),
        as_of="2026-03-14T22:00:00+08:00",
        turn_id="turn-1",
        file_exists=True,
        file_content="# recent\n",
    )

    assert plan.status == "ok"
    assert client.calls[0]["response_schema"] is None
    messages = client.calls[0]["messages"]
    assert messages[0].role == "system"
    assert messages[1].role == "system"
    assert "Native structured outputs are unavailable" in messages[1].content


def test_memory_edit_planner_can_mark_file_content_unavailable():
    client = _PlannerClient([
        (
            '{"status":"ok","operations":['
            '{"kind":"append_entry","payload_text":"- note"}'
            "]}"
        )
    ])
    planner = MemoryEditPlanner(
        client,
        "You are the memory editor.",
        supports_response_schema=True,
    )

    plan = planner.plan(
        request=_request(),
        as_of="2026-03-14T22:00:00+08:00",
        turn_id="turn-1",
        file_exists=True,
        file_content="",
        file_content_available=False,
    )

    assert plan.status == "ok"
    user_payload = client.calls[0]["messages"][-1].content
    assert '"content_available": false' in user_payload


def test_memory_edit_planner_fallback_keeps_parse_retry_flow():
    client = _PlannerClient([
        "not json",
        (
            '{"status":"ok","operations":['
            '{"kind":"append_entry","payload_text":"- retry"}'
            "]}"
        ),
    ])
    planner = MemoryEditPlanner(
        client,
        "You are the memory editor.",
        supports_response_schema=False,
        parse_retries=1,
        parse_retry_prompt="Return valid JSON now.",
    )

    plan = planner.plan(
        request=_request(),
        as_of="2026-03-14T22:00:00+08:00",
        turn_id="turn-1",
        file_exists=False,
        file_content="",
    )

    assert plan.status == "ok"
    assert len(client.calls) == 2
    assert client.calls[0]["response_schema"] is None
    assert client.calls[1]["response_schema"] is None
    retry_messages = client.calls[1]["messages"]
    assert retry_messages[-1].role == "user"
    assert retry_messages[-1].content == "Return valid JSON now."
