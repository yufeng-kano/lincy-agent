"""Tests for context refresh: boot cache and refresh logic."""

from unittest.mock import MagicMock, patch

from lincy.context import ContextBuilder, Conversation


# ── Boot files cache ──────────────────────────────────────────────────


class TestBootFilesCache:
    def test_build_uses_cached_boot_content(self, tmp_path):
        """After reload_boot_files, build() should use cached content, not re-read disk."""
        mem_dir = tmp_path / "memory" / "agent"
        mem_dir.mkdir(parents=True)
        (mem_dir / "persona.md").write_text("original", encoding="utf-8")

        builder = ContextBuilder(
            system_prompt="System",
            agent_os_dir=tmp_path,
            boot_files=["memory/agent/persona.md"],
        )
        builder.reload_boot_files()

        # Modify file on disk after reload
        (mem_dir / "persona.md").write_text("modified", encoding="utf-8")

        conv = Conversation()
        conv.add("user", "hi")
        messages = builder.build(conv)

        boot_msgs = [m for m in messages if m.role == "system" and "[Core Rules]" in (m.content or "")]
        assert len(boot_msgs) == 1
        assert "original" in boot_msgs[0].content
        assert "modified" not in boot_msgs[0].content

    def test_reload_picks_up_disk_changes(self, tmp_path):
        """Calling reload_boot_files() again should pick up disk changes."""
        mem_dir = tmp_path / "memory" / "agent"
        mem_dir.mkdir(parents=True)
        (mem_dir / "persona.md").write_text("v1", encoding="utf-8")

        builder = ContextBuilder(
            system_prompt="System",
            agent_os_dir=tmp_path,
            boot_files=["memory/agent/persona.md"],
        )
        builder.reload_boot_files()

        # Modify and reload
        (mem_dir / "persona.md").write_text("v2", encoding="utf-8")
        builder.reload_boot_files()

        conv = Conversation()
        conv.add("user", "hi")
        messages = builder.build(conv)

        boot_msgs = [m for m in messages if m.role == "system" and "[Core Rules]" in (m.content or "")]
        assert "v2" in boot_msgs[0].content

    def test_no_boot_content_without_reload(self, tmp_path):
        """Without calling reload_boot_files, build() should not inject boot context."""
        mem_dir = tmp_path / "memory" / "agent"
        mem_dir.mkdir(parents=True)
        (mem_dir / "persona.md").write_text("data", encoding="utf-8")

        builder = ContextBuilder(
            system_prompt="System",
            agent_os_dir=tmp_path,
            boot_files=["memory/agent/persona.md"],
        )
        # Intentionally not calling reload_boot_files()

        conv = Conversation()
        conv.add("user", "hi")
        messages = builder.build(conv)

        boot_msgs = [m for m in messages if m.role == "system" and "[Core Rules]" in (m.content or "")]
        assert len(boot_msgs) == 0

    def test_update_system_prompt(self):
        """update_system_prompt should replace the system prompt in build output."""
        builder = ContextBuilder(system_prompt="old prompt")
        builder.update_system_prompt("new prompt")

        conv = Conversation()
        conv.add("user", "hi")
        messages = builder.build(conv)

        assert messages[0].content == "new prompt"


# ── _perform_context_refresh ──────────────────────────────────────────


class TestPerformContextRefresh:
    def test_compacts_conversation(self, tmp_path):
        """Refresh should compact conversation to preserve_turns."""
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.side_effect = FileNotFoundError
        core.builder = MagicMock()
        core.session_mgr = None
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        # Add 3 turns
        for i in range(3):
            core.conversation.add("user", f"msg{i}")
            core.conversation.add("assistant", f"resp{i}")

        assert len(core.conversation.get_messages()) == 6

        core._perform_context_refresh(preserve_turns=1)

        # Only 1 turn preserved (2 messages)
        assert len(core.conversation.get_messages()) == 2

    def test_reloads_boot_files(self, tmp_path):
        """Refresh should call builder.reload_boot_files()."""
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt {agent_os_dir}"
        core.builder = MagicMock()
        core.session_mgr = None
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_context_refresh(preserve_turns=0)

        core.builder.reload_boot_files.assert_called_once()

    def test_updates_system_prompt(self, tmp_path):
        """Refresh should re-resolve system prompt with agent_os_dir."""
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "path={agent_os_dir}"
        core.builder = MagicMock()
        core.session_mgr = None
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_context_refresh(preserve_turns=0)

        core.builder.update_system_prompt.assert_called_once_with(
            f"path={tmp_path}"
        )

    def test_rotates_session(self, tmp_path):
        """Refresh should finalize old session and create new one."""
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt"
        core.builder = MagicMock()
        core.session_mgr = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path
        core.user_id = "test_user"
        core.display_name = "Test"

        core._perform_context_refresh(preserve_turns=0)

        core.session_mgr.finalize.assert_called_once_with("refreshed")
        core.session_mgr.create.assert_called_once_with("test_user", "Test")

    def test_exception_does_not_propagate(self, tmp_path):
        """Refresh errors should be swallowed (logged, not raised)."""
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = MagicMock()
        core.conversation.compact.side_effect = RuntimeError("boom")
        core.workspace = MagicMock()
        core.builder = MagicMock()
        core.session_mgr = None
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        # Should not raise
        core._perform_context_refresh(preserve_turns=0)


class TestPerformReloadResources:
    def test_updates_system_prompt_and_reloads_boot_files(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt {agent_os_dir}"
        core.builder = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_reload_resources()

        core.builder.update_system_prompt.assert_called_once_with(
            f"prompt {tmp_path}"
        )
        core.builder.reload_boot_files.assert_called_once()

    def test_does_not_rotate_session(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt"
        core.builder = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path
        core.session_mgr = MagicMock()

        core._perform_reload_resources()

        core.session_mgr.finalize.assert_not_called()
        core.session_mgr.create.assert_not_called()

    def test_reports_missing_system_prompt_but_still_reloads_boot_files(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.side_effect = FileNotFoundError
        core.builder = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_reload_resources()

        core.builder.reload_boot_files.assert_called_once()
        core.console.print_warning.assert_called_once()

    def test_reports_reload_boot_file_failure_to_user(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt"
        core.builder = MagicMock()
        core.builder.reload_boot_files.side_effect = RuntimeError("boom")
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_reload_resources()

        core.console.print_error.assert_called_once_with("boom")


class TestPerformReloadSystemPrompt:
    def test_updates_only_system_prompt(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt {agent_os_dir}"
        core.builder = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_reload_system_prompt()

        core.builder.update_system_prompt.assert_called_once_with(
            f"prompt {tmp_path}"
        )
        core.builder.reload_boot_files.assert_not_called()
        core.console.print_info.assert_called_once_with("System prompt reloaded.")

    def test_reports_missing_file_to_user(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.side_effect = FileNotFoundError
        core.builder = MagicMock()
        core.console = MagicMock()
        core.agent_os_dir = tmp_path

        core._perform_reload_system_prompt()

        core.builder.update_system_prompt.assert_not_called()
        core.console.print_error.assert_called_once_with(
            "Failed to reload system prompt: file not found."
        )


class TestPerformNewSession:
    def test_archives_clears_conversation_and_rotates_session(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.conversation.add("user", "hello")
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt {agent_os_dir}"
        core.builder = MagicMock()
        core.session_mgr = MagicMock()
        core.console = MagicMock()
        core.turn_context = MagicMock()
        core.turn_context.clear = MagicMock()
        core.agent_os_dir = tmp_path
        core.user_id = "test_user"
        core.display_name = "Test"
        core.config = MagicMock()
        core.config.maintenance.archive = MagicMock()
        core._turns_since_memory_sync = 3

        with patch("lincy.agent.core._run_memory_archive") as mock_archive:
            core._perform_new_session()

        mock_archive.assert_called_once_with(
            tmp_path,
            core.config.maintenance.archive,
            core.console,
        )
        assert core.conversation.get_messages() == []
        core.turn_context.clear.assert_called_once()
        core.builder.update_system_prompt.assert_called_once_with(f"prompt {tmp_path}")
        core.builder.reload_boot_files.assert_called_once()
        core.session_mgr.finalize.assert_called_once_with("refreshed")
        core.session_mgr.create.assert_called_once_with("test_user", "Test")
        assert core._turns_since_memory_sync == 0

    def test_exception_does_not_propagate(self, tmp_path):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core.conversation = Conversation()
        core.workspace = MagicMock()
        core.workspace.get_system_prompt.return_value = "prompt"
        core.builder = MagicMock()
        core.builder.reload_boot_files.side_effect = RuntimeError("boom")
        core.session_mgr = MagicMock()
        core.console = MagicMock()
        core.turn_context = None
        core.agent_os_dir = tmp_path
        core.user_id = "test_user"
        core.display_name = "Test"
        core.config = MagicMock()
        core.config.maintenance.archive = MagicMock()
        core._turns_since_memory_sync = 1

        with patch("lincy.agent.core._run_memory_archive"):
            core._perform_new_session()
