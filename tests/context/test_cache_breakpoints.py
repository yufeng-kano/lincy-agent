"""Tests for shared prompt-cache breakpoint helpers and subagent wiring."""

import pytest
from pydantic import ValidationError

from lincy.context.cache_breakpoints import (
    advance_cache_breakpoint,
    build_cache_control,
    resolve_breakpoint_cache_ttl,
)
from lincy.core.schema import CacheConfig
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, ToolParameter
from lincy.tools.registry import ToolRegistry
from lincy.worker.runner import WorkerRunner


def test_resolve_breakpoint_cache_ttl_clamps_for_claude_code():
    assert resolve_breakpoint_cache_ttl(
        provider="claude_code",
        enabled=True,
        configured_ttl="24h",
    ) == "1h"


def test_resolve_breakpoint_cache_ttl_disabled_or_non_breakpoint():
    assert resolve_breakpoint_cache_ttl(
        provider="claude_code",
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
            provider="claude_code",
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
