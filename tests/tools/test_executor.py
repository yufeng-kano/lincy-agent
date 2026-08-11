"""Tests for shell command executor."""

import os
import sys
from pathlib import Path


from lincy.tools.executor import ShellExecutor
from lincy.tools.builtin.shell import (
    create_execute_shell,
    is_claude_code_stream_json_command,
)
from lincy.cli.claude_code_stream_json import (
    parse_claude_code_stream_json_line,
    extract_text_from_claude_code_stream_json_lines,
)


class TestShellExecutor:
    def test_basic_command(self, tmp_path: Path):
        """Basic command execution works."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute("echo hello")
        assert "hello" in result

    def test_cwd_tracking(self, tmp_path: Path):
        """Working directory is tracked across commands."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        executor = ShellExecutor(agent_os_dir=tmp_path)
        assert executor.cwd == tmp_path

        executor.execute(f"cd {subdir}")
        assert executor.cwd == subdir

    def test_cwd_persists(self, tmp_path: Path):
        """Commands run in tracked cwd."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        executor = ShellExecutor(agent_os_dir=tmp_path)
        executor.execute(f"cd {subdir}")

        result = executor.execute("pwd")
        assert str(subdir) in result

    def test_blacklist_blocks_command(self, tmp_path: Path):
        """Blacklisted commands are blocked."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            blacklist=["rm\\s+-rf"],
        )
        result = executor.execute("rm -rf /")
        assert "blocked" in result.lower()

    def test_blacklist_partial_match(self, tmp_path: Path):
        """Blacklist patterns match substrings."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            blacklist=["dangerous"],
        )
        result = executor.execute("echo dangerous_command")
        assert "blocked" in result.lower()

    def test_blacklist_allows_safe(self, tmp_path: Path):
        """Non-matching commands are allowed."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            blacklist=["rm\\s+-rf"],
        )
        result = executor.execute("ls -la")
        assert "blocked" not in result.lower()

    def test_timeout_kills_process(self, tmp_path: Path):
        """Long-running commands are terminated."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            timeout=1,  # 1 second timeout
        )
        result = executor.execute("sleep 10")
        assert "timed out" in result.lower()

    def test_stdin_is_closed_for_subprocesses(self, tmp_path: Path):
        """Commands reading stdin fail fast instead of hanging."""
        executor = ShellExecutor(agent_os_dir=tmp_path, timeout=1)
        original_stdin = os.dup(0)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        os.dup2(read_fd, 0)
        os.close(read_fd)

        try:
            result = executor.execute(
                f'{sys.executable} -c "import sys; print(\'ready\'); sys.stdout.flush(); input()"'
            )
        finally:
            os.dup2(original_stdin, 0)
            os.close(original_stdin)
            os.close(write_fd)

        assert "timed out" not in result.lower()
        assert "ready" in result
        assert "EOFError" in result

    def test_cancel_kills_process_buffered(self, tmp_path: Path):
        """Cancellation callback terminates buffered command execution."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            timeout=10,
            is_cancel_requested=lambda: True,
        )
        result = executor.execute("sleep 10")
        assert "cancelled" in result.lower()

    def test_command_error_output(self, tmp_path: Path):
        """Command errors are captured."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute("ls /nonexistent_path_12345")
        assert "No such file" in result or "cannot access" in result.lower()

    def test_creates_agent_os_dir(self, tmp_path: Path):
        """Working directory is created if it doesn't exist."""
        new_dir = tmp_path / "new" / "nested" / "dir"
        ShellExecutor(agent_os_dir=new_dir)
        assert new_dir.exists()

    def test_multiline_output(self, tmp_path: Path):
        """Multiline output is captured correctly."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute("echo line1; echo line2; echo line3")
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_env_vars(self, tmp_path: Path):
        """Environment variables work."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute("export TEST_VAR=hello && echo $TEST_VAR")
        assert "hello" in result

    def test_is_blocked_returns_pattern(self, tmp_path: Path):
        """is_blocked returns the matched pattern."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            blacklist=["rm\\s+-rf", "mkfs"],
        )
        assert executor.is_blocked("rm -rf /") == "rm\\s+-rf"
        assert executor.is_blocked("mkfs /dev/sda") == "mkfs"
        assert executor.is_blocked("ls -la") is None

    def test_per_call_timeout_clamp(self, tmp_path: Path):
        """Per-call timeout below configured default is clamped up."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            timeout=10,  # Default 10 seconds
        )
        # Attempt lower timeout (1s) -> clamped to default (10s), sleep 5 succeeds
        result = executor.execute("sleep 5", timeout=1)
        assert "timed out" not in result.lower()

    def test_per_call_timeout_higher_allowed(self, tmp_path: Path):
        """Per-call timeout above configured default is accepted."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            timeout=1,  # Default 1 second
        )
        # Override with higher timeout (10s), sleep 3 succeeds
        result = executor.execute("sleep 3", timeout=10)
        assert "timed out" not in result.lower()

    def test_cd_to_nonexistent_dir_keeps_old_cwd(self, tmp_path: Path):
        """Failed cd command does not corrupt cwd tracking."""
        executor = ShellExecutor(agent_os_dir=tmp_path)

        # Try to cd to nonexistent directory
        executor.execute("cd /nonexistent_path_12345")

        # cwd should remain unchanged
        assert executor.cwd == tmp_path

    def test_heredoc_does_not_poison_cwd(self, tmp_path: Path):
        """Heredoc commands do not corrupt cwd tracking."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        test_file = tmp_path / "test.txt"

        # Execute a heredoc command
        executor.execute(f"""cat > {test_file} <<EOF
line1
line2
EOF""")

        # cwd should still be valid
        assert executor.cwd == tmp_path
        assert executor.cwd.exists()

        # File should be created with correct content
        assert test_file.exists()
        content = test_file.read_text()
        assert "line1" in content
        assert "line2" in content

    def test_command_output_contains_marker(self, tmp_path: Path):
        """Command outputting marker string does not corrupt cwd."""
        from lincy.tools.executor import _CWD_MARKER

        executor = ShellExecutor(agent_os_dir=tmp_path)
        executor.execute(f"echo '{_CWD_MARKER}'")

        # cwd should still be valid
        assert executor.cwd == tmp_path

    def test_path_with_spaces(self, tmp_path: Path):
        """Paths with spaces are handled correctly."""
        space_dir = tmp_path / "path with spaces"
        space_dir.mkdir()

        executor = ShellExecutor(agent_os_dir=tmp_path)
        executor.execute(f"cd '{space_dir}'")

        assert executor.cwd == space_dir

    def test_extra_output_after_marker(self, tmp_path: Path):
        """Extra output between marker and pwd does not corrupt cwd."""
        executor = ShellExecutor(agent_os_dir=tmp_path)

        # Command that produces stderr after the main command
        executor.execute("echo test; echo 'some warning' >&2")

        assert executor.cwd == tmp_path

    # ------------------------------------------------------------------
    # Streaming (on_stdout_line callback)
    # ------------------------------------------------------------------

    def test_streaming_callback_receives_lines(self, tmp_path: Path):
        """on_stdout_line callback receives each stdout line."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        lines: list[str] = []
        result = executor.execute(
            "echo aaa; echo bbb; echo ccc",
            on_stdout_line=lines.append,
        )
        assert "aaa" in lines
        assert "bbb" in lines
        assert "ccc" in lines
        # Full output is still returned
        assert "aaa" in result
        assert "bbb" in result

    def test_streaming_preserves_cwd_tracking(self, tmp_path: Path):
        """cwd marker is correctly extracted in streaming mode."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        executor = ShellExecutor(agent_os_dir=tmp_path)
        lines: list[str] = []
        executor.execute(f"cd {subdir}", on_stdout_line=lines.append)
        assert executor.cwd == subdir

    def test_streaming_timeout(self, tmp_path: Path):
        """Timeout works correctly in streaming mode."""
        executor = ShellExecutor(agent_os_dir=tmp_path, timeout=1)
        lines: list[str] = []
        result = executor.execute("sleep 10", on_stdout_line=lines.append)
        assert "timed out" in result.lower()

    def test_streaming_cancel(self, tmp_path: Path):
        """Cancellation callback terminates streaming command execution."""
        executor = ShellExecutor(
            agent_os_dir=tmp_path,
            timeout=10,
            is_cancel_requested=lambda: True,
        )
        lines: list[str] = []
        result = executor.execute("sleep 10", on_stdout_line=lines.append)
        assert "cancelled" in result.lower()

    def test_no_callback_unchanged(self, tmp_path: Path):
        """Without on_stdout_line, behaviour is identical to before."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute("echo unchanged")
        assert "unchanged" in result

    def test_output_transform_applied(self, tmp_path: Path):
        """output_transform converts collected lines for tool result."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        lines: list[str] = []
        result = executor.execute(
            "echo aaa; echo bbb",
            on_stdout_line=lines.append,
            output_transform=lambda collected: ",".join(
                line for line in collected if line in ("aaa", "bbb")
            ),
        )
        assert result == "aaa,bbb"

    def test_output_transform_not_used_without_streaming(self, tmp_path: Path):
        """output_transform is ignored when on_stdout_line is None."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute(
            "echo hello",
            output_transform=lambda collected: "should not appear",
        )
        assert "hello" in result
        assert "should not appear" not in result

    def test_output_transform_receives_cleaned_lines(self, tmp_path: Path):
        """output_transform does not receive injected cwd marker lines."""
        executor = ShellExecutor(agent_os_dir=tmp_path)
        result = executor.execute(
            "echo hello",
            on_stdout_line=lambda _line: None,
            output_transform=lambda collected: "\n".join(collected),
        )
        assert result == "hello"
        assert "__CWD_MARKER_" not in result


class TestExecuteShellStreamingDetection:
    """Tests for Claude Code stream-json command detection."""

    def test_detects_claude_stream_json_command(self):
        assert is_claude_code_stream_json_command(
            'claude -p --output-format stream-json "weather tomorrow" --model sonnet --verbose'
        )

    def test_detects_claude_stream_json_command_after_cd(self):
        cmd = (
            'cd /tmp && claude -p --output-format stream-json --verbose "hi"'
        )
        assert is_claude_code_stream_json_command(cmd)

    def test_rejects_non_claude_false_positive(self):
        assert not is_claude_code_stream_json_command(
            "echo --output-format stream-json"
        )

    def test_rejects_claude_as_argument_false_positive(self):
        assert not is_claude_code_stream_json_command(
            "printf %s claude --output-format stream-json"
        )

    def test_rejects_commented_out_claude_false_positive(self):
        assert not is_claude_code_stream_json_command(
            "echo ok # claude --output-format stream-json"
        )

    def test_wrapper_does_not_enable_transform_on_false_positive(self):
        class DummyExecutor:
            def __init__(self):
                self.last_call = None

            def execute(self, command, timeout=None, on_stdout_line=None, output_transform=None):
                self.last_call = {
                    "command": command,
                    "timeout": timeout,
                    "on_stdout_line": on_stdout_line,
                    "output_transform": output_transform,
                }
                return "ok"

        dummy = DummyExecutor()

        def callback(_line):
            return None

        wrapper = create_execute_shell(
            dummy,  # type: ignore[arg-type]
            on_stdout_line=callback,
            output_transform=lambda lines: ",".join(lines),
        )
        wrapper("echo --output-format stream-json", timeout=12)

        assert dummy.last_call is not None
        assert dummy.last_call["timeout"] == 12
        assert dummy.last_call["on_stdout_line"] is None
        assert dummy.last_call["output_transform"] is None

    def test_wrapper_enables_transform_for_claude_stream_json(self):
        class DummyExecutor:
            def __init__(self):
                self.last_call = None

            def execute(self, command, timeout=None, on_stdout_line=None, output_transform=None):
                self.last_call = {
                    "command": command,
                    "timeout": timeout,
                    "on_stdout_line": on_stdout_line,
                    "output_transform": output_transform,
                }
                return "ok"

        dummy = DummyExecutor()

        def callback(_line):
            return None

        def transform(lines):
            return ",".join(lines)

        wrapper = create_execute_shell(
            dummy,  # type: ignore[arg-type]
            on_stdout_line=callback,
            output_transform=transform,
        )
        wrapper('claude -p --output-format stream-json "hi" --verbose')

        assert dummy.last_call is not None
        assert dummy.last_call["on_stdout_line"] is callback
        assert dummy.last_call["output_transform"] is transform


class TestClaudeCodeStreamJsonParser:
    """Tests for Claude Code stream-json event parsing."""

    def test_parse_tool_use_in_assistant_event(self):
        """Assistant message with tool_use content block is parsed."""
        import json
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
                ],
            },
        })
        ev = parse_claude_code_stream_json_line(line)
        assert ev.kind == "tool_use"
        assert ev.tool_name == "Edit"

    def test_parse_assistant_text_ignored(self):
        """Assistant message with only text is ignored (not shown during streaming)."""
        import json
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hello"}],
            },
        })
        ev = parse_claude_code_stream_json_line(line)
        assert ev.kind == "ignored"

    def test_parse_result_event(self):
        """Result event is parsed with final text."""
        import json
        line = json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "Done. Modified 3 files.",
        })
        ev = parse_claude_code_stream_json_line(line)
        assert ev.kind == "result"
        assert ev.text == "Done. Modified 3 files."

    def test_parse_system_event_ignored(self):
        """System init events are ignored."""
        import json
        line = json.dumps({"type": "system", "subtype": "init", "session_id": "abc"})
        ev = parse_claude_code_stream_json_line(line)
        assert ev.kind == "ignored"

    def test_parse_non_json(self):
        """Non-JSON lines are treated as plain text."""
        ev = parse_claude_code_stream_json_line("just plain output")
        assert ev.kind == "text"
        assert ev.text == "just plain output"

    def test_extract_text_from_result_event(self):
        """Extract final text from result event."""
        import json
        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "id": "t1", "input": {}}]}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "Done. Cleaned up 2 files."}),
        ]
        assert extract_text_from_claude_code_stream_json_lines(lines) == "Done. Cleaned up 2 files."

    def test_extract_text_fallback_plain(self):
        """Non-JSON lines are returned as-is when no result event found."""
        lines = ["line one", "line two"]
        assert extract_text_from_claude_code_stream_json_lines(lines) == "line one\nline two"

    def test_extract_text_ignores_non_result_json(self):
        """Only result event text is returned, not assistant text."""
        import json
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "intermediate"}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "final answer"}),
        ]
        assert extract_text_from_claude_code_stream_json_lines(lines) == "final answer"

    def test_extract_text_fallback_uses_assistant_text_when_no_result(self):
        """Assistant text is returned when stream-json ends without result event."""
        import json
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial answer"}]},
            }),
        ]
        assert extract_text_from_claude_code_stream_json_lines(lines) == "partial answer"

    def test_extract_text_fallback_uses_tool_error_when_no_result(self):
        """Top-level tool_use_result error is preserved as readable fallback."""
        import json
        lines = [
            json.dumps({
                "type": "user",
                "tool_use_result": "Error: permission denied",
                "message": {"role": "user", "content": []},
            }),
        ]
        assert extract_text_from_claude_code_stream_json_lines(lines) == "Error: permission denied"
