from enum import Enum
from typing import TYPE_CHECKING, Callable

from ..agent.ui_event_console import AgentUiPort

if TYPE_CHECKING:
    from ..tools.builtin.shell_task import ShellTaskManager


class CommandResult(Enum):
    """Result of command execution."""
    CONTINUE = "continue"  # Continue chat loop
    EXIT = "exit"  # Exit immediately without saving
    CLEAR = "clear"  # Clear conversation history
    COMPACT = "compact"  # Compact context (keep recent turns)
    RELOAD_RESOURCES = "reload_resources"  # Reload prompt + boot resources
    RELOAD_SYSTEM_PROMPT = "reload_system_prompt"  # Reload system prompt from disk


class CommandHandler:
    """Handler for slash commands."""

    def __init__(self, console: AgentUiPort) -> None:
        self._console = console
        self._shell_task_manager: ShellTaskManager | None = None
        self._commands: dict[str, tuple[Callable[[str], CommandResult], str]] = {
            "/help": (self._help, "Show available commands"),
            "/clear": (self._clear, "Clear conversation history"),
            "/compact": (self._compact, "Compact context (keep recent turns)"),
            "/exit": (self._exit, "Exit immediately (no save)"),
            "/reload": (self._reload, "Reload prompt and boot resources"),
            "/shell-status": (self._shell_status, "Show active shell session state"),
            "/shell-input": (self._shell_input, "Send text to a waiting shell session"),
            "/shell-enter": (self._shell_enter, "Send Enter to a waiting shell session"),
            "/shell-up": (self._shell_up, "Send Up to a waiting shell session"),
            "/shell-down": (self._shell_down, "Send Down to a waiting shell session"),
            "/shell-left": (self._shell_left, "Send Left to a waiting shell session"),
            "/shell-right": (self._shell_right, "Send Right to a waiting shell session"),
            "/shell-tab": (self._shell_tab, "Send Tab to a waiting shell session"),
            "/shell-esc": (self._shell_escape, "Send Escape to a waiting shell session"),
            "/shell-cancel": (self._shell_cancel, "Cancel an active shell session"),
        }

    def set_shell_task_manager(self, manager: "ShellTaskManager") -> None:
        """Attach shell session control hooks."""
        self._shell_task_manager = manager

    def can_execute_while_processing(self, text: str) -> bool:
        """Return whether the slash command is safe during an active turn."""
        cmd = text.split(maxsplit=1)[0].lower()
        return cmd in {
            "/help",
            "/shell-status",
            "/shell-input",
            "/shell-enter",
            "/shell-up",
            "/shell-down",
            "/shell-left",
            "/shell-right",
            "/shell-tab",
            "/shell-esc",
            "/shell-cancel",
        }

    def is_command(self, text: str) -> bool:
        """Check if text is a slash command."""
        return text.startswith("/")

    def execute(self, text: str) -> CommandResult:
        """Execute a slash command."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        if cmd in self._commands:
            handler, _ = self._commands[cmd]
            return handler(args)
        else:
            self._console.print_error(f"Unknown command: {cmd}")
            self._console.print_info("Type /help for available commands.")
            return CommandResult.CONTINUE

    def _help(self, _args: str) -> CommandResult:
        """Show help message."""
        self._console.print_info("\nAvailable commands:")
        for cmd, (_, desc) in self._commands.items():
            self._console.print_info(f"  {cmd:10} - {desc}")
        self._console.print_info("")
        return CommandResult.CONTINUE

    def _clear(self, _args: str) -> CommandResult:
        """Clear conversation - returns CLEAR to signal app to reset."""
        self._console.print_info("Conversation cleared.\n")
        return CommandResult.CLEAR

    def _compact(self, _args: str) -> CommandResult:
        """Compact context by keeping only recent turns."""
        return CommandResult.COMPACT

    def _exit(self, _args: str) -> CommandResult:
        """Exit immediately without saving."""
        return CommandResult.EXIT

    def _reload(self, args: str) -> CommandResult:
        """Reload resources."""
        if not args or args == "all":
            return CommandResult.RELOAD_RESOURCES
        if args == "system-prompt":
            return CommandResult.RELOAD_SYSTEM_PROMPT
        self._console.print_error(f"Unknown reload target: {args}")
        self._console.print_info("Usage: /reload [all|system-prompt]")
        return CommandResult.CONTINUE

    def _shell_status(self, args: str) -> CommandResult:
        """Show shell session status."""
        manager = self._shell_task_manager
        if manager is None:
            self._console.print_warning("Shell session manager is not available.")
            return CommandResult.CONTINUE
        session_id = args or None
        self._console.print_info(manager.format_status(session_id))
        return CommandResult.CONTINUE

    def _shell_input(self, args: str) -> CommandResult:
        """Send text input to a waiting shell session."""
        manager = self._shell_task_manager
        if manager is None:
            self._console.print_warning("Shell session manager is not available.")
            return CommandResult.CONTINUE
        if not args:
            self._console.print_info("Usage: /shell-input [session_id] <text>")
            return CommandResult.CONTINUE

        session_id = None
        payload = args
        first, sep, rest = args.partition(" ")
        if first.startswith("sh_") and sep:
            session_id = first
            payload = rest

        result = manager.send_input(payload, session_id=session_id)
        if result.startswith("Error"):
            self._console.print_error(result)
        else:
            self._console.print_info(result)
        return CommandResult.CONTINUE

    def _shell_enter(self, args: str) -> CommandResult:
        """Send Enter to a waiting shell session."""
        manager = self._shell_task_manager
        if manager is None:
            self._console.print_warning("Shell session manager is not available.")
            return CommandResult.CONTINUE

        session_id = args or None
        result = manager.send_enter(session_id=session_id)
        if result.startswith("Error"):
            self._console.print_error(result)
        else:
            self._console.print_info(result)
        return CommandResult.CONTINUE

    def _shell_cancel(self, args: str) -> CommandResult:
        """Cancel an active shell session."""
        manager = self._shell_task_manager
        if manager is None:
            self._console.print_warning("Shell session manager is not available.")
            return CommandResult.CONTINUE

        session_id = args or None
        result = manager.cancel_session(session_id=session_id)
        if result.startswith("Error"):
            self._console.print_error(result)
        else:
            self._console.print_info(result)
        return CommandResult.CONTINUE

    def _shell_up(self, args: str) -> CommandResult:
        """Send Up to a waiting shell session."""
        return self._run_shell_control(args, "send_up")

    def _shell_down(self, args: str) -> CommandResult:
        """Send Down to a waiting shell session."""
        return self._run_shell_control(args, "send_down")

    def _shell_left(self, args: str) -> CommandResult:
        """Send Left to a waiting shell session."""
        return self._run_shell_control(args, "send_left")

    def _shell_right(self, args: str) -> CommandResult:
        """Send Right to a waiting shell session."""
        return self._run_shell_control(args, "send_right")

    def _shell_tab(self, args: str) -> CommandResult:
        """Send Tab to a waiting shell session."""
        return self._run_shell_control(args, "send_tab")

    def _shell_escape(self, args: str) -> CommandResult:
        """Send Escape to a waiting shell session."""
        return self._run_shell_control(args, "send_escape")

    def _run_shell_control(
        self,
        args: str,
        method_name: str,
    ) -> CommandResult:
        """Run a one-shot shell session control command."""
        manager = self._shell_task_manager
        if manager is None:
            self._console.print_warning("Shell session manager is not available.")
            return CommandResult.CONTINUE

        session_id = args or None
        method = getattr(manager, method_name)
        result = method(session_id=session_id)
        if result.startswith("Error"):
            self._console.print_error(result)
        else:
            self._console.print_info(result)
        return CommandResult.CONTINUE
