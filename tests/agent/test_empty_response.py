"""Tests for empty response fallback mechanism."""

from unittest.mock import MagicMock

from lincy.agent.turn_runtime import _run_empty_response_fallback


def _make_deps(chat_response="fallback text"):
    """Create mocked dependencies for _run_empty_response_fallback."""
    client = MagicMock()
    client.chat.return_value = chat_response

    conversation = MagicMock()

    builder = MagicMock()
    builder.build.return_value = [
        MagicMock(role="system", content="sys"),
        MagicMock(role="user", content="hi"),
    ]

    console = MagicMock()

    return client, conversation, builder, console


class TestRunEmptyResponseFallback:
    def test_returns_llm_response(self):
        client, conv, builder, console = _make_deps("here is my reply")
        result = _run_empty_response_fallback(client, conv, builder, console)
        assert result == "here is my reply"

    def test_calls_chat_without_tools(self):
        client, conv, builder, console = _make_deps("ok")
        _run_empty_response_fallback(client, conv, builder, console)
        # chat() is called (not chat_with_tools)
        client.chat.assert_called_once()
        messages = client.chat.call_args[0][0]
        # Last message is the nudge
        assert "Your previous response was empty" in messages[-1].content

    def test_returns_empty_when_llm_returns_empty(self):
        client, conv, builder, console = _make_deps("")
        result = _run_empty_response_fallback(client, conv, builder, console)
        assert result == ""

    def test_returns_empty_when_llm_returns_whitespace(self):
        client, conv, builder, console = _make_deps("   \n  ")
        result = _run_empty_response_fallback(client, conv, builder, console)
        assert result == ""

    def test_returns_empty_when_llm_returns_none(self):
        client, conv, builder, console = _make_deps(None)
        result = _run_empty_response_fallback(client, conv, builder, console)
        assert result == ""

    def test_uses_spinner(self):
        client, conv, builder, console = _make_deps("ok")
        _run_empty_response_fallback(client, conv, builder, console)
        console.spinner.assert_called_once()

    def test_builds_local_messages(self):
        """Verify it uses builder.build() for a local copy, not modifying conversation."""
        client, conv, builder, console = _make_deps("ok")
        _run_empty_response_fallback(client, conv, builder, console)
        builder.build.assert_called_once_with(conv)
        # Conversation should not be modified
        conv.add.assert_not_called()
