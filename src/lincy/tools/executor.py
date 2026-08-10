"""Shell command executor with safety controls."""

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

from .security import clamp_timeout, check_shell_command, truncate_output

# Marker for extracting cwd after command execution
_CWD_MARKER = "__CWD_MARKER_8f3a2b__"

# Timeout for joining reader thread after process exits
_READER_JOIN_TIMEOUT = 5
_WAIT_POLL_INTERVAL_SECONDS = 0.2


class _CommandCancelledError(Exception):
    """Raised when a running shell command is cancelled by the user."""


def _load_env_allowlist(keys: list[str]) -> dict[str, str]:
    """Load specific keys from .env file. Ignores missing keys."""
    if not keys:
        return {}
    all_values = dotenv_values()
    return {k: all_values[k] for k in keys if k in all_values}


class ShellExecutor:
    """Execute shell commands with cwd tracking and safety controls."""

    def __init__(
        self,
        agent_os_dir: Path,
        blacklist: list[str] | None = None,
        timeout: int = 30,
        export_env: list[str] | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
    ):
        """Initialize the executor.

        Args:
            agent_os_dir: Initial working directory.
            blacklist: List of regex patterns to block.
            timeout: Command timeout in seconds.
            export_env: Keys to load from .env into subprocess environment.
        """
        self._cwd = agent_os_dir.resolve()
        self._blacklist = [re.compile(p) for p in (blacklist or [])]
        self._timeout = timeout
        self._extra_env = _load_env_allowlist(export_env or [])
        self._is_cancel_requested = is_cancel_requested

        # Ensure working directory exists
        self._cwd.mkdir(parents=True, exist_ok=True)

    @property
    def cwd(self) -> Path:
        """Current working directory."""
        return self._cwd

    def is_blocked(self, command: str) -> str | None:
        """Check if command matches any blacklist pattern.

        Returns:
            The matched pattern string if blocked, None otherwise.
        """
        for pattern in self._blacklist:
            if pattern.search(command):
                return pattern.pattern
        return None

    def execute(
        self,
        command: str,
        timeout: int | None = None,
        on_stdout_line: Callable[[str], None] | None = None,
        output_transform: Callable[[list[str]], str | None] | None = None,
    ) -> str:
        """Execute a shell command and return output.

        Args:
            command: The shell command to execute.
            timeout: Override timeout in seconds (uses default if None).
            on_stdout_line: Optional callback invoked for each stdout line
                in real-time.  Called from a reader thread.  When provided,
                stdout is read line-by-line instead of buffered.
            output_transform: Optional function to convert collected lines
                into the final output string (streaming mode only).
                Defaults to ``"\\n".join``.

        Returns:
            Command output (stdout + stderr) or error message.
        """
        # Check blacklist
        blocked = check_shell_command(command, self._blacklist)
        if blocked:
            return blocked

        # Append pwd to track directory changes
        # Use newlines instead of semicolons to avoid breaking heredocs
        full_command = f"{command}\necho '{_CWD_MARKER}'\npwd"

        effective_timeout = clamp_timeout(timeout, self._timeout)

        try:
            env = {**os.environ, **self._extra_env} if self._extra_env else None
            process = subprocess.Popen(
                full_command,
                shell=True,
                # Close stdin so subprocesses fail fast instead of hanging
                # waiting for user input or stealing keystrokes from the TUI.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(self._cwd),
                env=env,
                text=True,
                # Create new process group for proper cleanup
                preexec_fn=os.setsid,
            )

            if on_stdout_line is not None:
                collected = self._collect_streaming(
                    process,
                    effective_timeout,
                    on_stdout_line,
                    is_cancel_requested=self._is_cancel_requested,
                )
                if collected is None:
                    return f"Error: Command timed out after {effective_timeout} seconds"

                # Always extract CWD from raw output first
                raw = "\n".join(collected)
                stripped = self._process_cwd_marker(raw)
                cleaned_lines = self._strip_cwd_marker_lines(collected)

                # Apply transform to cleaned lines; fall back to cleaned raw output.
                transformed = output_transform(cleaned_lines) if output_transform else None
                output = transformed if transformed is not None else stripped
            else:
                raw = self._execute_buffered(
                    process,
                    effective_timeout,
                    is_cancel_requested=self._is_cancel_requested,
                )
                if raw is None:
                    return f"Error: Command timed out after {effective_timeout} seconds"
                output = self._process_cwd_marker(raw)

            return truncate_output(output)

        except _CommandCancelledError:
            return "Error: Command cancelled by user"
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_cwd_marker(self, output: str) -> str:
        """Extract CWD from marker, update self._cwd, return cleaned output."""
        if _CWD_MARKER not in output:
            return output
        parts = output.rsplit(_CWD_MARKER, 1)
        cleaned = parts[0].rstrip()
        pwd_output = parts[1].strip()
        new_cwd = pwd_output.splitlines()[-1] if pwd_output else ""
        if new_cwd and new_cwd.startswith("/"):
            new_cwd_path = Path(new_cwd).resolve()
            if new_cwd_path.exists() and new_cwd_path.is_dir():
                self._cwd = new_cwd_path
        return cleaned

    @staticmethod
    def _strip_cwd_marker_lines(lines: list[str]) -> list[str]:
        """Remove the injected cwd marker suffix from collected line output."""
        for i in range(len(lines) - 1, -1, -1):
            if lines[i] == _CWD_MARKER:
                return lines[:i]
        return list(lines)

    @staticmethod
    def _execute_buffered(
        process: subprocess.Popen,
        timeout: int,
        *,
        is_cancel_requested: Callable[[], bool] | None = None,
    ) -> str | None:
        """Read stdout via communicate(). Returns None on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            if is_cancel_requested is not None and is_cancel_requested():
                ShellExecutor._terminate_process_group(process)
                raise _CommandCancelledError
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ShellExecutor._terminate_process_group(process)
                return None
            try:
                output, _ = process.communicate(
                    timeout=min(_WAIT_POLL_INTERVAL_SECONDS, remaining),
                )
                return output
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _collect_streaming(
        process: subprocess.Popen,
        timeout: int,
        on_stdout_line: Callable[[str], None],
        *,
        is_cancel_requested: Callable[[], bool] | None = None,
    ) -> list[str] | None:
        """Read stdout line-by-line via background thread.

        Returns collected lines, or None on timeout.
        """
        collected: list[str] = []

        def _drain() -> None:
            assert process.stdout is not None
            for raw_line in process.stdout:
                collected.append(raw_line.rstrip("\n"))
                on_stdout_line(raw_line.rstrip("\n"))

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        while True:
            if is_cancel_requested is not None and is_cancel_requested():
                ShellExecutor._terminate_process_group(process)
                reader.join(timeout=_READER_JOIN_TIMEOUT)
                raise _CommandCancelledError
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ShellExecutor._terminate_process_group(process)
                reader.join(timeout=_READER_JOIN_TIMEOUT)
                return None
            try:
                process.wait(timeout=min(_WAIT_POLL_INTERVAL_SECONDS, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        reader.join(timeout=_READER_JOIN_TIMEOUT)
        return collected

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        """Best-effort terminate subprocess process-group."""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=1)
        except Exception:
            pass
