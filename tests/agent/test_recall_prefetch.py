"""Tests for the per-turn recall prefetch attached to the turn overlay."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.agent.core import AgentCore
from lincy.agent.token_telemetry import LatestTokenStatus, TurnTokenUsage
from lincy.agent.turn_context import TurnContext
from lincy.agent.turn_overlay import RECALL_PREFETCH_METADATA_KEY
from lincy.context.builder import TURN_OVERLAY_METADATA_KEY, ContextBuilder
from lincy.context.conversation import Conversation
from lincy.core.schema import RecallAgentConfig
from lincy.llm.schema import Message


class _FakeRecallAgent:
    def __init__(self, result: str | None = "- [2026-11-05 14:44][session/x] took the medicine"):
        self.config = RecallAgentConfig()
        self.calls: list[dict] = []
        self.result = result
        self.error: Exception | None = None

    def recall(
        self, question, *, context_tail="", exclude_paths=(), max_tool_calls=None,
        deadline_seconds=None,
    ):
        self.calls.append({
            "question": question,
            "context_tail": context_tail,
            "exclude_paths": set(exclude_paths),
            "max_tool_calls": max_tool_calls,
            "deadline_seconds": deadline_seconds,
        })
        if self.error is not None:
            raise self.error
        return self.result


def _make_core(tmp_path: Path, recall_agent) -> AgentCore:
    core = AgentCore.__new__(AgentCore)
    core.client = MagicMock()
    core.conversation = Conversation()
    core.builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    core.registry = MagicMock()
    core.console = MagicMock()
    core.console.debug = False
    core.config = SimpleNamespace(
        context=SimpleNamespace(
            common_ground=SimpleNamespace(enabled=False),
            boot_files=["memory/agent/long-term.md"],
            boot_files_as_tool=["memory/agent/temp-memory.md"],
        ),
        tools=SimpleNamespace(memory_edit=SimpleNamespace(turn_retry_limit=1)),
    )
    core.agent_os_dir = tmp_path
    core.turn_context = TurnContext()
    core.shared_state_store = None
    core.note_store = None
    core.recall_agent = recall_agent
    core.memory_edit_allow_failure = False
    core._latest_token_status = LatestTokenStatus()
    core._turn_token_usage = TurnTokenUsage()
    return core


def _text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.text or "" for part in message.content or [])


def _last_user_text(messages: list[Message]) -> str:
    return next(_text(m) for m in reversed(messages) if m.role == "user")


def test_user_turn_prefetches_recall_into_overlay(tmp_path: Path):
    agent = _FakeRecallAgent()
    core = _make_core(tmp_path, agent)
    core.conversation.add("user", "earlier question", channel="cli", sender="tester")
    core.conversation.add("assistant", "earlier answer")
    metadata: dict[str, object] = {"turn_processing_started_at": "2026-11-05T15:00:00+08:00"}

    prepared = core._prepare_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )

    assert len(agent.calls) == 1
    call = agent.calls[0]
    assert call["question"] == "did he take the medicine"
    assert "user: earlier question" in call["context_tail"]
    assert "assistant: earlier answer" in call["context_tail"]
    assert call["max_tool_calls"] == agent.config.prefetch_max_tool_calls
    assert call["deadline_seconds"] == agent.config.prefetch_deadline_seconds
    assert call["exclude_paths"] == {"memory/agent/long-term.md", "memory/agent/temp-memory.md"}
    latest = _last_user_text(prepared.messages)
    assert "[Recall]\n- [2026-11-05 14:44][session/x] took the medicine" in latest
    assert latest.index("[Runtime Context]") < latest.index("[Recall]")
    assert metadata[RECALL_PREFETCH_METADATA_KEY] == agent.result
    entry = core.conversation.get_messages()[-1]
    assert "[Recall]" in entry.metadata[TURN_OVERLAY_METADATA_KEY]
    assert entry.content == "did he take the medicine"


def test_retry_reuses_cached_prefetch_instead_of_searching_again(tmp_path: Path):
    agent = _FakeRecallAgent()
    core = _make_core(tmp_path, agent)
    metadata: dict[str, object] = {}

    first = core._prepare_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )
    core.conversation.truncate_to(first.pre_turn_anchor)
    agent.result = "changed"
    retry = core._prepare_retry_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )

    assert len(agent.calls) == 1
    assert "[Recall]\n- [2026-11-05 14:44][session/x] took the medicine" in _last_user_text(retry.messages)


def test_empty_result_is_cached_and_renders_no_block(tmp_path: Path):
    agent = _FakeRecallAgent(result=None)
    core = _make_core(tmp_path, agent)
    metadata: dict[str, object] = {}

    prepared = core._prepare_turn_attempt(
        "anything new today", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )

    assert "[Recall]" not in _last_user_text(prepared.messages)
    assert metadata[RECALL_PREFETCH_METADATA_KEY] == ""
    core._prepare_turn_attempt(
        "anything new today", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )
    assert len(agent.calls) == 1


def test_internal_turns_and_short_messages_skip_prefetch(tmp_path: Path):
    agent = _FakeRecallAgent()
    core = _make_core(tmp_path, agent)

    for metadata in ({"system": True}, {"scheduled_reason": "follow up"}, {"task_due": True}):
        core._prepare_turn_attempt(
            "[HEARTBEAT] check in", channel="system", sender="system", timestamp=None,
            turn_metadata=metadata,
        )
    core._prepare_turn_attempt(
        "[worker:research] SUCCESS long report", channel="worker", sender="research",
        timestamp=None, turn_metadata={},
    )
    core._prepare_turn_attempt(
        "shell task 12 finished", channel="shell_task", sender=None, timestamp=None, turn_metadata={},
    )
    core._prepare_turn_attempt("hi", channel="cli", sender="tester", timestamp=None, turn_metadata={})
    core._prepare_turn_attempt("long enough", channel="cli", sender="tester", timestamp=None, turn_metadata=None)

    assert agent.calls == []


def test_prefetch_can_be_disabled_or_absent(tmp_path: Path):
    agent = _FakeRecallAgent()
    agent.config = RecallAgentConfig(prefetch=False)
    core = _make_core(tmp_path, agent)
    core._prepare_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None, turn_metadata={},
    )
    assert agent.calls == []

    core = _make_core(tmp_path, None)
    prepared = core._prepare_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None, turn_metadata={},
    )
    assert "[Recall]" not in _last_user_text(prepared.messages)


def test_recall_failure_does_not_break_the_turn(tmp_path: Path):
    agent = _FakeRecallAgent()
    agent.error = RuntimeError("kano-proxy down")
    core = _make_core(tmp_path, agent)
    metadata: dict[str, object] = {}

    prepared = core._prepare_turn_attempt(
        "did he take the medicine", channel="cli", sender="tester", timestamp=None,
        turn_metadata=metadata,
    )

    assert "[Recall]" not in _last_user_text(prepared.messages)
    assert metadata[RECALL_PREFETCH_METADATA_KEY] == ""
