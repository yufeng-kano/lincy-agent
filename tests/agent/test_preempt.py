"""Tests for folding fresher same-conversation inbound into the running turn."""

from contextlib import nullcontext
from unittest.mock import MagicMock

from lincy.agent.core import _run_responder
from lincy.context.conversation import Conversation
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition
from lincy.tools.registry import ToolRegistry


class _Client:
    """Fake LLM client that returns queued responses."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls = 0

    def chat_with_tools(self, messages, tools, temperature=None):
        del messages, tools, temperature
        self.calls += 1
        if not self._responses:
            raise RuntimeError("no response queued")
        return self._responses.pop(0)


class _Builder:
    def build(self, conversation):
        del conversation
        return [
            Message(role="system", content="sys"),
            Message(role="user", content="u"),
        ]


def _make_registry(tool_names: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in tool_names:
        defn = ToolDefinition(name=name, description=f"test {name}", parameters={})
        registry.register(name, lambda **kw: "OK", defn)
    return registry


def _console():
    console = MagicMock()
    console.spinner.side_effect = lambda *a, **k: nullcontext()
    console.debug = False
    console.show_tool_use = False
    return console


def _base_messages():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="u"),
    ]


def _run(client, conv, registry, **kwargs):
    return _run_responder(
        client=client,
        messages=_base_messages(),
        tools=[],
        conversation=conv,
        builder=_Builder(),
        registry=registry,
        console=_console(),
        **kwargs,
    )


def test_pending_tool_calls_cancelled_and_turn_continues_with_folded_inbound():
    """Read-only tools are preempted too; the folded message drives the next call."""
    registry = _make_registry(["memory_search", "send_message"])
    conv = Conversation()
    client = _Client([
        LLMResponse(
            content="I'll look that up",
            tool_calls=[
                ToolCall(id="t1", name="memory_search", arguments={}),
                ToolCall(id="t2", name="send_message", arguments={}),
            ],
        ),
        LLMResponse(content="answer for the new message", tool_calls=[]),
    ])
    pending = [True]

    def _check():
        return pending[0]

    def _fold():
        pending[0] = False
        conv.add("user", "actually, change of plan", channel="cli", sender="u")
        return True

    response = _run(client, conv, registry, check_preempt=_check, on_preempt=_fold)

    assert response.content == "answer for the new message"
    assert client.calls == 2
    messages = conv.get_messages()
    roles = [m.role for m in messages]
    # assistant (tools) -> cancelled results -> folded user message
    assert roles == ["assistant", "tool", "tool", "user"]
    assert messages[0].message.content is None  # stale draft stripped
    assert "preempted" in messages[1].content
    assert "preempted" in messages[2].content
    assert messages[3].content == "actually, change of plan"


def test_turn_ends_when_nothing_was_queued_after_all():
    registry = _make_registry(["send_message"])
    client = _Client([
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="t1", name="send_message", arguments={})],
        ),
    ])

    response = _run(
        client, Conversation(), registry,
        check_preempt=lambda: True, on_preempt=lambda: False,
    )

    assert client.calls == 1
    assert response.content is None
    assert response.tool_calls == []


def test_turn_ends_without_fold_callback():
    """Without on_preempt the old behaviour stays: cancel and end the turn."""
    registry = _make_registry(["send_message"])
    client = _Client([
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="t1", name="send_message", arguments={})],
        ),
    ])

    response = _run(client, Conversation(), registry, check_preempt=lambda: True)

    assert client.calls == 1
    assert response.content is None
    assert response.tool_calls == []


def test_completed_tools_in_the_round_are_kept():
    registry = _make_registry(["memory_search", "send_message", "schedule_action"])
    conv = Conversation()
    client = _Client([
        LLMResponse(
            content="I'll send that now",
            tool_calls=[
                ToolCall(id="t1", name="memory_search", arguments={}),
                ToolCall(id="t2", name="send_message", arguments={}),
                ToolCall(id="t3", name="schedule_action", arguments={}),
            ],
        ),
    ])
    checks = iter([False, True, True])

    _run(client, conv, registry, check_preempt=lambda: next(checks))

    tool_msgs = [m for m in conv.get_messages() if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["memory_search", "send_message", "schedule_action"]
    assert tool_msgs[0].content == "OK"
    assert "preempted" in tool_msgs[1].content
    assert "preempted" in tool_msgs[2].content


def test_tools_run_when_nothing_is_pending():
    registry = _make_registry(["send_message"])
    client = _Client([
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="t1", name="send_message", arguments={})],
        ),
        LLMResponse(content="sent", tool_calls=[]),
    ])

    response = _run(client, Conversation(), registry, check_preempt=lambda: False)

    assert response.content == "sent"


def test_max_preempts_zero_disables_preemption():
    registry = _make_registry(["send_message"])
    calls = 0

    def _check():
        nonlocal calls
        calls += 1
        return True

    client = _Client([
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="t1", name="send_message", arguments={})],
        ),
        LLMResponse(content="finally sent", tool_calls=[]),
    ])

    response = _run(client, Conversation(), registry, check_preempt=_check, max_preempts=0)

    assert response.content == "finally sent"
    assert calls == 0


def test_max_preempts_caps_folds_per_turn():
    """After the cap, later messages wait for the next turn and tools run."""
    registry = _make_registry(["send_message"])
    conv = Conversation()
    client = _Client([
        LLMResponse(content=None, tool_calls=[ToolCall(id="t1", name="send_message", arguments={})]),
        LLMResponse(content=None, tool_calls=[ToolCall(id="t2", name="send_message", arguments={})]),
        LLMResponse(content="done", tool_calls=[]),
    ])

    def _fold():
        conv.add("user", "more", channel="cli", sender="u")
        return True

    response = _run(
        client, conv, registry,
        check_preempt=lambda: True, on_preempt=_fold, max_preempts=1,
    )

    assert response.content == "done"
    tool_msgs = [m for m in conv.get_messages() if m.role == "tool"]
    assert "preempted" in tool_msgs[0].content
    assert tool_msgs[1].content == "OK"


def test_core_preempt_taker_folds_queued_scope_messages(tmp_path):
    """The core taker pulls same-scope inbound, records it, and acks it."""
    from lincy.agent.core import AgentCore
    from lincy.agent.queue import PersistentPriorityQueue
    from lincy.agent.schema import InboundMessage

    q = PersistentPriorityQueue(tmp_path / "q")
    core = AgentCore.__new__(AgentCore)
    core._queue = q
    core.console = MagicMock()
    core.conversation = Conversation()
    q.put(InboundMessage(
        channel="discord", content="change of plan", priority=0, sender="yu",
        metadata={"scope_id": "discord:dm:1", "channel_id": "c1", "is_dm": True},
    ))
    q.put(InboundMessage(
        channel="discord", content="someone else", priority=0, sender="x",
        metadata={"scope_id": "discord:dm:2"},
    ))

    check = core._make_preempt_checker("discord", "discord:dm:1")
    take = core._make_preempt_taker("discord", "discord:dm:1")

    assert check()
    assert take() is True
    assert not check()
    [entry] = core.conversation.get_messages()
    assert entry.role == "user"
    assert entry.content == "change of plan"
    assert entry.channel == "discord"
    assert entry.sender == "yu"
    assert entry.metadata["scope_id"] == "discord:dm:1"
    core.console.print_inbound.assert_called_once()
    assert q.pending_inbound_count() == 1
    assert not list((tmp_path / "q" / "active").iterdir())
    assert take() is False
