"""Background shell task tool with optional human-handoff detection."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time
from pathlib import Path
from typing import Protocol

from ...core.schema import ShellHandoffConfig
from ...llm.schema import ToolDefinition, ToolParameter
from ..shell_handoff import ShellHandoffEvaluator
from ..shell_session import InteractiveShellSession, ShellSessionSnapshot
from ..security import clamp_timeout, check_shell_command, is_memory_write_shell_command

logger = logging.getLogger(__name__)
_DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 3.0

class WarningSink(Protocol):
    """Minimal UI sink contract needed by shell_task."""

    def emit(self, event) -> None:
        """Emit one UI event."""
        ...

SHELL_TASK_DEFINITION = ToolDefinition(
    name="shell_task",
    description=(
        "Start a background shell task and return immediately. "
        "Use this only when you can continue without the command output in this turn. "
        "The final result is delivered later as a [shell_task, from system] message. "
        "If the command later requires user action, the user may be prompted locally."
    ),
    parameters={
        "command": ToolParameter(
            type="string",
            description=(
                "The shell command to run in the background. "
                "Use execute_shell instead when you need the output in this turn."
            ),
        ),
        "timeout": ToolParameter(
            type="integer",
            description=(
                "Timeout in seconds for the background command. "
                "Clamped to at least the configured default; cannot lower it."
            ),
        ),
    },
    required=["command"],
)


class ShellTaskManager:
    """Own background shell session lifecycle, commands, and shutdown."""

    def __init__(
        self,
        *,
        max_concurrent: int = 2,
        shutdown_join_timeout: float = _DEFAULT_SHUTDOWN_JOIN_TIMEOUT_SECONDS,
        ui_sink: WarningSink | None = None,
    ) -> None:
        self._closing = threading.Event()
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._sessions: dict[str, InteractiveShellSession] = {}
        self._next_session_number = 1
        self._shutdown_join_timeout = shutdown_join_timeout
        self._ui_sink = ui_sink

    def is_closing(self) -> bool:
        """Return whether shutdown has started."""
        return self._closing.is_set()

    def try_acquire_slot(self) -> bool:
        """Reserve one background shell task slot."""
        return self._semaphore.acquire(blocking=False)

    def release_slot(self) -> None:
        """Release one background shell task slot."""
        self._semaphore.release()

    def allocate_session_id(self) -> str:
        """Allocate a new stable shell session identifier."""
        with self._lock:
            value = self._next_session_number
            self._next_session_number += 1
        return f"sh_{value:04d}"

    def start_session(self, session: InteractiveShellSession) -> bool:
        """Track and start a shell session unless shutdown has started."""
        with self._lock:
            if self._closing.is_set():
                return False
            self._sessions[session.session_id] = session
        try:
            session.start()
        except Exception:
            with self._lock:
                self._sessions.pop(session.session_id, None)
            raise
        return True

    def finish_session(self, session_id: str) -> None:
        """Forget a completed session and free one concurrency slot."""
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed is not None:
            self.release_slot()

    def enqueue_if_open(self, queue, msg) -> bool:
        """Queue a result only while background shell tasks remain open."""
        with self._lock:
            if self._closing.is_set():
                return False
        queue.put(msg)
        return True

    def emit_handoff_warning(
        self,
        *,
        session: InteractiveShellSession,
        state: str,
        snapshot: ShellSessionSnapshot,
    ) -> None:
        """Surface a shell handoff event directly to the UI."""
        action = (
            "Waiting for input. Use /shell-input, /shell-enter, /shell-up, /shell-down, /shell-left, /shell-right, /shell-tab, /shell-esc, or /shell-cancel."
            if state == "waiting_user_input"
            else "Waiting for external action. Complete the external step, then use /shell-status or wait."
        )
        lines = [
            f"[shell_task {snapshot.session_id}] {action}",
            f"Command: {session.command}",
        ]
        if snapshot.tail_lines:
            lines.append("Recent output:")
            lines.extend(f"  {line}" for line in snapshot.tail_lines[-4:])
        message = "\n".join(lines)
        if self._ui_sink is not None:
            from ...tui.events import WarningEvent

            self._ui_sink.emit(WarningEvent(message=message))
        else:
            logger.warning("%s", message)

    def format_status(self, session_id: str | None = None) -> str:
        """Render current shell session status for slash commands."""
        session, error = self._resolve_session(session_id, allow_any_state=True)
        if session_id is not None:
            if session is None:
                return error
            return self._format_snapshot(session.snapshot())

        with self._lock:
            sessions = list(self._sessions.values())
        if not sessions:
            return "No active shell sessions."

        parts = [self._format_snapshot(item.snapshot()) for item in sessions]
        return "\n\n".join(parts)

    def send_input(self, text: str, session_id: str | None = None) -> str:
        """Forward text input to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_input(text)

    def send_enter(self, session_id: str | None = None) -> str:
        """Send Enter to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_enter()

    def send_up(self, session_id: str | None = None) -> str:
        """Send Up to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_arrow_up()

    def send_down(self, session_id: str | None = None) -> str:
        """Send Down to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_arrow_down()

    def send_left(self, session_id: str | None = None) -> str:
        """Send Left to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_arrow_left()

    def send_right(self, session_id: str | None = None) -> str:
        """Send Right to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_arrow_right()

    def send_tab(self, session_id: str | None = None) -> str:
        """Send Tab to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_tab()

    def send_escape(self, session_id: str | None = None) -> str:
        """Send Escape to a waiting shell session."""
        session, error = self._resolve_session(
            session_id,
            states={"waiting_user_input"},
        )
        if session is None:
            return error
        return session.write_escape()

    def cancel_session(self, session_id: str | None = None) -> str:
        """Cancel an active shell session."""
        session, error = self._resolve_session(session_id, allow_any_state=True)
        if session is None:
            return error
        session.request_cancel()
        return f"Cancellation requested for shell session {session.session_id}."

    def shutdown(self) -> None:
        """Stop accepting work and wait briefly for active sessions to exit."""
        with self._lock:
            self._closing.set()
            sessions = list(self._sessions.values())

        for session in sessions:
            session.request_cancel()

        deadline = time.monotonic() + self._shutdown_join_timeout
        for session in sessions:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session.join(timeout=remaining)

    def _resolve_session(
        self,
        session_id: str | None,
        *,
        states: set[str] | None = None,
        allow_any_state: bool = False,
    ) -> tuple[InteractiveShellSession | None, str]:
        with self._lock:
            sessions = list(self._sessions.values())

        if session_id is not None:
            for session in sessions:
                if session.session_id == session_id:
                    snapshot = session.snapshot()
                    if not allow_any_state and states and snapshot.state not in states:
                        return (
                            None,
                            f"Error: shell session {session_id} is in state {snapshot.state}.",
                        )
                    return session, ""
            return None, f"Error: shell session {session_id} was not found."

        candidates = []
        for session in sessions:
            snapshot = session.snapshot()
            if allow_any_state or states is None or snapshot.state in states:
                candidates.append(session)

        if not candidates:
            return None, "Error: no matching shell session is waiting for input."
        if len(candidates) > 1:
            ids = ", ".join(item.session_id for item in candidates)
            return (
                None,
                "Error: multiple shell sessions match. Specify a session id: "
                + ids,
            )
        return candidates[0], ""

    @staticmethod
    def _format_snapshot(snapshot: ShellSessionSnapshot) -> str:
        lines = [
            f"Session: {snapshot.session_id}",
            f"State: {snapshot.state}",
            f"CWD: {snapshot.cwd}",
            f"Command: {snapshot.command}",
            f"Idle: {snapshot.idle_seconds:.1f}s",
        ]
        if snapshot.tail_lines:
            lines.append("Recent output:")
            lines.extend(f"  {line}" for line in snapshot.tail_lines[-4:])
        return "\n".join(lines)


def create_shell_task(
    *,
    queue,
    ui_sink: WarningSink | None,
    cwd_provider: Callable[[], Path],
    agent_os_dir: Path,
    blacklist: list[str] | None = None,
    timeout: int = 30,
    export_env: list[str] | None = None,
    handoff: ShellHandoffConfig | None = None,
    max_concurrent: int = 2,
    manager: ShellTaskManager | None = None,
) -> Callable[..., str]:
    """Create a queue-backed shell_task tool."""
    manager = manager or ShellTaskManager(
        max_concurrent=max_concurrent,
        ui_sink=ui_sink,
    )
    default_timeout = timeout
    handoff_evaluator = ShellHandoffEvaluator.from_config(
        handoff or ShellHandoffConfig()
    )
    def shell_task(command: str = "", timeout: int | None = None, **kwargs) -> str:
        from ...agent.schema import InboundMessage

        del kwargs
        if queue is None:
            return "Error: shell_task requires a queue-backed runtime."
        if not command:
            return "Error: command is required."
        if is_memory_write_shell_command(command, agent_os_dir=agent_os_dir):
            return "Error: Direct memory writes via shell are blocked. Use memory_edit."
        blocked = check_shell_command(command, blacklist or [])
        if blocked is not None:
            return blocked
        if manager.is_closing():
            return "[SHELL UNAVAILABLE] Background shell tasks are shutting down."
        if not manager.try_acquire_slot():
            return (
                "[SHELL BUSY] Too many background shell tasks are already running. "
                "Wait for a result before starting another one."
            )

        effective_timeout = clamp_timeout(timeout, default_timeout)

        cwd = cwd_provider()
        session_id = manager.allocate_session_id()

        def _on_handoff(
            session: InteractiveShellSession,
            _rule_id: str,
            state: str,
            snapshot: ShellSessionSnapshot,
        ) -> None:
            manager.emit_handoff_warning(
                session=session,
                state=state,
                snapshot=snapshot,
            )

        def _on_complete(
            session: InteractiveShellSession,
            output: str,
            _final_state: str,
        ) -> None:
            try:
                header = (
                    "[SHELL TASK ERROR]"
                    if output.startswith("Error")
                    else "[SHELL TASK RESULT]"
                )
                body = output if output else "(no output)"
                msg = InboundMessage(
                    channel="shell_task",
                    content=(
                        f"{header}\n"
                        f"Session: {session.session_id}\n"
                        f"Command: {session.command}\n"
                        f"CWD: {session.cwd}\n\n"
                        f"{body}"
                    ),
                    priority=0,
                    sender="system",
                    metadata={
                        "shell_session_id": session.session_id,
                        "shell_command": session.command,
                        "shell_cwd": str(session.cwd),
                    },
                )
                manager.enqueue_if_open(queue, msg)
            finally:
                manager.finish_session(session.session_id)

        session = InteractiveShellSession(
            session_id=session_id,
            command=command,
            cwd=cwd,
            timeout=effective_timeout,
            export_env=export_env,
            handoff=handoff_evaluator,
            on_handoff=_on_handoff,
            on_complete=_on_complete,
        )
        try:
            if not manager.start_session(session):
                manager.release_slot()
                return "[SHELL UNAVAILABLE] Background shell tasks are shutting down."
        except Exception:
            manager.release_slot()
            raise
        return (
            "[SHELL DISPATCHED] Background shell task accepted. "
            f"Session: {session_id}. "
            "Result will be delivered as a [shell_task, from system] message."
        )

    return shell_task
