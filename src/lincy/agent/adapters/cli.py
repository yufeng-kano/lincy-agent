"""Textual-backed CLI channel adapter for chat-cli."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ..schema import InboundMessage, OutboundMessage
from ...tui.events import ProcessingFinishedEvent

if TYPE_CHECKING:
    from ...tui.sink import UiSink
    from ...tui.controller import TurnCancelController

    from ..core import AgentCore
    from ...cli.commands import CommandHandler
    from ...context import Conversation, ContextBuilder
    from ...session import SessionManager
    from ...workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class CLIAdapter:
    """CLI channel adapter that receives input from a Textual UI controller.

    This adapter no longer owns terminal input rendering. It only:
    - validates/submits user text to the agent queue
    - executes slash commands locally
    - provides history rollback/reuse data for the UI (`Ctrl+R`)
    - emits turn-complete signals back to the UI via `UiSink`
    """

    channel_name = "cli"
    priority = 0

    def __init__(
        self,
        *,
        ui_sink: UiSink,
        commands: CommandHandler,
        session_mgr: SessionManager,
        conversation: Conversation,
        builder: ContextBuilder,
        workspace: WorkspaceManager,
        agent_os_dir: Path,
        user_id: str,
        display_name: str,
        cancel_controller: TurnCancelController | None = None,
    ) -> None:
        self._ui_sink = ui_sink
        self._commands = commands
        self._session_mgr = session_mgr
        self._conversation = conversation
        self._builder = builder
        self._workspace = workspace
        self._agent_os_dir = agent_os_dir
        self._user_id = user_id
        self._display_name = display_name
        self._cancel = cancel_controller

        self._agent: AgentCore | None = None
        self._turn_done = threading.Event()
        self._turn_done.set()

    # ------------------------------------------------------------------
    # ChannelAdapter protocol
    # ------------------------------------------------------------------

    def start(self, agent: AgentCore) -> None:
        self._agent = agent
        self._turn_done.set()

    def send(self, message: OutboundMessage) -> None:
        # Display is handled by agent/console. Future CLI delivery hooks can use this.
        pass

    def on_turn_start(self, channel: str) -> None:
        # No terminal suspension needed; Textual is the single renderer.
        if channel == self.channel_name and self._cancel is not None:
            self._cancel.begin_turn()

    def on_turn_complete(self) -> None:
        interrupted = False
        if self._cancel is not None:
            interrupted = self._cancel.phase in {"requested", "pending", "acknowledged"}
            if interrupted:
                self._cancel.acknowledge()
                self._cancel.complete()
            else:
                self._cancel.reset()
        self._ui_sink.emit(ProcessingFinishedEvent(channel=self.channel_name, interrupted=interrupted))
        self._turn_done.set()

    def stop(self) -> None:
        self._turn_done.set()

    # ------------------------------------------------------------------
    # Textual UI integration
    # ------------------------------------------------------------------

    def submit_input(self, raw_text: str) -> bool:
        """Handle text submitted by the Textual UI.

        Returns True when the process should exit (e.g. /quit).
        """
        try:
            return self._submit(raw_text, remote=False)
        except RuntimeError as exc:
            self._commands._console.print_warning(str(exc))
            return False

    def submit_remote(self, raw_text: str) -> bool:
        """Handle text submitted by the web remote-TUI.

        Same semantics as ``submit_input``, but rejects with RuntimeError
        instead of only printing a warning so the control API can return 409.
        """
        return self._submit(raw_text, remote=True)

    def _submit(self, raw_text: str, *, remote: bool) -> bool:
        assert self._agent is not None

        user_input = raw_text.strip()
        if not user_input:
            return False

        if self._commands.is_command(user_input):
            if (
                not self._turn_done.is_set()
                and not self._commands.can_execute_while_processing(user_input)
            ):
                message = "Still processing the previous turn."
                if remote:
                    raise RuntimeError(message)
                self._commands._console.print_warning(message)
                return False
            should_stop = self._handle_command(user_input)
            return should_stop

        if not self._turn_done.is_set():
            message = "Still processing the previous turn."
            if remote:
                raise RuntimeError(message)
            self._commands._console.print_warning(message)
            return False

        msg = InboundMessage(
            channel="cli",
            content=user_input,
            priority=self.priority,
            sender=self._user_id,
        )
        self._turn_done.clear()
        self._agent.enqueue(msg)
        return False

    def select_recent_input(self) -> str | None:
        """Rollback to a recent user turn and return prefill text for editing."""
        return self.select_recent_input_by_index(0)

    def list_recent_inputs(self, limit: int = 10) -> list[str]:
        """Return recent user turn previews (most recent first)."""
        msgs = self._conversation.get_messages()
        user_turns = [(i, m) for i, m in enumerate(msgs) if m.role == "user"]
        if not user_turns:
            return []

        previews: list[str] = []
        for _idx, msg in reversed(user_turns[-limit:]):
            content = msg.content or ""
            if isinstance(content, list):
                preview = "[non-text message]"
            else:
                preview = content.replace("\n", " ").strip()
                if not preview:
                    preview = "[empty]"
            previews.append(preview)
        return previews

    def select_recent_input_by_index(self, choice: int, limit: int = 10) -> str | None:
        """Rollback to a selected recent user turn and return prefill text."""
        msgs = self._conversation.get_messages()
        user_turns = [(i, m) for i, m in enumerate(msgs) if m.role == "user"]
        if not user_turns:
            self._commands._console.print_info("No user history to reuse.")
            return None

        recent = list(reversed(user_turns[-limit:]))  # most recent first
        if choice < 0 or choice >= len(recent):
            self._commands._console.print_warning("History selection out of range.")
            return None

        selected_idx, selected_msg = recent[choice]
        prev_input = selected_msg.content or ""
        if isinstance(prev_input, list):
            # History reuse only supports text user messages.
            return None

        self._conversation.truncate_to(selected_idx)
        self._session_mgr.rewrite_messages(self._conversation.get_messages())
        self._commands._console.print_info("Rolled back to selected previous input.")
        return prev_input

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_command(self, text: str) -> bool:
        from ...cli.commands import CommandResult

        assert self._agent is not None
        result = self._commands.execute(text)

        if result == CommandResult.EXIT:
            self._session_mgr.finalize("exited")
            self._commands._console.print_goodbye()
            self._agent.request_shutdown(graceful=False)
            return True

        if result == CommandResult.CLEAR:
            self._conversation.clear()
        elif result == CommandResult.COMPACT:
            compact_result = self._agent.run_manual_compact()
            if compact_result.changed:
                via = (
                    f" via {compact_result.source_label}"
                    if compact_result.source_label
                    else ""
                )
                if compact_result.removed_messages > 0:
                    self._commands._console.print_info(
                        "Context compacted"
                        f"{via}: {compact_result.removed_messages} messages removed."
                    )
                else:
                    self._commands._console.print_info(
                        f"Context compacted{via}."
                    )
            else:
                self._commands._console.print_info("Context is already compact.")
        elif result == CommandResult.RELOAD_RESOURCES:
            self._agent.request_reload()
        elif result == CommandResult.RELOAD_SYSTEM_PROMPT:
            self._agent.request_reload_system_prompt()

        return False
