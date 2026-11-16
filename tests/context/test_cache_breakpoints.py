"""Tests for shared prompt-cache breakpoint helpers and subagent wiring."""

import pytest
from pydantic import ValidationError

from lincy.context.cache_breakpoints import (
    TOOL_RESULT_CACHE_CONTROL,
    advance_cache_breakpoint,
    build_cache_control,
    resolve_breakpoint_cache_ttl,
)
from lincy.core.schema import CacheConfig
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, ToolParameter
from lincy.tools.registry import ToolRegistry
from lincy.worker.runner import WorkerRunner


def test_resolve_breakpoint_cache_ttl_clamps_for_kano_proxy():
    assert resolve_breakpoint_cache_ttl(
        provider="kano_proxy",
        enabled=True,
        configured_ttl="24h",
    ) == "1h"


def test_resolve_breakpoint_cache_ttl_disabled_or_non_breakpoint():
    assert resolve_breakpoint_cache_ttl(
        provider="kano_proxy",
        enabled=False,
        configured_ttl="1h",
    ) is None
    assert resolve_breakpoint_cache_ttl(
        provider="openai",
        enabled=True,
        configured_ttl="24h",
    ) is None


def test_resolve_breakpoint_cache_ttl_rejects_unknown_ttl():
    with pytest.raises(ValueError, match="unsupported cache.ttl"):
        resolve_breakpoint_cache_ttl(
            provider="kano_proxy",
            enabled=True,
            configured_ttl="7d",
        )


def test_build_cache_control_shapes():
    assert build_cache_control(None) is None
    assert build_cache_control("ephemeral") == {"type": "ephemeral"}
    assert build_cache_control("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_build_cache_control_rejects_unknown_ttl():
    with pytest.raises(ValueError, match="unsupported cache.ttl"):
        build_cache_control("7d")


def test_cache_config_rejects_unknown_ttl_at_load():
    with pytest.raises(ValidationError):
        CacheConfig(enabled=True, ttl="7d")


def test_advance_cache_breakpoint_marks_latest_user_message():
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    messages = [
        Message(role="system", content="sys", cache_control=cache_ctrl),
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
        ),
        Message(role="tool", content="x", tool_call_id="c1", name="echo"),
    ]

    advanced = advance_cache_breakpoint(messages)

    assert messages[1].cache_control is None
    assert isinstance(messages[1].content, str)
    assert isinstance(advanced[1].content, list)
    assert advanced[1].content[0].cache_control == cache_ctrl
    assert advanced[0].cache_control == cache_ctrl


def test_worker_runner_advances_cache_breakpoint_each_request():
    class _FakeClient:
        def __init__(self):
            self.calls: list[list[Message]] = []
            self._n = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del tools, temperature
            self.calls.append([m.model_copy(deep=True) for m in messages])
            self._n += 1
            if self._n == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
                    total_tokens=1,
                )
            return LLMResponse(content="done", total_tokens=1)

    registry = ToolRegistry()
    registry.register(
        "echo",
        lambda text: text,
        ToolDefinition(
            name="echo",
            description="Echo",
            parameters={"text": ToolParameter(type="string", description="t")},
            required=["text"],
        ),
    )
    client = _FakeClient()
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    runner = WorkerRunner(
        client,
        registry,
        frozenset(),
        "system prompt",
        cache_control=cache_ctrl,
    )

    result = runner.run("do work", worker_label="worker-cache")

    assert result.success is True
    assert len(client.calls) == 2
    for call in client.calls:
        assert call[0].role == "system"
        assert call[0].cache_control == cache_ctrl
        user = next(m for m in call if m.role == "user")
        assert isinstance(user.content, list)
        assert user.content[0].cache_control == cache_ctrl


def _tool_loop_messages(cache_ctrl: dict[str, str]) -> list[Message]:
    return [
        Message(role="system", content="sys", cache_control=cache_ctrl),
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "x"}),
                ToolCall(id="c2", name="echo", arguments={"text": "y"}),
            ],
        ),
        Message(role="tool", content="x", tool_call_id="c1", name="echo"),
        Message(role="tool", content="y", tool_call_id="c2", name="echo"),
    ]


def test_advance_cache_breakpoint_can_mark_the_latest_tool_result():
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    messages = _tool_loop_messages(cache_ctrl)

    advanced = advance_cache_breakpoint(messages, include_tool_results=True)

    # The loop reads this entry seconds later, so it never takes the 1h TTL.
    assert advanced[4].cache_control == TOOL_RESULT_CACHE_CONTROL == {"type": "ephemeral"}
    assert advanced[4].content == "y"
    assert advanced[3].cache_control is None
    assert isinstance(advanced[1].content, str)
    assert advanced[0].cache_control == cache_ctrl
    assert all(m.cache_control is None for m in messages[1:])


def test_advance_cache_breakpoint_moves_off_an_earlier_tool_result():
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    first = advance_cache_breakpoint(_tool_loop_messages(cache_ctrl), include_tool_results=True)
    grown = [
        *first,
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c3", name="echo", arguments={"text": "z"})],
        ),
        Message(role="tool", content="z", tool_call_id="c3", name="echo"),
    ]

    advanced = advance_cache_breakpoint(grown, include_tool_results=True)

    marked = [i for i, m in enumerate(advanced) if m.role == "tool" and m.cache_control]
    assert marked == [6]


def test_advance_cache_breakpoint_prefers_a_later_user_message_over_tool_results():
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    messages = [*_tool_loop_messages(cache_ctrl), Message(role="user", content="wrap up")]

    advanced = advance_cache_breakpoint(messages, include_tool_results=True)

    assert all(m.cache_control is None for m in advanced if m.role == "tool")
    assert advanced[5].content[0].cache_control == cache_ctrl


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "echo",
        lambda text: text,
        ToolDefinition(
            name="echo",
            description="Echo",
            parameters={"text": ToolParameter(type="string", description="t")},
            required=["text"],
        ),
    )
    return registry


class _TwoRoundClient:
    """One tool call, then a final answer; records each request."""

    def __init__(self, tool_text: str = "hi"):
        self.calls: list[list[Message]] = []
        self._tool_text = tool_text

    def chat_with_tools(self, messages, tools, temperature=None):
        del tools, temperature
        self.calls.append([m.model_copy(deep=True) for m in messages])
        if len(self.calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": self._tool_text})],
                total_tokens=1,
            )
        return LLMResponse(content="done", total_tokens=1)


def test_worker_runner_marks_the_latest_tool_result_when_enabled():
    client = _TwoRoundClient()
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    runner = WorkerRunner(
        client,
        _echo_registry(),
        frozenset(),
        "system prompt",
        cache_control=cache_ctrl,
        cache_tool_results=True,
    )

    assert runner.run("do work", worker_label="worker-cache").success is True

    first, second = client.calls
    # No tool result yet: the task message carries the breakpoint.
    assert first[1].content[0].cache_control == cache_ctrl
    assert second[-1].role == "tool"
    assert second[-1].cache_control == TOOL_RESULT_CACHE_CONTROL
    assert isinstance(second[1].content, str)
    assert second[0].cache_control == cache_ctrl


def test_worker_runner_skips_tool_result_breakpoint_once_history_is_compacted():
    # The tool output alone overflows the budget, so the second request has
    # dropped that turn: a breakpoint there would be written and never read.
    client = _TwoRoundClient(tool_text="word " * 4000)
    cache_ctrl = {"type": "ephemeral", "ttl": "1h"}
    runner = WorkerRunner(
        client,
        _echo_registry(),
        frozenset(),
        "system prompt",
        max_context_tokens=500,
        cache_control=cache_ctrl,
        cache_tool_results=True,
    )

    runner.run("do work", worker_label="worker-cache")

    second = client.calls[1]
    assert [m.role for m in second] == ["system", "user"]
    assert second[1].content[0].cache_control == cache_ctrl
