"""Unit tests for the two-tier ContextCompactor routing logic."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.agent.compaction import ContextCompactor
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation


def _seed_turns(conv: Conversation, count: int) -> None:
    for i in range(count):
        conv.add("user", f"user-{i}")
        conv.add("assistant", f"assistant-{i}")


class _StubCompactorAgent:
    """Stub for the tier-1 CompactorAgent."""

    def __init__(self, *, raises: bool = False, summary: str = "distilled summary"):
        self.raises = raises
        self.summary = summary
        self.calls = 0

    def summarize(self, entries):
        self.calls += 1
        if self.raises:
            raise RuntimeError("compactor agent failed")
        return self.summary


def _make_core(
    *,
    turns: int = 4,
    compactor_agent=None,
):
    conversation = Conversation()
    _seed_turns(conversation, turns)
    builder = ContextBuilder(system_prompt="sys", preserve_turns=2)
    registry = MagicMock()
    registry.get_definitions.return_value = []
    session_mgr = MagicMock()
    return SimpleNamespace(
        conversation=conversation,
        builder=builder,
        registry=registry,
        session_mgr=session_mgr,
        compactor_agent=compactor_agent,
    )


def test_compactor_agent_routes_before_message_dropping():
    """Compactor agent available: tier 1 should run, not local message dropping."""
    agent = _StubCompactorAgent()
    core = _make_core(compactor_agent=agent)
    compactor = ContextCompactor(core)

    result = compactor.compact(preserve_turns=2, trigger="soft_limit")

    assert agent.calls == 1
    assert result.changed is True
    assert result.source == "compactor"
    assert result.fallback is False

    messages = core.conversation.get_messages()
    # 1 summary entry + 2 preserved turns (user+assistant each) = 5 entries.
    assert len(messages) == 5
    assert messages[0].content == "[Conversation summary before compaction]\ndistilled summary"
    assert messages[0].metadata == {"rendered_static": True}
    # Preserved turns stay verbatim, most recent turns kept.
    assert messages[1].content == "user-2"
    assert messages[-1].content == "assistant-3"

    core.session_mgr.rewrite_messages.assert_called_once_with(messages)
    core.session_mgr.record_compaction.assert_called_once_with(
        source="compactor", trigger="soft_limit", removed_messages=3, fallback=False,
    )


def test_compactor_exception_falls_through_to_local():
    agent = _StubCompactorAgent(raises=True)
    core = _make_core(compactor_agent=agent)
    compactor = ContextCompactor(core)

    result = compactor.compact(preserve_turns=2, trigger="soft_limit")

    assert agent.calls == 1
    assert result.source == "local_fallback"
    assert result.changed is True
    assert result.fallback is True

    messages = core.conversation.get_messages()
    user_count = sum(1 for m in messages if m.role == "user")
    assert user_count == 2
    core.session_mgr.record_compaction.assert_called_once_with(
        source="local_fallback", trigger="soft_limit", removed_messages=4, fallback=True,
    )


def test_nothing_to_compact_is_a_noop_without_crashing():
    """No tier has real work to do (already within preserve_turns): the turn
    must still complete cleanly."""
    agent = _StubCompactorAgent(raises=True)
    core = _make_core(turns=1, compactor_agent=agent)
    compactor = ContextCompactor(core)

    result = compactor.compact(preserve_turns=2, trigger="soft_limit")

    # Tier 1 sees len(turns) <= preserve_turns and returns unchanged without
    # ever calling the (failing) stub agent; that's not itself a failure.
    assert agent.calls == 0
    assert result.changed is False
    assert result.source == "compactor"
    assert result.fallback is False
    core.session_mgr.record_compaction.assert_not_called()


def test_compactor_only_result_satisfies_render_cache_contract():
    """Compacted entries must satisfy the render-cache invariants:
    replace_messages + clear_render_cache + rewrite_messages, with the summary
    entry marked rendered_static so the builder does not re-tag it."""
    agent = _StubCompactorAgent(summary="lessons + agreements + follow-ups")
    core = _make_core(compactor_agent=agent)
    compactor = ContextCompactor(core)

    core.builder.build(core.conversation)  # populate render cache before compaction
    assert core.builder._rendered_conv

    result = compactor.compact(preserve_turns=2, trigger="soft_limit")

    assert result.changed is True
    # Render cache cleared so the next build() re-renders against the new entries.
    assert core.builder._rendered_conv == []
    assert core.builder._rendered_conv_sources == []

    rendered = core.builder.build(core.conversation)
    # rendered[0] is the builder's own system-prompt prefix; the compacted
    # summary is the first conversation-tier message right after it.
    assert rendered[0].role == "system"
    assert rendered[1].role == "assistant"
    assert "lessons + agreements + follow-ups" in rendered[1].content
