"""End-to-end tests for the per-turn overlay stored on the turn's user entry.

AgentCore computes [Runtime Context], [Timing Notice], [Agent Notes] changes,
and common ground once per turn and stores them in the user entry's metadata
(see agent/turn_overlay.py); ContextBuilder renders them after that message on
every request, so earlier turns are never rewritten on the wire.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.agent.core import AgentCore, _run_responder
from lincy.agent.note_store import NoteStore
from lincy.agent.token_telemetry import LatestTokenStatus, TurnTokenUsage
from lincy.agent.turn_context import TurnContext
from lincy.agent.turn_overlay import AGENT_NOTES_SHOWN_METADATA_KEY
from lincy.context.builder import TURN_OVERLAY_METADATA_KEY, ContextBuilder
from lincy.context.conversation import Conversation
from lincy.llm.providers.anthropic import AnthropicClient
from lincy.llm.providers.anthropic_messages import convert_messages
from lincy.llm.schema import AnthropicMessagePayload, LLMResponse, Message, ToolCall
from lincy.tools.registry import ToolResult


class _FakeClient:
    """The first call makes a tool call; every later call finishes."""

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
    note_store.create(key="mood", value="ok")

    client = _FakeClient()
    core = AgentCore.__new__(AgentCore)
    core.client = client
    core.conversation = Conversation()
    core.builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path, cache_ttl="1h")
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


def _run_turn(core: AgentCore, client: _FakeClient, text: str, started_at: str) -> None:
    prepared = core._prepare_turn_attempt(
        text,
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata={"turn_processing_started_at": started_at},
    )
    _run_responder(
        client=client,
        messages=prepared.messages,
        tools=[],
        conversation=core.conversation,
        builder=core.builder,
        registry=core.registry,
        console=core.console,
        max_iterations=5,
    )


def _text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.text or "" for part in message.content or [])


def _user_text_containing(messages: list[Message], marker: str) -> str:
    return next(_text(m) for m in messages if m.role == "user" and marker in _text(m))


def _anthropic_user_payload(messages: list[Message], marker: str) -> dict:
    """The user message as sent on the Anthropic wire, without cache markers."""
    _, converted = convert_messages(messages, AnthropicMessagePayload, replay_thinking=True)
    for payload in AnthropicClient._serialize_messages(converted):
        blocks = payload["content"]
        if payload["role"] == "user" and any(marker in (b.get("text") or "") for b in blocks):
            return {
                "role": payload["role"],
                "content": [
                    {k: v for k, v in block.items() if k != "cache_control"}
                    for block in blocks
                ],
            }
    raise AssertionError(f"no user payload containing {marker!r}")


def test_overlay_is_stored_on_the_turn_entry_and_stable_across_rounds(tmp_path: Path):
    core, note_store, client = _make_core(tmp_path)
    # A prior turn without an overlay must render unchanged.
    core.conversation.add("user", "earlier turn", channel="cli", sender="tester")
    core.conversation.add("assistant", "earlier reply")

    _run_turn(core, client, "hello", "2026-03-12T09:11:00+08:00")

    assert len(client.calls) == 2
    # The tool call between the rounds really changed the note store.
    assert note_store.get("location").value == "changed-mid-turn"
    for call_messages in client.calls:
        latest = _user_text_containing(call_messages, "hello")
        assert latest.count("[Agent Notes]") == 1
        assert latest.count("[Runtime Context]") == 1
        assert 'location: "original"' in latest
        assert "changed-mid-turn" not in latest
        earlier = _user_text_containing(call_messages, "earlier turn")
        assert "[Agent Notes]" not in earlier
        assert "[Runtime Context]" not in earlier
    assert _user_text_containing(client.calls[0], "hello") == _user_text_containing(
        client.calls[1], "hello"
    )

    # The overlay lives in the entry's metadata; the message text stays raw.
    entry = next(e for e in core.conversation.get_messages() if e.content == "hello")
    assert "[Agent Notes]" in entry.metadata[TURN_OVERLAY_METADATA_KEY]
    assert set(entry.metadata[AGENT_NOTES_SHOWN_METADATA_KEY]) == {"location", "mood"}


def test_next_turn_keeps_earlier_overlay_byte_identical_and_shows_only_changes(
    tmp_path: Path,
):
    core, _, client = _make_core(tmp_path)

    _run_turn(core, client, "first turn", "2026-03-12T09:11:00+08:00")
    first_turn_request = client.calls[-1]
    _run_turn(core, client, "second turn", "2026-03-12T09:20:00+08:00")
    second_turn_request = client.calls[-1]

    assert _anthropic_user_payload(second_turn_request, "first turn") == (
        _anthropic_user_payload(first_turn_request, "first turn")
    )

    second = _user_text_containing(second_turn_request, "second turn")
    notes_block = second[second.index("[Agent Notes]"):]
    assert 'location: "changed-mid-turn"' in notes_block
    assert "mood:" not in notes_block
    assert "current_local_time: 2026-03-12 (Thu) 09:20" in second


def test_overlay_absent_when_nothing_applies(tmp_path: Path):
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

    core._prepare_turn_attempt(
        "hi",
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata=None,
    )

    assert core.conversation.get_messages()[-1].metadata is None
