"""Tests for CLIAdapter slash command handling."""

import threading
from unittest.mock import MagicMock

from lincy.agent.adapters.cli import CLIAdapter
from lincy.agent.core import ContextCompactionResult
from lincy.cli.commands import CommandResult


def test_reload_resources_command_enqueues_reload_request(tmp_path):
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._commands = MagicMock()
    adapter._commands.execute.return_value = CommandResult.RELOAD_RESOURCES
    adapter._commands._console = MagicMock()
    adapter._builder = MagicMock()
    adapter._workspace = MagicMock()
    adapter._agent_os_dir = tmp_path
    adapter._session_mgr = MagicMock()
    adapter._conversation = MagicMock()
    adapter._user_id = "u"
    adapter._display_name = "User"

    should_stop = adapter._handle_command("/reload")

    assert should_stop is False
    adapter._agent.request_reload.assert_called_once_with()
    adapter._builder.update_system_prompt.assert_not_called()
    adapter._builder.reload_boot_files.assert_not_called()


def test_reload_system_prompt_command_enqueues_prompt_only_request(tmp_path):
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._commands = MagicMock()
    adapter._commands.execute.return_value = CommandResult.RELOAD_SYSTEM_PROMPT
    adapter._commands._console = MagicMock()
    adapter._builder = MagicMock()
    adapter._workspace = MagicMock()
    adapter._agent_os_dir = tmp_path
    adapter._session_mgr = MagicMock()
    adapter._conversation = MagicMock()
    adapter._user_id = "u"
    adapter._display_name = "User"

    should_stop = adapter._handle_command("/reload system-prompt")

    assert should_stop is False
    adapter._agent.request_reload_system_prompt.assert_called_once_with()
    adapter._builder.update_system_prompt.assert_not_called()
    adapter._builder.reload_boot_files.assert_not_called()


def test_submit_input_allows_shell_command_while_turn_busy():
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._commands = MagicMock()
    adapter._commands.is_command.return_value = True
    adapter._commands.can_execute_while_processing.return_value = True
    adapter._commands._console = MagicMock()
    adapter._handle_command = MagicMock(return_value=False)
    adapter._turn_done = threading.Event()
    adapter._turn_done.clear()

    result = adapter.submit_input("/shell-status")

    assert result is False
    adapter._handle_command.assert_called_once_with("/shell-status")
    adapter._commands._console.print_warning.assert_not_called()


def test_compact_command_delegates_to_agent(tmp_path):
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._agent.run_manual_compact.return_value = ContextCompactionResult(
        changed=True,
        removed_messages=3,
        source="codex_remote",
        trigger="manual",
    )
    adapter._commands = MagicMock()
    adapter._commands.execute.return_value = CommandResult.COMPACT
    adapter._commands._console = MagicMock()
    adapter._builder = MagicMock()
    adapter._workspace = MagicMock()
    adapter._agent_os_dir = tmp_path
    adapter._session_mgr = MagicMock()
    adapter._conversation = MagicMock()
    adapter._user_id = "u"
    adapter._display_name = "User"

    should_stop = adapter._handle_command("/compact")

    assert should_stop is False
    adapter._agent.run_manual_compact.assert_called_once_with()
    adapter._commands._console.print_info.assert_called_once_with(
        "Context compacted via codex remote: 3 messages removed."
    )


def test_submit_remote_raises_when_turn_busy():
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._commands = MagicMock()
    adapter._commands.is_command.return_value = False
    adapter._commands._console = MagicMock()
    adapter._user_id = "u"
    adapter.priority = 0
    adapter._turn_done = threading.Event()
    adapter._turn_done.clear()

    try:
        adapter.submit_remote("hello")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "processing" in str(exc).lower()

    assert raised
    adapter._agent.enqueue.assert_not_called()


def test_submit_remote_enqueues_cli_message():
    adapter = CLIAdapter.__new__(CLIAdapter)
    adapter._agent = MagicMock()
    adapter._commands = MagicMock()
    adapter._commands.is_command.return_value = False
    adapter._user_id = "u"
    adapter.priority = 0
    adapter._turn_done = threading.Event()
    adapter._turn_done.set()

    result = adapter.submit_remote("  hello  ")

    assert result is False
    assert not adapter._turn_done.is_set()
    msg = adapter._agent.enqueue.call_args.args[0]
    assert msg.channel == "cli"
    assert msg.content == "hello"
    assert msg.sender == "u"
