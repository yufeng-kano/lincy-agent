"""Tests for the responder-side per-turn dynamic overlay end to end.

Covers the architecture change where [Runtime Context], [Timing Notice],
[Decision Reminder], and [Agent Notes] moved from ContextBuilder.build()'s
frozen render cache into a responder-only overlay applied to the latest
user message of each outgoing request (see agent/turn_overlay.py,
agent/responder.py:_build_dynamic_turn_overlay, and
docs/dev/agent-task-system.md).
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.agent.core import AgentCore, _run_responder
from lincy.agent.turn_runtime import LatestTokenStatus, TurnTokenUsage
from lincy.agent.note_store import NoteStore
from lincy.agent.turn_context import TurnContext
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.llm.schema import LLMResponse, Message, ToolCall
from lincy.tools.registry import ToolResult


class _FakeClient:
    """Round 1 makes a tool call; round 2 finishes with plain content."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self._n = 0

    def chat_with_tools(self, messages, tools, temperature=None):
        del tools, temperature
        self.calls.append(list(messages))
        self._n += 1
        if self._n == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="t1", name="dummy", arguments={})],
            )
        return LLMResponse(content="done", tool_calls=[])


class _NoteMutatingRegistry:
    """Executing the tool call edits a note, simulating a mid-turn write."""

    def __init__(self, note_store: NoteStore) -> None:
        self._note_store = note_store

    def has_tool(self, name: str) -> bool:
        return name == "dummy"

    def execute(self, tool_call: ToolCall) -> ToolResult:
        del tool_call
        self._note_store.update(key="location", value="changed-mid-turn")
        return ToolResult("OK")


def _make_core(tmp_path: Path) -> tuple[AgentCore, NoteStore, _FakeClient]:
    """Minimal AgentCore double wired for _prepare_turn_attempt + _run_responder."""
    note_store = NoteStore(tmp_path / "state")
    note_store.create(key="location", value="original")

    client = _FakeClient()
    core = AgentCore.__new__(AgentCore)
    core.client = client
    core.conversation = Conversation()
    core.builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    core.registry = _NoteMutatingRegistry(note_store)
    core.console = MagicMock()
    core.console.debug = False
    core.console.show_tool_use = False
    core.console.spinner.side_effect = lambda *a, **k: nullcontext()
    core.config = SimpleNamespace(
        context=SimpleNamespace(common_ground=SimpleNamespace(enabled=False)),
        tools=SimpleNamespace(
            max_tool_iterations=5,
            memory_edit=SimpleNamespace(turn_retry_limit=1),
        ),
        agents={"brain": SimpleNamespace(staged_planning=SimpleNamespace(enabled=False))},
        features=SimpleNamespace(
            send_message_batch_guidance=SimpleNamespace(enabled=False),
        ),
    )
    core.agent_os_dir = tmp_path
    core.turn_context = TurnContext()
    core.shared_state_store = None
    core.note_store = note_store
    core.memory_edit_allow_failure = False
    core._latest_token_status = LatestTokenStatus()
    core._turn_token_usage = TurnTokenUsage()
    return core, note_store, client


def _latest_user_message(messages: list[Message]) -> Message:
    return next(m for m in reversed(messages) if m.role == "user")


def _historical_user_messages(messages: list[Message]) -> list[Message]:
    user_messages = [m for m in messages if m.role == "user"]
    return user_messages[:-1]


def test_dynamic_overlay_lands_once_on_latest_user_message_and_is_snapshot_stable(
    tmp_path: Path,
):
    core, note_store, client = _make_core(tmp_path)

    # A prior, already-completed turn: its user message must stay clean.
    core.conversation.add("user", "earlier turn", channel="cli", sender="tester")
    core.conversation.add("assistant", "earlier reply")

    prepared = core._prepare_turn_attempt(
        "hello",
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata={"turn_processing_started_at": "2026-03-12T09:11:00+08:00"},
    )

    response = _run_responder(
        client=client,
        messages=prepared.messages,
        tools=[],
        conversation=core.conversation,
        builder=core.builder,
        registry=core.registry,
        console=core.console,
        max_iterations=5,
        message_overlay=prepared.message_overlay,
    )

    assert response.content == "done"
    assert len(client.calls) == 2
    # The tool execution between round 1 and round 2 really did mutate the
    # note store, so a naive rebuild-without-snapshot would show it.
    assert note_store.get("location").value == "changed-mid-turn"

    for call_messages in client.calls:
        latest = _latest_user_message(call_messages)
        assert latest.content.count("[Agent Notes]") == 1
        assert latest.content.count("[Runtime Context]") == 1
        assert 'location: "original"' in latest.content
        assert "changed-mid-turn" not in latest.content

        for historical in _historical_user_messages(call_messages):
            assert "[Agent Notes]" not in (historical.content or "")
            assert "[Runtime Context]" not in (historical.content or "")

    # Snapshot stability: both rounds saw byte-identical latest-user content,
    # even though the note store changed in between.
    first_latest = _latest_user_message(client.calls[0])
    second_latest = _latest_user_message(client.calls[1])
    assert first_latest.content == second_latest.content

    # The conversation store itself never received the injected blocks.
    for entry in core.conversation.get_messages():
        content = entry.message.content
        if isinstance(content, str):
            assert "[Agent Notes]" not in content
            assert "[Runtime Context]" not in content


def test_dynamic_overlay_absent_when_no_boot_dir_no_notes_no_decision_reminder(
    tmp_path: Path,
):
    core = AgentCore.__new__(AgentCore)
    core.conversation = Conversation()
    # builder has no agent_os_dir: [Runtime Context] has nothing to say.
    core.builder = ContextBuilder(system_prompt="sys")
    core.console = MagicMock()
    core.console.debug = False
    core.config = SimpleNamespace(
        context=SimpleNamespace(common_ground=SimpleNamespace(enabled=False)),
    )
    # core.agent_os_dir is unrelated to the builder's copy above; only used
    # here for _TurnMemorySnapshot's (unexercised) path bookkeeping.
    core.agent_os_dir = tmp_path
    core.shared_state_store = None
    core.note_store = None

    prepared = core._prepare_turn_attempt(
        "hi",
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata=None,
    )

    assert prepared.message_overlay is None


class _ImmediateFakeClient:
    """Always finishes immediately with plain content (no tool calls)."""

    def __init__(self, content: str = "done") -> None:
        self.calls: list[list[Message]] = []
        self._content = content

    def chat_with_tools(self, messages, tools, temperature=None):
        del tools, temperature
        self.calls.append(list(messages))
        return LLMResponse(content=self._content, tool_calls=[])


class _FakeConscienceAgent:
    """Always reports feedback, forcing the brain re-run branch."""

    def check(self, **kwargs):
        del kwargs
        return "add a warmer closing line"


def test_conscience_rerun_reuses_turn_overlay_snapshot_and_includes_agent_notes(
    tmp_path: Path, monkeypatch
):
    """The conscience re-run must see the same turn-start overlay snapshot
    as the primary call (dynamic blocks + common ground), not a fresh
    rebuild -- see agent/core.py:_maybe_run_conscience_check.
    """
    from lincy.agent import core as core_module

    core, note_store, _ = _make_core(tmp_path)
    # Dedicated client that always finishes in one round: the shared
    # _FakeClient always opens with a tool call, which is irrelevant noise
    # for this test (it only cares about the re-run's request content).
    client = _ImmediateFakeClient()
    core.client = client
    core.registry = MagicMock()
    core.registry.get_definitions.return_value = []
    core.conscience_agent = _FakeConscienceAgent()

    prepared = core._prepare_turn_attempt(
        "hello",
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata={"turn_processing_started_at": "2026-03-12T09:11:00+08:00"},
    )

    captured_overlays = []
    real_run_brain_responder = core_module._responder._run_brain_responder

    def _spy_run_brain_responder(**kwargs):
        captured_overlays.append(kwargs.get("message_overlay"))
        return real_run_brain_responder(
            **kwargs,
            run_responder_fn=core_module._run_responder,
            stage1_gather_fn=core_module.run_stage1_information_gathering,
            stage2_plan_fn=core_module.run_stage2_brain_planning,
        )

    monkeypatch.setattr(core_module, "_run_brain_responder", _spy_run_brain_responder)

    # The note store changes AFTER the primary call's overlay was snapshotted
    # but BEFORE the conscience re-run. If the re-run reused the identical
    # snapshot (no re-read of NoteStore), it must still show "original".
    note_store.update(key="location", value="changed-after-primary")

    primary_response = LLMResponse(content="hi there", tool_calls=[])
    result = core._maybe_run_conscience_check(
        response=primary_response,
        prepared=prepared,
        channel="cli",
        sender="tester",
        is_cancel_requested=None,
        on_cancel_pending=None,
    )

    assert result.content == "done"
    assert len(client.calls) == 1

    # Same object as the primary call's overlay -- not a fresh rebuild.
    assert len(captured_overlays) == 1
    assert captured_overlays[0] is prepared.message_overlay

    rerun_request = client.calls[0]
    user_messages = [m for m in rerun_request if m.role == "user"]
    latest = user_messages[-1]
    assert "[conscience-check] add a warmer closing line" in latest.content
    assert latest.content.count("[Agent Notes]") == 1
    assert 'location: "original"' in latest.content
    assert "changed-after-primary" not in latest.content

    for historical in user_messages[:-1]:
        assert "[Agent Notes]" not in (historical.content or "")
