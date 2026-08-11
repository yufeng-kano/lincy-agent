"""Tests for CLI formatter helpers."""

import json

from lincy.cli.formatter import (
    format_tool_call,
    format_tool_result,
    format_gui_tool_call,
    format_gui_tool_result,
)
from lincy.llm.schema import ToolCall


def test_format_tool_call_memory_edit_shows_target_paths():
    tool_call = ToolCall(
        id="m1",
        name="memory_edit",
        arguments={
            "as_of": "2026-02-09T01:10:00+08:00",
            "turn_id": "turn-1",
            "requests": [
                {
                    "request_id": "r1",
                    "target_path": "memory/agent/recent.md",
                    "instruction": "append short-term entry",
                },
                {
                    "request_id": "r2",
                    "target_path": "memory/agent/recent.md",
                    "instruction": "append state entry",
                },
            ],
        },
    )

    text = format_tool_call(tool_call)
    assert text.startswith("MemoryEdit: 2 request(s)")
    assert "\n  - memory/agent/recent.md" in text
    assert "\n  - memory/agent/recent.md" in text
    assert "memory/agent/recent.md" in text
    assert "memory/agent/recent.md" in text


def test_format_tool_call_memory_edit_ignores_updates_alias():
    tool_call = ToolCall(
        id="m2",
        name="memory_edit",
        arguments={
            "as_of": "2026-02-09T01:10:00+08:00",
            "turn_id": "turn-2",
            "updates": [
                {
                    "request_id": "r1",
                    "target_path": "memory/agent/recent.md",
                    "instruction": "append entry",
                }
            ],
        },
    )

    text = format_tool_call(tool_call)
    assert text == "MemoryEdit: 0 request(s)"


def test_format_tool_call_memory_edit_requires_target_path_key():
    tool_call = ToolCall(
        id="m2b",
        name="memory_edit",
        arguments={
            "as_of": "2026-02-09T01:12:00+08:00",
            "turn_id": "turn-2b",
            "requests": [
                {
                    "request_id": "r1",
                    "targetPath": "memory/agent/recent.md",
                    "instruction": "append entry",
                }
            ],
        },
    )

    text = format_tool_call(tool_call)
    assert text.startswith("MemoryEdit: 1 request(s)")
    assert "memory/agent/recent.md" not in text


def test_format_tool_result_memory_edit_shows_file_statuses():
    tool_call = ToolCall(
        id="m3",
        name="memory_edit",
        arguments={},
    )
    result = json.dumps(
        {
            "status": "ok",
            "turn_id": "turn-3",
            "applied": [
                {
                    "request_id": "r1",
                    "status": "applied",
                    "path": "memory/agent/recent.md",
                },
                {
                    "request_id": "r2",
                    "status": "noop",
                    "path": "memory/agent/long-term.md",
                },
            ],
            "errors": [],
        },
        ensure_ascii=False,
    )

    text = format_tool_result(tool_call, result)
    assert "status=ok" in text
    assert "\nfiles:\n" in text
    assert "\n  - memory/agent/recent.md(applied)" in text
    assert "\n  - memory/agent/long-term.md(noop)" in text


def test_format_tool_result_memory_edit_ignores_legacy_result_fields():
    tool_call = ToolCall(
        id="m4",
        name="memory_edit",
        arguments={},
    )
    result = json.dumps(
        {
            "status": "ok",
            "turn_id": "turn-4",
            "applied": [
                {
                    "request_id": "r1",
                    "apply_status": "applied",
                    "target_path": "memory/agent/recent.md",
                }
            ],
            "errors": [],
        },
        ensure_ascii=False,
    )

    text = format_tool_result(tool_call, result)
    assert "files=" not in text


def test_format_tool_result_memory_edit_shows_warnings():
    tool_call = ToolCall(
        id="m5",
        name="memory_edit",
        arguments={},
    )
    result = json.dumps(
        {
            "status": "ok",
            "turn_id": "turn-5",
            "applied": [
                {
                    "request_id": "r1",
                    "status": "applied",
                    "path": "memory/agent/long-term.md",
                }
            ],
            "errors": [],
            "warnings": [
                {
                    "path": "memory/agent/long-term.md",
                    "code": "file_too_long",
                    "detail": "151 lines (threshold: 150), see kernel/builtin-skills/memory-maintenance/",
                }
            ],
        },
        ensure_ascii=False,
    )

    text = format_tool_result(tool_call, result)
    assert "warnings=1" in text
    assert "\nwarnings:\n" in text
    assert "memory/agent/long-term.md(file_too_long)" in text


def test_format_tool_call_unknown_tool_pretty_prints_json_args():
    tool_call = ToolCall(
        id="u1",
        name="custom_tool",
        arguments={"b": 2, "a": {"x": 1}},
    )

    text = format_tool_call(tool_call)
    assert text.startswith("{\n")
    assert '"a"' in text
    assert "\n  \"b\": 2\n" in text


def test_format_tool_call_web_fetch_shows_url():
    tool_call = ToolCall(
        id="wf1",
        name="web_fetch",
        arguments={"url": "https://example.com/docs"},
    )

    text = format_tool_call(tool_call)
    assert text == "Web Fetch: https://example.com/docs"


def test_format_tool_result_unknown_tool_pretty_prints_json_text():
    tool_call = ToolCall(
        id="u2",
        name="custom_tool",
        arguments={},
    )
    result = json.dumps({"z": [1, 2], "a": 1}, ensure_ascii=False)

    text = format_tool_result(tool_call, result)
    assert text.startswith("{\n")
    assert '\n  "a": 1,' in text
    assert '\n  "z": [\n' in text


def test_format_tool_call_send_message_formats_body_as_block():
    tool_call = ToolCall(
        id="sm1",
        name="send_message",
        arguments={
            "channel": "cli",
            "to": "yufeng",
            "body": "line1\nline2",
            "attachments": ["/tmp/a.txt"],
        },
    )

    text = format_tool_call(tool_call)
    assert "channel: cli" in text
    assert "to: yufeng" in text
    assert "body:" in text
    assert "  line1" in text
    assert "  line2" in text
    assert "attachments:" in text
    assert "  - /tmp/a.txt" in text


# --- GUI tool formatting tests ---


class TestFormatToolCallGUITask:
    def test_gui_task_shows_intent(self):
        tc = ToolCall(id="g1", name="gui_task", arguments={"intent": "Open Safari"})
        text = format_tool_call(tc)
        assert "GUI Task: Open Safari" in text
        assert "app_prompt: (none)" in text

    def test_gui_task_no_truncation_by_default(self):
        long_intent = "A" * 100
        tc = ToolCall(id="g2", name="gui_task", arguments={"intent": long_intent})
        text = format_tool_call(tc)
        assert f"GUI Task: {long_intent}" in text

    def test_gui_task_with_app_prompt(self):
        tc = ToolCall(id="g4", name="gui_task", arguments={
            "intent": "Open LINE",
            "app_prompt": "personal-skills/gui-control/references/line-operation.md",
        })
        text = format_tool_call(tc)
        assert "GUI Task: Open LINE" in text
        assert "app_prompt: personal-skills/gui-control/references/line-operation.md" in text

    def test_gui_task_without_app_prompt(self):
        tc = ToolCall(id="g5", name="gui_task", arguments={"intent": "Open Safari"})
        text = format_tool_call(tc)
        assert "app_prompt: (none)" in text


class TestFormatToolCallShellTask:
    def test_shell_task_shows_command(self):
        tc = ToolCall(id="s1", name="shell_task", arguments={"command": "uv run pytest"})
        text = format_tool_call(tc)
        assert text == "Shell Task: uv run pytest"


class TestFormatToolCallWebSearch:
    def test_web_search_shows_query(self):
        tc = ToolCall(id="w1", name="web_search", arguments={"query": "latest openai pricing"})
        text = format_tool_call(tc)
        assert text == "Web Search: latest openai pricing"


class TestFormatGUIToolCall:
    def test_done(self):
        tc = ToolCall(id="6", name="done", arguments={"summary": "Task done."})
        assert format_gui_tool_call(tc) == "done: Task done."

    def test_fail(self):
        tc = ToolCall(id="7", name="fail", arguments={"reason": "Could not find."})
        assert format_gui_tool_call(tc) == "fail: Could not find."

    def test_report_problem(self):
        tc = ToolCall(id="8", name="report_problem", arguments={"problem": "Target not found."})
        assert format_gui_tool_call(tc) == "report_problem: Target not found."

    def test_report_problem_missing_arg(self):
        tc = ToolCall(id="9", name="report_problem", arguments={})
        assert format_gui_tool_call(tc) == "report_problem: ?"


class TestFormatGUIToolResult:
    def test_other_tool_short(self):
        tc = ToolCall(id="3", name="click", arguments={"bbox": [1, 2, 3, 4]})
        assert format_gui_tool_result(tc, "Clicked at (100, 200)") == "Clicked at (100, 200)"

    def test_other_tool_result_does_not_truncate(self):
        tc = ToolCall(id="3", name="click", arguments={"bbox": [1, 2, 3, 4]})
        long_result = "B" * 80
        text = format_gui_tool_result(tc, long_result)
        assert text == long_result
