"""Tests for background shell_task handoff flows."""

from __future__ import annotations

import os
import shlex
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from lincy.core.schema import ShellHandoffConfig, ShellHandoffRuleConfig
from lincy.core.config import load_config
from lincy.tools.builtin.shell_task import (
    SHELL_TASK_DEFINITION,
    ShellTaskManager,
    create_shell_task,
)
from lincy.tools.executor import ShellExecutor


class _QueueStub:
    def __init__(self) -> None:
        self.items: list[object] = []
        self._event = threading.Event()

    def put(self, msg) -> None:
        self.items.append(msg)
        self._event.set()

    def wait_for_count(self, count: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.items) >= count:
                return True
            self._event.wait(0.05)
            self._event.clear()
        return len(self.items) >= count


class _UiSinkStub:
    def __init__(self) -> None:
        self.events: list[object] = []
        self._event = threading.Event()

    def emit(self, event) -> None:
        self.events.append(event)
        self._event.set()

    def wait_for_count(self, count: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.events) >= count:
                return True
            self._event.wait(0.05)
            self._event.clear()
        return len(self.events) >= count


def _python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(textwrap.dedent(code))}"


def _make_waiting_input_handoff(grace_seconds: float = 0.05) -> ShellHandoffConfig:
    return ShellHandoffConfig(
        enabled=True,
        grace_seconds=grace_seconds,
        rules=[
            ShellHandoffRuleConfig(
                id="authorization_code",
                outcome="waiting_user_input",
                any_text=[r"(?i)authorization code"],
                prompt_suffix=[":"],
                process_alive=True,
            )
        ],
    )


class TestShellTaskDefinition:
    def test_name_and_params(self):
        assert SHELL_TASK_DEFINITION.name == "shell_task"
        assert "requires user action" in SHELL_TASK_DEFINITION.description
        assert "command" in SHELL_TASK_DEFINITION.parameters
        assert "timeout" in SHELL_TASK_DEFINITION.parameters
        assert SHELL_TASK_DEFINITION.required == ["command"]


class TestShellTaskManager:
    def test_start_session_removes_failed_session(self):
        manager = ShellTaskManager(max_concurrent=1)

        class _BrokenSession:
            session_id = "sh_9999"

            def start(self):
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            manager.start_session(_BrokenSession())

        assert manager.format_status() == "No active shell sessions."


class TestCreateShellTask:
    def test_dispatches_and_injects_result(self, tmp_path: Path):
        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
        )

        output = fn(command="echo hello")

        assert "[SHELL DISPATCHED]" in output
        assert "Session: sh_" in output
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert msg.channel == "shell_task"
        assert msg.sender == "system"
        assert "[SHELL TASK RESULT]" in msg.content
        assert "Command: echo hello" in msg.content
        assert "hello" in msg.content

    def test_empty_command_returns_error(self, tmp_path: Path):
        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
        )

        output = fn(command="")

        assert output == "Error: command is required."
        assert not queue.items

    def test_busy_when_concurrency_limit_reached(self, tmp_path: Path):
        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            max_concurrent=1,
        )

        first = fn(command=_python_command("""
            import time
            time.sleep(0.5)
        """))
        second = fn(command="echo later")

        assert "[SHELL DISPATCHED]" in first
        assert "[SHELL BUSY]" in second
        assert queue.wait_for_count(1)

    def test_shutdown_rejects_new_work(self, tmp_path: Path):
        queue = _QueueStub()
        manager = ShellTaskManager(max_concurrent=1)
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            manager=manager,
        )

        manager.shutdown()
        output = fn(command="echo hello")

        assert "[SHELL UNAVAILABLE]" in output
        assert not queue.items

    def test_blocks_memory_write_commands(self, tmp_path: Path):
        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
        )

        output = fn(command="echo nope > memory/agent/recent.md")

        assert output == "Error: Direct memory writes via shell are blocked. Use memory_edit."
        assert not queue.items

    def test_uses_dispatch_time_cwd_snapshot(self, tmp_path: Path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        foreground = ShellExecutor(agent_os_dir=tmp_path)
        foreground.execute(f"cd {shlex.quote(str(first))}")
        current_cwd = {"value": foreground.cwd}

        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: current_cwd["value"],
            agent_os_dir=tmp_path,
        )

        output = fn(command=_python_command("""
            import os
            import time
            time.sleep(0.2)
            print(os.getcwd())
        """))
        current_cwd["value"] = second

        assert "[SHELL DISPATCHED]" in output
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert f"CWD: {first}" in msg.content
        assert str(first) in msg.content
        assert str(second) not in msg.content

    def test_shutdown_kills_active_background_process(self, tmp_path: Path):
        queue = _QueueStub()
        manager = ShellTaskManager(max_concurrent=1)
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            manager=manager,
        )
        pid_file = tmp_path / "shell.pid"

        output = fn(command=f"echo $$ > {shlex.quote(str(pid_file))}; sleep 60")

        assert "[SHELL DISPATCHED]" in output
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        assert pid_file.exists()

        pid = int(pid_file.read_text().strip())
        manager.shutdown()

        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"Background shell process {pid} survived shutdown")

        assert not queue.wait_for_count(1, timeout=0.3)

    def test_grace_seconds_prevents_transient_false_positive(self, tmp_path: Path):
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=_make_waiting_input_handoff(grace_seconds=0.5),
            manager=manager,
        )

        output = fn(command=_python_command("""
            print('Enter authorization code:', flush=True)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert queue.wait_for_count(1)
        assert not ui_sink.events

    def test_emits_warning_and_accepts_input_for_waiting_session(self, tmp_path: Path):
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=_make_waiting_input_handoff(),
            manager=manager,
            timeout=2,
        )

        output = fn(command=_python_command("""
            import sys
            print('Enter authorization code:', flush=True)
            value = sys.stdin.readline().strip()
            print('received=' + value, flush=True)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert ui_sink.wait_for_count(1)
        warning = ui_sink.events[0]
        assert "Waiting for input" in warning.message
        assert "Enter authorization code:" in warning.message
        status = manager.format_status()
        assert "State: waiting_user_input" in status

        forwarded = manager.send_input("123456")
        assert "Forwarded input to shell session sh_" in forwarded
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert "[SHELL TASK RESULT]" in msg.content
        assert "received=[REDACTED INPUT]" in msg.content
        assert "123456" not in msg.content

    def test_can_target_session_explicitly(self, tmp_path: Path):
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=2, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=_make_waiting_input_handoff(),
            manager=manager,
            timeout=2,
        )

        first = fn(command=_python_command("""
            import sys
            print('Enter authorization code:', flush=True)
            print('one=' + sys.stdin.readline().strip(), flush=True)
        """))
        second = fn(command=_python_command("""
            import sys
            print('Enter authorization code:', flush=True)
            print('two=' + sys.stdin.readline().strip(), flush=True)
        """))

        assert "Session: sh_0001" in first
        assert "Session: sh_0002" in second
        assert ui_sink.wait_for_count(2)
        assert manager.send_input("zzz") == (
            "Error: multiple shell sessions match. Specify a session id: sh_0001, sh_0002"
        )

        result = manager.send_input("abc123", session_id="sh_0002")
        assert result == "Forwarded input to shell session sh_0002."
        assert queue.wait_for_count(1)
        assert "two=[REDACTED INPUT]" in queue.items[0].content
        manager.shutdown()

    def test_repo_config_detects_interactive_menu_prompt(self, tmp_path: Path):
        config = load_config("agent.yaml")
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=config.tools.shell.handoff,
            manager=manager,
            timeout=5,
        )

        output = fn(command=_python_command("""
            import time
            print('? What is your preferred protocol?  [Use arrows to move, type to filter]', flush=True)
            time.sleep(4)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert ui_sink.wait_for_count(1)
        warning = ui_sink.events[0]
        assert "Waiting for input" in warning.message
        assert "Use arrows to move" in warning.message
        manager.shutdown()

    def test_headless_shell_session_blocks_browser_launchers(self, tmp_path: Path):
        queue = _QueueStub()
        fn = create_shell_task(
            queue=queue,
            ui_sink=None,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            timeout=5,
        )

        output = fn(command=_python_command("""
            import subprocess
            subprocess.run(["osascript"], check=False)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert "chat-agent headless shell blocked browser launcher" in msg.content
        assert "osascript" in msg.content

    def test_send_down_reaches_waiting_menu_session(self, tmp_path: Path):
        config = load_config("agent.yaml")
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=config.tools.shell.handoff,
            manager=manager,
            timeout=5,
        )

        output = fn(command=_python_command("""
            import sys
            import termios
            import time
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            print('? Choose one  [Use arrows to move, type to filter]', flush=True)
            try:
                tty.setraw(fd)
                value = sys.stdin.buffer.read(3)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print(value.hex(), flush=True)
            time.sleep(0.1)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert ui_sink.wait_for_count(1)
        assert "Waiting for input" in ui_sink.events[0].message
        result = manager.send_down()
        assert result == "Sent Down to shell session sh_0001."
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert "1b5b42" in msg.content
        manager.shutdown()

    def test_send_tab_reaches_waiting_menu_session(self, tmp_path: Path):
        config = load_config("agent.yaml")
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=config.tools.shell.handoff,
            manager=manager,
            timeout=5,
        )

        output = fn(command=_python_command("""
            import sys
            import termios
            import time
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            print('? Choose one  [Use arrows to move, type to filter]', flush=True)
            try:
                tty.setraw(fd)
                value = sys.stdin.buffer.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print(value.hex(), flush=True)
            time.sleep(0.1)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert ui_sink.wait_for_count(1)
        assert "Waiting for input" in ui_sink.events[0].message
        result = manager.send_tab()
        assert result == "Sent Tab to shell session sh_0001."
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert "09" in msg.content
        manager.shutdown()

    def test_navigation_controls_can_chain_without_waiting_for_reclassification(
        self,
        tmp_path: Path,
    ):
        config = load_config("agent.yaml")
        queue = _QueueStub()
        ui_sink = _UiSinkStub()
        manager = ShellTaskManager(max_concurrent=1, ui_sink=ui_sink)
        fn = create_shell_task(
            queue=queue,
            ui_sink=ui_sink,
            cwd_provider=lambda: tmp_path,
            agent_os_dir=tmp_path,
            handoff=config.tools.shell.handoff,
            manager=manager,
            timeout=5,
        )

        output = fn(command=_python_command("""
            import sys
            import termios
            import time
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            print('? Choose one  [Use arrows to move, type to filter]', flush=True)
            try:
                tty.setraw(fd)
                value = sys.stdin.buffer.read(4)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print(value.hex(), flush=True)
            time.sleep(0.1)
        """))

        assert "[SHELL DISPATCHED]" in output
        assert ui_sink.wait_for_count(1)
        assert manager.send_down() == "Sent Down to shell session sh_0001."
        assert manager.send_enter() == "Sent Enter to shell session sh_0001."
        assert queue.wait_for_count(1)
        msg = queue.items[0]
        assert "1b5b420a" in msg.content
        manager.shutdown()
