"""File operation tools."""

import json
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from ...llm.schema import ToolDefinition, ToolParameter
from ..security import resolve_allowed_path

# ---------------------------------------------------------------------------
# Curly-quote normalization (edit_file)
# ---------------------------------------------------------------------------

_CURLY_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",  # left single
        "\u2019": "'",  # right single
        "\u201c": '"',  # left double
        "\u201d": '"',  # right double
    }
)


def _normalize_quotes(text: str) -> str:
    """Normalize curly/smart quotes to straight ASCII equivalents."""
    return text.translate(_CURLY_QUOTE_MAP)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

READ_FILE_DEFINITION = ToolDefinition(
    name="read_file",
    description=(
        "Read file content. By default returns text with line numbers. "
        "Set output_format='json' for structured output with metadata. "
        "Supports PDF files (text extraction) and Jupyter notebooks (.ipynb)."
    ),
    parameters={
        "path": ToolParameter(
            type="string",
            description="The path to the file to read.",
        ),
        "offset": ToolParameter(
            type="integer",
            description="Line number to start reading from (1-indexed). Defaults to 1.",
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum number of lines to read. Defaults to 2000.",
        ),
        "output_format": ToolParameter(
            type="string",
            description="Output format: 'text' (default) or 'json'.",
            enum=["text", "json"],
        ),
    },
    required=["path"],
)

WRITE_FILE_DEFINITION = ToolDefinition(
    name="write_file",
    description="Create a file or write to an existing empty file. Fails if target file already has content.",
    parameters={
        "path": ToolParameter(
            type="string",
            description="The path to the file to write.",
        ),
        "content": ToolParameter(
            type="string",
            description="The content to write to the file.",
        ),
    },
    required=["path", "content"],
)

EDIT_FILE_DEFINITION = ToolDefinition(
    name="edit_file",
    description="Edit a file by replacing a specific string. The old_string must be unique in the file unless replace_all is True.",
    parameters={
        "path": ToolParameter(
            type="string",
            description="The path to the file to edit.",
        ),
        "old_string": ToolParameter(
            type="string",
            description="The exact string to find and replace.",
        ),
        "new_string": ToolParameter(
            type="string",
            description="The string to replace with.",
        ),
        "replace_all": ToolParameter(
            type="boolean",
            description="If True, replace all occurrences. If False (default), the old_string must be unique.",
        ),
    },
    required=["path", "old_string", "new_string"],
)


# ---------------------------------------------------------------------------
# Notebook (.ipynb) parsing
# ---------------------------------------------------------------------------

def _parse_notebook(path: Path) -> str:
    """Parse a Jupyter notebook into readable markdown-style text."""
    data = json.loads(path.read_text(encoding="utf-8"))

    kernel_lang = (
        data.get("metadata", {}).get("kernelspec", {}).get("language", "python")
    )

    parts: list[str] = []
    cells = data.get("cells", [])

    for i, cell in enumerate(cells, start=1):
        cell_type = cell.get("cell_type", "unknown")
        source = "".join(cell.get("source", []))

        if cell_type == "markdown":
            parts.append(f"--- Cell {i} [markdown] ---")
            parts.append(source)
        elif cell_type == "code":
            parts.append(f"--- Cell {i} [code] ---")
            parts.append(f"```{kernel_lang}")
            parts.append(source)
            parts.append("```")
            outputs = cell.get("outputs", [])
            if outputs:
                out_parts: list[str] = []
                for out in outputs:
                    otype = out.get("output_type")
                    if otype == "stream":
                        out_parts.append("".join(out.get("text", [])))
                    elif otype in ("execute_result", "display_data"):
                        text_data = out.get("data", {}).get("text/plain", [])
                        if text_data:
                            out_parts.append(
                                "".join(text_data)
                                if isinstance(text_data, list)
                                else text_data
                            )
                    elif otype == "error":
                        ename = out.get("ename", "Error")
                        evalue = out.get("evalue", "")
                        out_parts.append(f"{ename}: {evalue}")
                if out_parts:
                    parts.append("")
                    parts.append("Output:")
                    parts.extend(out_parts)
        elif cell_type == "raw":
            parts.append(f"--- Cell {i} [raw] ---")
            parts.append(source)

        parts.append("")  # blank line between cells

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shared formatting helper
# ---------------------------------------------------------------------------

def _format_lines(
    lines: list[str],
    *,
    path: str,
    resolved_path: str,
    offset: int,
    limit: int,
    output_format: str,
) -> str:
    """Format a list of text lines into the requested output format."""
    total = len(lines)
    start = max(0, offset - 1)
    end = start + limit
    selected = lines[start:end]

    if output_format == "json":
        payload = {
            "path": path,
            "resolved_path": resolved_path,
            "encoding": "utf-8",
            "offset": offset,
            "limit": limit,
            "total_lines": total,
            "returned_lines": len(selected),
            "start_line": start + 1,
            "end_line": start + len(selected),
            "truncated": end < total,
            "lines": [
                {"line": i, "content": line}
                for i, line in enumerate(selected, start=start + 1)
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    end_line = start + len(selected)
    header = f'<file path="{path}" lines="{start + 1}-{end_line}" total_lines="{total}">'
    result = [header]
    for i, line in enumerate(selected, start=start + 1):
        result.append(f"{i:6d}\t{line}")
    result.append("</file>")
    return "\n".join(result)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

def create_read_file(
    allowed_paths: list[str],
    base_dir: Path,
) -> Callable[..., str]:
    """Create a read_file function with path checking."""

    def read_file(
        path: str,
        offset: int = 1,
        limit: int = 2000,
        output_format: str = "text",
    ) -> str:
        """Read a file with optional offset and limit."""
        target = resolve_allowed_path(path, allowed_paths, base_dir)
        if target is None:
            return f"Error: Path '{path}' is not allowed"

        if not target.exists():
            return f"Error: File '{target}' does not exist"

        if not target.is_file():
            return f"Error: '{target}' is not a file"

        if output_format not in {"text", "json"}:
            return "Error: Invalid output_format. Use 'text' or 'json'."

        suffix = target.suffix.lower()

        # PDF: extract text via pymupdf
        if suffix == ".pdf":
            try:
                from .pdf_utils import extract_pdf_text

                text = extract_pdf_text(str(target))
            except ValueError as exc:
                return f"Error: {exc}"
            except Exception as exc:
                return f"Error reading PDF: {exc}"
            return _format_lines(
                text.splitlines(),
                path=path,
                resolved_path=str(target),
                offset=offset,
                limit=limit,
                output_format=output_format,
            )

        # Jupyter notebook: parse cells
        if suffix == ".ipynb":
            try:
                text = _parse_notebook(target)
            except Exception as exc:
                return f"Error reading notebook: {exc}"
            return _format_lines(
                text.splitlines(),
                path=path,
                resolved_path=str(target),
                offset=offset,
                limit=limit,
                output_format=output_format,
            )

        # Default: UTF-8 text
        try:
            content = target.read_bytes()
            if b"\x00" in content[:8192]:
                return f"Error: '{target}' appears to be a binary file"
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return f"Error: '{target}' is not a valid UTF-8 file"
        except Exception as exc:
            return f"Error reading file: {exc}"

        return _format_lines(
            lines,
            path=path,
            resolved_path=str(target),
            offset=offset,
            limit=limit,
            output_format=output_format,
        )

    return read_file


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

def create_write_file(
    allowed_paths: list[str],
    base_dir: Path,
) -> Callable[..., str]:
    """Create a write_file function with path checking."""

    def write_file(path: str, content: str) -> str:
        """Write content to a file."""
        target = resolve_allowed_path(path, allowed_paths, base_dir)
        if target is None:
            return f"Error: Path '{path}' is not allowed"

        if target.exists() and not target.is_file():
            return f"Error: '{target}' is not a file"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)

            if target.exists() and target.stat().st_size > 0:
                return (
                    f"Error: Refusing to overwrite non-empty file '{target}'. "
                    "Use edit_file for updates."
                )

            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content.encode('utf-8'))} bytes to {target}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    return write_file


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def _replace_with_normalized_quotes(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> tuple[str, int]:
    """Replace matches found via curly-quote normalization.

    str.maketrans is a 1:1 char mapping so character offsets stay aligned
    between original and normalized text.
    """
    normalized_content = _normalize_quotes(content)
    normalized_old = _normalize_quotes(old_string)

    result_parts: list[str] = []
    last_end = 0
    count = 0

    pos = normalized_content.find(normalized_old)
    while pos >= 0:
        result_parts.append(content[last_end:pos])
        result_parts.append(new_string)
        last_end = pos + len(normalized_old)
        count += 1
        if not replace_all:
            break
        pos = normalized_content.find(normalized_old, last_end)

    result_parts.append(content[last_end:])
    return "".join(result_parts), count


def create_edit_file(
    allowed_paths: list[str],
    base_dir: Path,
) -> Callable[..., str]:
    """Create an edit_file function with path checking."""

    def edit_file(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """Edit a file by replacing strings."""
        target = resolve_allowed_path(path, allowed_paths, base_dir)
        if target is None:
            return f"Error: Path '{path}' is not allowed"

        if not target.exists():
            return f"Error: File '{target}' does not exist"

        if not target.is_file():
            return f"Error: '{target}' is not a file"

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading file: {exc}"

        # Phase 1: exact match (fast path)
        count = content.count(old_string)

        if count == 0:
            # Phase 2: try curly-quote normalization
            normalized_content = _normalize_quotes(content)
            normalized_old = _normalize_quotes(old_string)
            norm_count = normalized_content.count(normalized_old)

            if norm_count > 0:
                if norm_count > 1 and not replace_all:
                    lines = _find_occurrence_lines_normalized(content, old_string)
                    line_hint = ""
                    if lines:
                        preview = ", ".join(str(n) for n in lines[:5])
                        line_hint = f" First matches at lines: {preview}."
                    return (
                        f"Error: '{_preview_text(old_string)}' appears {norm_count} times "
                        f"(matched via quote normalization).{line_hint} "
                        "Use replace_all=True to replace all occurrences."
                    )

                new_content, replaced = _replace_with_normalized_quotes(
                    content, old_string, new_string, replace_all
                )
                try:
                    target.write_text(new_content, encoding="utf-8")
                    return (
                        f"Successfully replaced {replaced} occurrence(s) in {target} "
                        "(matched via quote normalization)"
                    )
                except Exception as exc:
                    return f"Error writing file: {exc}"

            return _build_not_found_error(old_string, content)

        if count > 1 and not replace_all:
            lines = _find_occurrence_lines(content, old_string)
            line_hint = ""
            if lines:
                preview = ", ".join(str(n) for n in lines[:5])
                line_hint = f" First matches at lines: {preview}."
            return (
                f"Error: '{_preview_text(old_string)}' appears {count} times."
                f"{line_hint} Use replace_all=True to replace all occurrences."
            )

        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        try:
            target.write_text(new_content, encoding="utf-8")
            return f"Successfully replaced {replaced} occurrence(s) in {target}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    return edit_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preview_text(text: str, max_len: int = 80) -> str:
    """Create a compact preview for error messages."""
    compact = text.replace("\n", "\\n")
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def _normalize_text(text: str) -> str:
    """Normalize line endings and trailing spaces for fuzzy comparison."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def _find_occurrence_lines(content: str, needle: str) -> list[int]:
    """Find 1-indexed line numbers where an exact needle occurs."""
    if not needle:
        return []

    positions: list[int] = []
    cursor = 0
    while True:
        idx = content.find(needle, cursor)
        if idx < 0:
            break
        positions.append(content.count("\n", 0, idx) + 1)
        cursor = idx + 1
    return positions


def _find_occurrence_lines_normalized(content: str, needle: str) -> list[int]:
    """Find 1-indexed line numbers via curly-quote normalization."""
    return _find_occurrence_lines(_normalize_quotes(content), _normalize_quotes(needle))


def _find_similar_lines(content: str, needle: str, max_items: int = 3) -> list[str]:
    """Return best-effort similar lines with line numbers."""
    query = needle.strip()
    if not query:
        return []

    candidates: list[tuple[float, int, str]] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        ratio = SequenceMatcher(None, query, line.strip()).ratio()
        if ratio >= 0.45:
            candidates.append((ratio, idx, line))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [f"{idx}: {line}" for _, idx, line in candidates[:max_items]]


def _build_not_found_error(old_string: str, content: str) -> str:
    """Build an actionable not-found message for edit failures."""
    hints: list[str] = []

    normalized_count = _normalize_text(content).count(_normalize_text(old_string))
    if normalized_count > 0:
        hints.append(
            f"Found {normalized_count} match(es) after normalizing line endings/trailing spaces."
        )

    stripped = old_string.strip()
    if stripped and stripped != old_string:
        stripped_count = content.count(stripped)
        if stripped_count > 0:
            hints.append(
                f"Found {stripped_count} match(es) after stripping surrounding whitespace."
            )

    similar = _find_similar_lines(content, old_string.splitlines()[0] if old_string else "")
    if similar:
        hints.append("Similar lines: " + " | ".join(similar))

    hint_text = " Hint: " + " ".join(hints) if hints else " Hint: Use read_file to copy exact text."
    return f"Error: '{_preview_text(old_string)}' not found in file.{hint_text}"
