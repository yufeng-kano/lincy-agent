"""Tests for _patch_interrupted_tool_calls."""

from lincy.agent.core import _patch_interrupted_tool_calls
from lincy.context import Conversation
from lincy.llm.schema import ToolCall


def _make_conversation(*specs):
    """Build a Conversation from (role, content, tool_calls?, tool_call_id?, name?) tuples."""
    conv = Conversation()
    for spec in specs:
        role = spec[0]
        content = spec[1]
        if role == "assistant" and len(spec) > 2 and spec[2]:
            conv.add_assistant_with_tools(content, spec[2])
        elif role == "tool":
            tool_call_id = spec[2] if len(spec) > 2 else None
            name = spec[3] if len(spec) > 3 else None
            conv.add_tool_result(tool_call_id, name, content)
        else:
            conv.add(role, content)
    return conv


class TestPatchInterruptedToolCalls:

    def test_no_assistant_message_returns_zero(self):
        conv = _make_conversation(
            ("user", "hello"),
        )
        assert _patch_interrupted_tool_calls(conv, 0) == 0

    def test_all_results_present_returns_zero(self):
        tc = ToolCall(id="tc1", name="foo", arguments={})
        conv = _make_conversation(
            ("user", "hello"),
            ("assistant", None, [tc]),
            ("tool", "result", "tc1", "foo"),
        )
        assert _patch_interrupted_tool_calls(conv, 0) == 0
        assert len(conv.get_messages()) == 3

    def test_missing_results_adds_placeholders(self):
        tc1 = ToolCall(id="tc1", name="foo", arguments={})
        tc2 = ToolCall(id="tc2", name="bar", arguments={})
        conv = _make_conversation(
            ("user", "hello"),
            ("assistant", None, [tc1, tc2]),
            ("tool", "result1", "tc1", "foo"),
        )
        added = _patch_interrupted_tool_calls(conv, 0)
        assert added == 1
        msgs = conv.get_messages()
        assert len(msgs) == 4
        assert msgs[3].role == "tool"
        assert msgs[3].tool_call_id == "tc2"
        assert msgs[3].content == "[Interrupted by user]"

    def test_all_results_missing(self):
        tc1 = ToolCall(id="tc1", name="foo", arguments={})
        tc2 = ToolCall(id="tc2", name="bar", arguments={})
        conv = _make_conversation(
            ("user", "hello"),
            ("assistant", None, [tc1, tc2]),
        )
        added = _patch_interrupted_tool_calls(conv, 0)
        assert added == 2
        msgs = conv.get_messages()
        assert len(msgs) == 4
        assert msgs[2].content == "[Interrupted by user]"
        assert msgs[3].content == "[Interrupted by user]"

    def test_since_skips_earlier_messages(self):
        tc_early = ToolCall(id="tc0", name="early", arguments={})
        tc_late = ToolCall(id="tc1", name="late", arguments={})
        conv = _make_conversation(
            ("user", "first"),
            ("assistant", None, [tc_early]),
            ("tool", "done", "tc0", "early"),
            ("user", "second"),
            ("assistant", None, [tc_late]),
        )
        # since=3 means only look from index 3 onwards
        added = _patch_interrupted_tool_calls(conv, 3)
        assert added == 1
        msgs = conv.get_messages()
        assert msgs[-1].tool_call_id == "tc1"

    def test_no_tool_calls_in_assistant_message(self):
        conv = _make_conversation(
            ("user", "hello"),
            ("assistant", "just text"),
        )
        assert _patch_interrupted_tool_calls(conv, 0) == 0

    def test_interrupt_before_any_llm_call(self):
        """Interrupt right after user message, no assistant response yet."""
        conv = _make_conversation(
            ("user", "hello"),
        )
        since = len(conv.get_messages())
        assert _patch_interrupted_tool_calls(conv, since) == 0
