"""Tests for what the memory-sync side channel reports back to its callers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lincy.agent.turn_runtime import _run_memory_sync_side_channel
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.llm.schema import LLMResponse, ToolCall, ToolDefinition
from lincy.tools.registry import ToolResult

TARGET = "memory/agent/temp-memory.md"
TOOLS = [ToolDefinition(name="memory_edit", description="edit memory", parameters={})]


class _Client:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls = 0

    def chat_with_tools(self, messages, tools, temperature=None):
        del messages, tools, temperature
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="nothing more", tool_calls=[])


def _edit_call(call_id: str = "c1") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCall(
        id=call_id, name="memory_edit",
        arguments={"requests": [{"request_id": "r1", "target_path": TARGET, "instruction": "rewrite"}]},
    )])


def _result(status: str, applied_path: str | None) -> str:
    applied = [{"request_id": "r1", "status": "applied", "path": applied_path}] if applied_path else []
    errors = [] if status == "ok" else [{"request_id": "r1", "code": "temp_memory_too_large", "detail": "x"}]
    return json.dumps({"status": status, "turn_id": "t", "applied": applied, "errors": errors})


def _registry(*contents: str) -> MagicMock:
    registry = MagicMock()
    registry.has_tool.return_value = True
    registry.execute.side_effect = [
        ToolResult(content, is_error=content.startswith("Error")) for content in contents
    ]
    return registry


def _run(tmp_path: Path, client: _Client, registry: MagicMock) -> bool:
    return _run_memory_sync_side_channel(
        client,
        Conversation(),
        ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path),
        TOOLS,
        registry,
        MagicMock(),
        missing_targets=[TARGET],
        max_retries=1,
    )


def test_applied_state_file_reports_success(tmp_path: Path):
    client = _Client([_edit_call()])
    registry = _registry(_result("ok", TARGET))

    assert _run(tmp_path, client, registry) is True
    assert registry.execute.call_count == 1
    assert client.calls == 1


def test_rejected_edit_is_retried_once_and_reports_failure(tmp_path: Path):
    """A rolled-back rewrite comes back as JSON with status failed, not as an Error string."""
    client = _Client([_edit_call("c1"), _edit_call("c2")])
    registry = _registry(_result("failed", None), _result("failed", None))

    assert _run(tmp_path, client, registry) is False
    assert registry.execute.call_count == 2
    assert client.calls == 2


def test_edit_of_another_file_does_not_settle_the_state_file(tmp_path: Path):
    client = _Client([_edit_call()])
    registry = _registry(_result("ok", "memory/agent/long-term.md"))

    assert _run(tmp_path, client, registry) is False
    assert client.calls == 1  # not an error, so no retry


def test_no_tool_call_reports_failure_without_retry(tmp_path: Path):
    client = _Client([LLMResponse(content="all good already", tool_calls=[])])
    registry = _registry()

    assert _run(tmp_path, client, registry) is False
    assert registry.execute.call_count == 0
    assert client.calls == 1
