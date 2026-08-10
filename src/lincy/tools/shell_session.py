"""Interactive PTY-backed shell session for shell_task handoff flows."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import errno
import os
import pty
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from dotenv import dotenv_values

from .shell_handoff import ShellHandoffEvaluator, ShellHandoffObservation
from .security import MAX_OUTPUT_SIZE, truncate_output
_SELECT_TIMEOUT_SECONDS = 0.2
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HEADLESS_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "BROWSER",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "SUDO_ASKPASS",
)
_HEADLESS_BROWSER_LAUNCHERS = (
    "open",
    "osascript",
    "xdg-open",
    "gio",
    "gnome-open",
    "kde-open",
    "kde-open5",
    "sensible-browser",
    "x-www-browser",
)
_HEADLESS_BROWSER_STUB_MESSAGE = "chat-agent headless shell blocked browser launcher"


@dataclass(frozen=True, slots=True)
class ShellSessionSnapshot:
    """Thread-safe status snapshot for one shell session."""

    session_id: str
    command: str
    cwd: Path
    state: str
    idle_seconds: float
    tail_lines: tuple[str, ...]
    process_alive: bool


class InteractiveShellSession:
    """Own one PTY subprocess and classify handoff states."""

    def __init__(
        self,
        *,
        session_id: str,
        command: str,
        cwd: Path,
        timeout: int,
        export_env: list[str] | None,
        handoff: ShellHandoffEvaluator,
        on_handoff,
        on_complete,
    ) -> None:
        self.session_id = session_id
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self._handoff = handoff
        self._on_handoff = on_handoff
        self._on_complete = on_complete
        self._export_env = export_env or []

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel_requested = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._state = "running"
        self._last_output_at = time.monotonic()
        self._started_at = time.monotonic()
        self._tail_lines: deque[str] = deque(maxlen=max(1, handoff.tail_lines))
        self._output_parts: list[str] = []
        self._current_line = ""
        self._secret_inputs: list[str] = []
        self._last_handoff_rule_id: str | None = None
        self._handoff_match_since: float | None = None
        self._pending_handoff_rule_id: str | None = None
        self._headless_shim_dir: tempfile.TemporaryDirectory[str] | None = None

    def start(self) -> None:
        """Start the PTY-backed subprocess thread."""
        thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"shell-session-{self.session_id}",
        )
        self._thread = thread
        thread.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background thread to finish."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def request_cancel(self) -> None:
        """Request cancellation and terminate the session."""
        self._cancel_requested.set()
        self.close()

    def close(self) -> None:
        """Terminate the subprocess and close the PTY."""
        with self._lock:
            process = self._process
            master_fd = self._master_fd
            self._master_fd = None
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGHUP)
            except Exception:
                pass
            time.sleep(0.05)
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

    def write_input(self, text: str) -> str:
        """Write user-provided text to the PTY master."""
        cleaned = text.strip()
        if len(cleaned) >= 4:
            with self._lock:
                self._secret_inputs.append(cleaned)
        with self._lock:
            process = self._process
            master_fd = self._master_fd
        if process is None or master_fd is None or process.poll() is not None:
            return "Error: shell session is no longer running."

        payload = text
        if not payload.endswith("\n"):
            payload += "\n"

        try:
            os.write(master_fd, payload.encode("utf-8"))
        except OSError as exc:
            return f"Error: failed to write to shell session: {exc}"
        with self._lock:
            self._state = "running"
            self._last_handoff_rule_id = None
            self._pending_handoff_rule_id = None
            self._handoff_match_since = None
        return f"Forwarded input to shell session {self.session_id}."

    def write_enter(self) -> str:
        """Send a bare newline to the shell session."""
        return self._write_control_bytes(b"\n", "Enter", preserve_waiting_state=True)

    def write_arrow_up(self) -> str:
        """Send the Up arrow escape sequence to the shell session."""
        return self._write_control_bytes(b"\x1b[A", "Up", preserve_waiting_state=True)

    def write_arrow_down(self) -> str:
        """Send the Down arrow escape sequence to the shell session."""
        return self._write_control_bytes(b"\x1b[B", "Down", preserve_waiting_state=True)

    def write_arrow_left(self) -> str:
        """Send the Left arrow escape sequence to the shell session."""
        return self._write_control_bytes(b"\x1b[D", "Left", preserve_waiting_state=True)

    def write_arrow_right(self) -> str:
        """Send the Right arrow escape sequence to the shell session."""
        return self._write_control_bytes(b"\x1b[C", "Right", preserve_waiting_state=True)

    def write_tab(self) -> str:
        """Send Tab to the shell session."""
        return self._write_control_bytes(b"\t", "Tab", preserve_waiting_state=True)

    def write_escape(self) -> str:
        """Send Escape to the shell session."""
        return self._write_control_bytes(b"\x1b", "Escape", preserve_waiting_state=True)

    def snapshot(self) -> ShellSessionSnapshot:
        """Return a thread-safe snapshot for UI and slash commands."""
        with self._lock:
            tail = tuple(self._tail_lines)
            current = self._current_line
            if current:
                tail = (*tail, current)
            return ShellSessionSnapshot(
                session_id=self.session_id,
                command=self.command,
                cwd=self.cwd,
                state=self._state,
                idle_seconds=max(0.0, time.monotonic() - self._last_output_at),
                tail_lines=tuple(tail[-self._handoff.tail_lines:]),
                process_alive=self._process is not None and self._process.poll() is None,
            )

    def _run(self) -> None:
        final_output = ""
        final_state = "exited"
        try:
            master_fd, slave_fd = pty.openpty()
            env = self._build_env()
            process = subprocess.Popen(
                self.command,
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self.cwd),
                env=env,
                preexec_fn=os.setsid,
                close_fds=True,
            )
            os.close(slave_fd)
            self._process = process
            self._master_fd = master_fd

            deadline = self._started_at + self.timeout
            while True:
                if self._cancel_requested.is_set():
                    final_state = "cancelled"
                    final_output = "Error: Command cancelled by user"
                    break

                now = time.monotonic()
                if now >= deadline:
                    final_state = "timed_out"
                    final_output = f"Error: Command timed out after {self.timeout} seconds"
                    break

                self._read_ready_output(timeout=_SELECT_TIMEOUT_SECONDS)
                self._update_handoff_state()

                if process.poll() is not None:
                    self._drain_remaining_output()
                    break

            if final_state == "exited":
                final_output = self._build_output()
        except Exception as exc:
            final_state = "failed"
            final_output = f"Error: {exc}"
        finally:
            self.close()
            self._cleanup_headless_shims()
            with self._lock:
                self._state = final_state
            self._on_complete(self, final_output, final_state)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in _HEADLESS_ENV_KEYS:
            env.pop(key, None)
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        shim_dir = self._ensure_headless_shim_dir()
        env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
        env["BROWSER"] = "chat-agent-browser-blocked"
        if self._export_env:
            dotenv_values_map = dotenv_values()
            for key in self._export_env:
                value = dotenv_values_map.get(key) or os.getenv(key)
                if value is not None:
                    env[key] = value
        return env

    def _read_ready_output(self, *, timeout: float) -> None:
        with self._lock:
            master_fd = self._master_fd
        if master_fd is None:
            return
        try:
            ready, _, _ = select.select([master_fd], [], [], timeout)
        except OSError:
            return
        if not ready:
            return

        try:
            data = os.read(master_fd, 4096)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise
        if not data:
            return

        chunk = self._sanitize_chunk(data.decode("utf-8", errors="replace"))
        if not chunk:
            return
        with self._lock:
            self._append_chunk_locked(chunk)
            self._last_output_at = time.monotonic()

    def _drain_remaining_output(self) -> None:
        while True:
            before = len(self._output_parts)
            self._read_ready_output(timeout=0.0)
            after = len(self._output_parts)
            if after == before:
                break

    def _append_chunk_locked(self, chunk: str) -> None:
        self._output_parts.append(chunk)
        if sum(len(part) for part in self._output_parts) > MAX_OUTPUT_SIZE:
            self._output_parts = [truncate_output("".join(self._output_parts))]

        secrets = tuple(self._secret_inputs)
        text = chunk.replace("\r\n", "\n").replace("\r", "\n")
        parts = text.split("\n")
        if len(parts) == 1:
            self._current_line += parts[0]
            return

        self._current_line += parts[0]
        self._tail_lines.append(self._sanitize_output(self._current_line, secrets=secrets))
        for middle in parts[1:-1]:
            self._tail_lines.append(self._sanitize_output(middle, secrets=secrets))
        self._current_line = parts[-1]

    def _update_handoff_state(self) -> None:
        if not self._handoff.enabled:
            return
        snapshot = self.snapshot()
        observation = ShellHandoffObservation(
            tail_lines=snapshot.tail_lines,
            last_line=snapshot.tail_lines[-1] if snapshot.tail_lines else "",
            process_alive=snapshot.process_alive,
            idle_seconds=snapshot.idle_seconds,
        )
        rule = self._handoff.evaluate(observation)
        now = time.monotonic()
        with self._lock:
            current_state = self._state
            current_rule_id = self._last_handoff_rule_id
            pending_rule_id = self._pending_handoff_rule_id
            match_since = self._handoff_match_since
        if rule is None:
            with self._lock:
                if current_state in {"waiting_external_action", "waiting_user_input"}:
                    self._state = "running"
                    self._last_handoff_rule_id = None
                self._pending_handoff_rule_id = None
                self._handoff_match_since = None
            return

        next_state = rule.outcome
        if pending_rule_id != rule.id:
            with self._lock:
                self._pending_handoff_rule_id = rule.id
                self._handoff_match_since = now
            return
        if match_since is None or (now - match_since) < self._handoff.grace_seconds:
            return
        if next_state == current_state and current_rule_id == rule.id:
            return

        with self._lock:
            self._state = next_state
            self._last_handoff_rule_id = rule.id
            self._pending_handoff_rule_id = rule.id
        self._on_handoff(self, rule.id, next_state, snapshot)

    def _build_output(self) -> str:
        with self._lock:
            parts = list(self._output_parts)
            current = self._current_line
            secrets = tuple(self._secret_inputs)
        output = "".join(parts)
        if current:
            output += current
        output = self._sanitize_output(output, secrets=secrets).strip()
        if not output:
            return "(no output)"
        return output

    def _sanitize_chunk(self, chunk: str) -> str:
        return _ANSI_ESCAPE_RE.sub("", chunk)

    def _sanitize_output(
        self,
        text: str,
        *,
        secrets: tuple[str, ...] | None = None,
    ) -> str:
        sanitized = text
        if secrets is None:
            with self._lock:
                secrets = tuple(self._secret_inputs)
        for secret in secrets:
            sanitized = sanitized.replace(secret, "[REDACTED INPUT]")
        return sanitized

    def _write_control_bytes(
        self,
        payload: bytes,
        label: str,
        *,
        preserve_waiting_state: bool = False,
    ) -> str:
        with self._lock:
            process = self._process
            master_fd = self._master_fd
        if process is None or master_fd is None or process.poll() is not None:
            return "Error: shell session is no longer running."
        try:
            os.write(master_fd, payload)
        except OSError as exc:
            return f"Error: failed to write to shell session: {exc}"
        with self._lock:
            if preserve_waiting_state and self._state == "waiting_user_input":
                pass
            else:
                self._state = "running"
                self._last_handoff_rule_id = None
                self._pending_handoff_rule_id = None
                self._handoff_match_since = None
        return f"Sent {label} to shell session {self.session_id}."

    def _ensure_headless_shim_dir(self) -> str:
        with self._lock:
            temp_dir = self._headless_shim_dir
            if temp_dir is None:
                temp_dir = tempfile.TemporaryDirectory(prefix="chat-agent-shell-")
                self._headless_shim_dir = temp_dir
                self._write_headless_browser_stubs(Path(temp_dir.name))
            return temp_dir.name

    def _cleanup_headless_shims(self) -> None:
        with self._lock:
            temp_dir = self._headless_shim_dir
            self._headless_shim_dir = None
        if temp_dir is not None:
            temp_dir.cleanup()

    @staticmethod
    def _write_headless_browser_stubs(directory: Path) -> None:
        script = (
            "#!/bin/sh\n"
            f'echo "{_HEADLESS_BROWSER_STUB_MESSAGE}: $0 $*" >&2\n'
            "exit 1\n"
        )
        for launcher in _HEADLESS_BROWSER_LAUNCHERS:
            path = directory / launcher
            path.write_text(script)
            path.chmod(0o755)
