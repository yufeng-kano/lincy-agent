import json

from ..llm.schema import ToolCall
from ..tui.formatting import indent_lines


def _pretty_json(value: object) -> str:
    """Render JSON-like values in a stable, human-readable form."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _parse_json_text(text: str) -> object | None:
    """Parse JSON text; return None when parsing fails."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped or not stripped.startswith(("{", "[")):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None



def _format_send_message_call(args: dict) -> str:
    """Format send_message tool call as a readable block, not raw JSON."""
    lines: list[str] = []
    channel = args.get("channel")
    to = args.get("to")
    subject = args.get("subject")
    reply_to_message = args.get("reply_to_message")

    lines.append(f"channel: {channel if isinstance(channel, str) else '?'}")
    if isinstance(to, str) and to:
        lines.append(f"to: {to}")
    else:
        lines.append("to: (reply/current sender)")

    if isinstance(subject, str) and subject:
        lines.append(f"subject: {subject}")
    if isinstance(reply_to_message, str) and reply_to_message:
        lines.append(f"reply_to_message: {reply_to_message}")

    body = args.get("body")
    if isinstance(body, str):
        lines.append("body:")
        lines.append(indent_lines(body, "  "))
    elif body is not None:
        lines.append("body:")
        lines.append(indent_lines(_pretty_json(body), "  "))
    else:
        lines.append("body: ?")

    attachments = args.get("attachments")
    if isinstance(attachments, list) and attachments:
        lines.append("attachments:")
        for item in attachments:
            lines.append(f"  - {item}")

    return "\n".join(lines)


def _extract_memory_paths_from_requests(args: dict) -> tuple[int, list[str]]:
    """Extract request count and unique target paths from memory_edit args."""
    request_list_raw = args.get("requests")
    request_list = (
        [item for item in request_list_raw if isinstance(item, dict)]
        if isinstance(request_list_raw, list)
        else []
    )
    paths: list[str] = []
    seen: set[str] = set()
    for request in request_list:
        path = request.get("target_path")
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            paths.append(path)
    return len(request_list), paths


def _format_multiline_paths(paths: list[str]) -> str:
    """Format paths as one item per line without truncation."""
    if not paths:
        return ""
    return "\n".join(f"  - {path}" for path in paths)


def _collect_memory_result_files(payload: dict) -> list[str]:
    """Collect memory_edit result paths with per-file statuses."""
    applied = payload.get("applied")
    if not isinstance(applied, list):
        return []

    pairs: list[str] = []
    seen: set[str] = set()
    for item in applied:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        status = item.get("status")
        if not isinstance(path, str) or not path:
            continue
        if path in seen:
            continue
        seen.add(path)
        if isinstance(status, str) and status:
            pairs.append(f"{path}({status})")
        else:
            pairs.append(path)
    return pairs


def _collect_memory_result_warnings(payload: dict) -> list[str]:
    """Collect memory_edit warnings with path/code/detail summary."""
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        return []

    items: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in warnings:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        code = item.get("code")
        detail = item.get("detail")
        if not isinstance(path, str) or not path:
            path = "?"
        if not isinstance(code, str) or not code:
            code = "warning"
        if not isinstance(detail, str):
            detail = ""

        key = (path, code, detail)
        if key in seen:
            continue
        seen.add(key)

        line = f"{path}({code})"
        if detail:
            line = f"{line}: {detail}"
        items.append(line)

    return items


def format_tool_call(tool_call: ToolCall) -> str:
    """Format tool call for display."""
    name = tool_call.name
    args = tool_call.arguments

    if name == "read_file":
        return f"Read: {args.get('path', '?')}"
    elif name == "write_file":
        return f"Write: {args.get('path', '?')}"
    elif name == "edit_file":
        return f"Edit: {args.get('path', '?')}"
    elif name == "memory_edit":
        count, paths = _extract_memory_paths_from_requests(args)
        path_summary = _format_multiline_paths(paths)
        if path_summary:
            return f"MemoryEdit: {count} request(s)\n{path_summary}"
        return f"MemoryEdit: {count} request(s)"
    elif name == "execute_shell":
        cmd = args.get("command", "?")
        return f"Shell: {cmd}"
    elif name == "shell_task":
        cmd = args.get("command", "?")
        return f"Shell Task: {cmd}"
    elif name == "web_search":
        query = args.get("query", "?")
        return f"Web Search: {query}"
    elif name == "web_fetch":
        url = args.get("url", "?")
        return f"Web Fetch: {url}"
    elif name == "read_image":
        return f"ReadImage: {args.get('path', '?')}"
    elif name == "get_current_time":
        tz = args.get("timezone") or "default"
        return f"Time: {tz}"
    elif name == "send_message":
        if isinstance(args, dict):
            return _format_send_message_call(args)
        return str(args)
    elif name == "gui_task":
        intent = args.get("intent", "?")
        app_prompt = args.get("app_prompt", "")
        prompt_info = f"app_prompt: {app_prompt}" if app_prompt else "app_prompt: (none)"
        return f"GUI Task: {intent}\n  {prompt_info}"
    else:
        if isinstance(args, dict):
            return _pretty_json(args)
        return f"{name}: {args}"


def format_gui_tool_call(tool_call: ToolCall) -> str:
    """Format a GUI manager internal tool call for display."""
    name = tool_call.name
    args = tool_call.arguments

    if name == "done":
        return f"done: {args.get('summary', '?')}"
    if name == "fail":
        return f"fail: {args.get('reason', '?')}"
    if name == "report_problem":
        return f"report_problem: {args.get('problem', '?')}"
    return f"{name}: {args}"


def format_gui_tool_result(tool_call: ToolCall, result: str) -> str:
    """Format a GUI manager internal tool result for display."""
    return result


def format_tool_result(tool_call: ToolCall, result: str) -> str:
    """Format tool result for display."""
    name = tool_call.name

    if result.startswith("Error"):
        return result

    if name == "read_file":
        if result.startswith("{"):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "returned_lines" in payload:
                returned = payload.get("returned_lines", 0)
                total = payload.get("total_lines", "?")
                return f"{returned} lines (json, total={total})"
        lines = result.count("\n") + 1 if result else 0
        return f"{lines} lines"
    elif name == "write_file":
        return result
    elif name == "edit_file":
        return result
    elif name == "memory_edit":
        if result.startswith("{"):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                status = payload.get("status", "unknown")
                applied = payload.get("applied", [])
                errors = payload.get("errors", [])
                warnings = payload.get("warnings", [])
                applied_count = len(applied) if isinstance(applied, list) else 0
                error_count = len(errors) if isinstance(errors, list) else 0
                warning_count = len(warnings) if isinstance(warnings, list) else 0
                file_items = _collect_memory_result_files(payload)
                warning_items = _collect_memory_result_warnings(payload)
                file_summary = _format_multiline_paths(file_items)
                warning_summary = _format_multiline_paths(warning_items)
                if status == "failed" and isinstance(errors, list) and errors:
                    first = errors[0]
                    if isinstance(first, dict):
                        code = first.get("code", "unknown")
                        detail = first.get("detail", "")
                        base = (
                            f"failed ({code}): {detail}"
                            if detail
                            else f"failed ({code})"
                        )
                        parts = [base]
                        if file_summary:
                            parts.append(f"files:\n{file_summary}")
                        if warning_summary:
                            parts.append(f"warnings:\n{warning_summary}")
                        return "\n".join(parts)
                base = (
                    f"status={status}, applied={applied_count}, "
                    f"errors={error_count}, warnings={warning_count}"
                )
                parts = [base]
                if file_summary:
                    parts.append(f"files:\n{file_summary}")
                if warning_summary:
                    parts.append(f"warnings:\n{warning_summary}")
                return "\n".join(parts)
        return result
    elif name == "execute_shell":
        stripped = result.strip()
        if not stripped:
            return "(empty)"
        return stripped
    elif name == "read_image":
        return result
    elif name == "get_current_time":
        return result
    else:
        payload = _parse_json_text(result)
        if payload is not None:
            return _pretty_json(payload)
        return result
