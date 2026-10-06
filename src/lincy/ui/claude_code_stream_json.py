"""Parser for Claude Code ``--output-format stream-json --verbose`` events.

Each line from the subprocess stdout is a JSON object.  The format is
Claude Code's own envelope (NOT Anthropic API streaming events):

- ``{"type":"system","subtype":"init",...}``  -- session init
- ``{"type":"assistant","message":{"content":[...]}}``  -- assistant turn
  Content blocks may be ``{"type":"tool_use","name":"Edit",...}`` or
  ``{"type":"text","text":"..."}`` etc.
- ``{"type":"user","message":{...}}``  -- tool results fed back
- ``{"type":"result","result":"final clean text",...}``  -- final output
"""

import json
from dataclasses import dataclass


@dataclass(slots=True)
class ClaudeCodeStreamJsonEvent:
    """A parsed Claude Code stream-json event relevant for display."""

    kind: str  # "tool_use", "text", "result", "ignored"
    tool_name: str = ""
    text: str = ""


def parse_claude_code_stream_json_line(line: str) -> ClaudeCodeStreamJsonEvent:
    """Parse a single Claude Code stream-json line into an event.

    Returns an event with kind="ignored" for lines that are not
    interesting for real-time display.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        # Not JSON -- treat as plain text output
        return ClaudeCodeStreamJsonEvent(kind="text", text=line)

    etype = obj.get("type", "")

    # Assistant turn -- scan content blocks for tool_use
    if etype == "assistant":
        message = obj.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return ClaudeCodeStreamJsonEvent(
                        kind="tool_use",
                        tool_name=block.get("name", "?"),
                    )
        return ClaudeCodeStreamJsonEvent(kind="ignored")

    # Final result -- contains clean text
    if etype == "result":
        return ClaudeCodeStreamJsonEvent(kind="result", text=obj.get("result", ""))

    return ClaudeCodeStreamJsonEvent(kind="ignored")


def extract_text_from_claude_code_stream_json_lines(lines: list[str]) -> str:
    """Extract the final text response from collected stream-json lines.

    Looks for the ``type: "result"`` event and returns its ``result``
    field.  Falls back to joining non-JSON lines if no result event
    is found (plain command output).
    """
    plain_lines: list[str] = []
    assistant_texts: list[str] = []
    tool_result_errors: list[str] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            plain_lines.append(line)
            continue

        if obj.get("type") == "result":
            return obj.get("result", "")

        if obj.get("type") == "assistant":
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        assistant_texts.append(block["text"])

        if obj.get("type") == "user":
            tool_use_result = obj.get("tool_use_result")
            if isinstance(tool_use_result, str) and tool_use_result.strip():
                tool_result_errors.append(tool_use_result)
                continue
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    if not block.get("is_error"):
                        continue
                    text = block.get("content")
                    if isinstance(text, str) and text.strip():
                        tool_result_errors.append(text)

    # No result event:
    # 1) normal command output (plain text)
    # 2) partial Claude stream-json (return readable fallback)
    if plain_lines:
        return "\n".join(plain_lines)

    readable: list[str] = []
    if tool_result_errors:
        readable.extend(tool_result_errors)
    if assistant_texts:
        readable.extend(assistant_texts)
    if readable:
        return "\n".join(readable)

    # Last resort: preserve JSON lines for debugging instead of returning empty.
    return "\n".join(lines)
