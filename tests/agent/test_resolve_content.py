"""Tests for content resolution helpers in agent core."""

from lincy.agent.run_helpers import (
    _latest_nonempty_assistant_content,
    _latest_intermediate_text,
    _resolve_final_content,
)
from lincy.llm.schema import Message, ToolCall


def _tc(name: str = "memory_edit") -> ToolCall:
    return ToolCall(id="tc_1", name=name, arguments={})


# -- _latest_nonempty_assistant_content --


class TestLatestNonemptyAssistantContent:
    def test_finds_pure_assistant_message(self):
        msgs = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ]
        assert _latest_nonempty_assistant_content(msgs) == "hello"

    def test_skips_messages_with_tool_calls(self):
        msgs = [
            Message(role="assistant", content="thinking...", tool_calls=[_tc()]),
        ]
        assert _latest_nonempty_assistant_content(msgs) == ""

    def test_skips_empty_content(self):
        msgs = [
            Message(role="assistant", content=""),
            Message(role="assistant", content="   "),
        ]
        assert _latest_nonempty_assistant_content(msgs) == ""

    def test_returns_latest(self):
        msgs = [
            Message(role="assistant", content="first"),
            Message(role="assistant", content="second"),
        ]
        assert _latest_nonempty_assistant_content(msgs) == "second"

    def test_empty_list(self):
        assert _latest_nonempty_assistant_content([]) == ""


# -- _latest_intermediate_text --


class TestLatestIntermediateText:
    def test_finds_text_from_tool_call_message(self):
        msgs = [
            Message(role="assistant", content="updating memory", tool_calls=[_tc()]),
        ]
        assert _latest_intermediate_text(msgs) == "updating memory"

    def test_skips_pure_assistant_messages(self):
        msgs = [
            Message(role="assistant", content="hello"),
        ]
        assert _latest_intermediate_text(msgs) == ""

    def test_skips_empty_content(self):
        msgs = [
            Message(role="assistant", content="", tool_calls=[_tc()]),
        ]
        assert _latest_intermediate_text(msgs) == ""

    def test_returns_latest(self):
        msgs = [
            Message(role="assistant", content="first", tool_calls=[_tc()]),
            Message(role="assistant", content="second", tool_calls=[_tc()]),
        ]
        assert _latest_intermediate_text(msgs) == "second"

    def test_empty_list(self):
        assert _latest_intermediate_text([]) == ""


# -- _resolve_final_content --


class TestResolveFinalContent:
    def test_uses_response_content_when_present(self):
        content, is_fallback = _resolve_final_content("direct reply", [])
        assert content == "direct reply"
        assert is_fallback is False

    def test_falls_back_to_pure_assistant(self):
        msgs = [Message(role="assistant", content="pure text")]
        content, is_fallback = _resolve_final_content("", msgs)
        assert content == "pure text"
        assert is_fallback is True

    def test_falls_back_to_intermediate_text(self):
        """Key bug scenario: only intermediate text exists."""
        msgs = [
            Message(role="assistant", content="reply with tool", tool_calls=[_tc()]),
            Message(role="tool", content="ok", tool_call_id="tc_1", name="memory_edit"),
        ]
        content, is_fallback = _resolve_final_content("", msgs)
        assert content == "reply with tool"
        assert is_fallback is True

    def test_falls_back_to_intermediate_when_response_none(self):
        msgs = [
            Message(role="assistant", content="doing stuff", tool_calls=[_tc()]),
        ]
        content, is_fallback = _resolve_final_content(None, msgs)
        assert content == "doing stuff"
        assert is_fallback is True

    def test_pure_assistant_preferred_over_intermediate(self):
        msgs = [
            Message(role="assistant", content="intermediate", tool_calls=[_tc()]),
            Message(role="assistant", content="pure"),
        ]
        content, is_fallback = _resolve_final_content("", msgs)
        assert content == "pure"
        assert is_fallback is True

    def test_returns_empty_when_nothing_found(self):
        msgs = [Message(role="user", content="hi")]
        content, is_fallback = _resolve_final_content("", msgs)
        assert content == ""
        assert is_fallback is False

    def test_whitespace_response_treated_as_empty(self):
        msgs = [
            Message(role="assistant", content="fallback", tool_calls=[_tc()]),
        ]
        content, is_fallback = _resolve_final_content("  \n  ", msgs)
        assert content == "fallback"
        assert is_fallback is True

    def test_empty_messages_empty_response(self):
        content, is_fallback = _resolve_final_content("", [])
        assert content == ""
        assert is_fallback is False
