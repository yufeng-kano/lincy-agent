"""Tests for Conversation mutation helpers."""

from datetime import datetime

from lincy.context import Conversation
from lincy.context.conversation import split_turns
from lincy.llm.schema import Message, ToolCall


def test_split_turns_groups_non_user_messages():
    messages = [
        Message(role="user", content="u1"),
        Message(role="assistant", content="a1"),
        Message(role="tool", content="t1"),
        Message(role="user", content="u2"),
        Message(role="assistant", content="a2"),
    ]

    turns = split_turns(messages)

    assert [[message.role for message in turn] for turn in turns] == [
        ["user", "assistant", "tool"],
        ["user", "assistant"],
    ]


def test_replace_messages_restores_history_without_callback():
    seen = []
    original = Conversation()
    original.add("user", "hi")
    original.add("assistant", "hello")

    restored = Conversation(on_message=seen.append)
    restored.replace_messages(original.get_messages())

    assert len(restored.get_messages()) == 2
    assert seen == []


def test_truncate_to_keeps_prefix_and_returns_removed_count():
    conversation = Conversation()
    conversation.add("user", "u1")
    conversation.add("assistant", "a1")
    conversation.add("user", "u2")

    removed = conversation.truncate_to(2)

    assert removed == 1
    assert [entry.content for entry in conversation.get_messages()] == ["u1", "a1"]


def test_truncate_to_noops_when_length_is_large_enough():
    conversation = Conversation()
    conversation.add("user", "u1")

    removed = conversation.truncate_to(5)

    assert removed == 0
    assert len(conversation.get_messages()) == 1


def test_set_on_message_updates_future_callback():
    seen = []
    conversation = Conversation()

    conversation.set_on_message(seen.append)
    conversation.add("user", "hello")

    assert len(seen) == 1
    assert seen[0].content == "hello"


def test_len_tracks_current_history_size():
    conversation = Conversation()
    assert len(conversation) == 0

    conversation.add("user", "first", timestamp=datetime(2024, 1, 2, 3, 4, 5))
    conversation.add("assistant", "second")
    assert len(conversation) == 2

    conversation.truncate_to(1)
    assert len(conversation) == 1


def _tool_call(call_id: str, name: str = "echo") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"text": "x"})


class TestRemoveDanglingToolCalls:
    def test_noop_on_complete_history(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_assistant_with_tools(None, [_tool_call("t1")])
        conversation.add_tool_result("t1", "echo", "ok")
        conversation.add("assistant", "done")

        assert conversation.remove_dangling_tool_calls() == 0
        assert len(conversation.get_messages()) == 4

    def test_removes_contentless_assistant_with_no_results(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_assistant_with_tools(None, [_tool_call("t1")])

        assert conversation.remove_dangling_tool_calls() == 1
        entries = conversation.get_messages()
        assert len(entries) == 1
        assert entries[0].role == "user"

    def test_keeps_assistant_text_when_dropping_calls(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_assistant_with_tools("thinking out loud", [_tool_call("t1")])

        assert conversation.remove_dangling_tool_calls() == 1
        entries = conversation.get_messages()
        assert len(entries) == 2
        assert entries[1].content == "thinking out loud"
        assert entries[1].message.tool_calls is None

    def test_partial_results_keep_completed_calls(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_assistant_with_tools(
            None, [_tool_call("t1"), _tool_call("t2")]
        )
        conversation.add_tool_result("t1", "echo", "ok")

        assert conversation.remove_dangling_tool_calls() == 1
        entries = conversation.get_messages()
        assert len(entries) == 3
        kept_calls = entries[1].message.tool_calls
        assert [tc.id for tc in kept_calls] == ["t1"]
        assert entries[2].message.tool_call_id == "t1"

    def test_removes_orphaned_tool_result(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_tool_result("ghost", "echo", "ok")

        assert conversation.remove_dangling_tool_calls() == 1
        entries = conversation.get_messages()
        assert len(entries) == 1
        assert entries[0].role == "user"

    def test_later_turns_untouched(self):
        conversation = Conversation()
        conversation.add("user", "hi")
        conversation.add_assistant_with_tools(None, [_tool_call("t1")])
        conversation.add("user", "second turn")
        conversation.add_assistant_with_tools(None, [_tool_call("t2")])
        conversation.add_tool_result("t2", "echo", "ok")
        conversation.add("assistant", "done")

        assert conversation.remove_dangling_tool_calls() == 1
        entries = conversation.get_messages()
        roles = [entry.role for entry in entries]
        assert roles == ["user", "user", "assistant", "tool", "assistant"]
        assert entries[2].message.tool_calls[0].id == "t2"
