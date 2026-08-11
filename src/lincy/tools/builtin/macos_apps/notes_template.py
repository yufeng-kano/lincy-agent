"""Apple Notes Markdown subset rendering helpers."""

from __future__ import annotations

import base64
from html import escape as html_escape
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ...security import resolve_allowed_path
from ..image import IMAGE_MIME_TYPES, html_to_markdown
from .runtime import (
    _HREF_RE,
    _INLINE_URL_RE,
    _MARKDOWN_IMAGE_RE,
    _NOTE_HEADING_BLOCK_RE,
    _ORDERED_LIST_RE,
    _TABLE_SEPARATOR_RE,
    _TEMPLATE_VAR_RE,
    _URL_TEXT_RE,
)

def _build_note_html(title: str | None, body: str) -> str:
    """Build a simple HTML payload accepted by Notes."""
    parts: list[str] = []
    if title:
        parts.append(f"<div><b>{html_escape(title)}</b></div>")
    for line in body.splitlines():
        if line.strip():
            parts.append(f"<div>{_linkify_escaped_urls(html_escape(line))}</div>")
        else:
            parts.append("<div><br></div>")
    return "".join(parts) or "<div><br></div>"


def _html_to_markdown(html: str) -> str:
    """Convert normalized Notes HTML into readable Markdown."""
    return html_to_markdown(_normalize_notes_heading_html(html))


def _heading_level_from_style(attrs: str, *, fallback: int | None = None) -> int | None:
    """Infer Notes heading level from normalized font size styles."""
    match = re.search(
        r"font-size\s*:\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>px|pt)",
        attrs or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return fallback
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    candidates = (
        {1: 20.0, 2: 18.0, 3: 16.0}
        if unit == "px"
        else {1: 15.0, 2: 13.5, 3: 12.0}
    )
    closest_level = min(candidates, key=lambda level: abs(candidates[level] - value))
    if abs(candidates[closest_level] - value) <= 0.6:
        return closest_level
    return fallback


def _normalize_notes_heading_html(html: str) -> str:
    """Convert Notes-normalized heading blocks back into semantic heading tags."""

    def replace(match: re.Match[str]) -> str:
        if match.group("h_level"):
            body = match.group("h_body") or ""
            level = _heading_level_from_style(
                match.group("h_attrs") or "",
                fallback=int(match.group("h_level")),
            )
        else:
            body = match.group("span_body") or ""
            level = _heading_level_from_style(match.group("span_attrs") or "")
        if level is None:
            return match.group(0)
        return f"<h{level}>{body}</h{level}>"

    return _NOTE_HEADING_BLOCK_RE.sub(replace, html)


def _normalize_markdown(text: str) -> str:
    """Collapse noisy blank lines and whitespace from Markdown output."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _first_visible_markdown_line(text: str) -> str:
    """Extract the first visible content line from Markdown-ish text."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-*]\s+|\d+\.\s+)", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            return line
    return ""


def _extract_source_url(html: str) -> str | None:
    """Extract the first http(s) link from a note body."""
    match = _HREF_RE.search(html)
    if match:
        return match.group("href").strip()
    text_match = _URL_TEXT_RE.search(html)
    return text_match.group(0).strip() if text_match else None


def _coerce_template_mapping(
    value: dict[str, Any] | None,
    *,
    field_name: str,
) -> dict[str, str]:
    """Normalize template variables/images into a string mapping."""
    if value is None:
        return {}
    normalized: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_name} keys must be non-empty strings")
        if raw is None:
            normalized[key] = ""
            continue
        if isinstance(raw, (str, int, float, bool)):
            normalized[key] = str(raw)
            continue
        raise ValueError(f"{field_name}.{key} must be a string-like scalar")
    return normalized


def _split_table_row(line: str) -> list[str]:
    """Split one simple pipe-table row."""
    trimmed = line.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return [cell.strip() for cell in trimmed.split("|")]


def _linkify_escaped_urls(text: str) -> str:
    """Wrap bare http(s) URLs in anchors after HTML escaping."""

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        return f'<a href="{html_escape(url, quote=True)}">{url}</a>'

    return _INLINE_URL_RE.sub(replace, text)


def _render_inline_markdown(text: str, *, image_html: dict[str, str]) -> str:
    """Render a small inline Markdown subset into HTML."""
    rendered = html_escape(text)
    placeholders: dict[str, str] = {}

    def stash(fragment: str) -> str:
        token = f"__CHAT_AGENT_INLINE_{len(placeholders)}__"
        placeholders[token] = fragment
        return token

    rendered = re.sub(
        r"`([^`]+)`",
        lambda match: stash(f"<code>{match.group(1)}</code>"),
        rendered,
    )
    rendered = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        lambda match: (
            stash(
                f'<a href="{html_escape(match.group(2), quote=True)}">'
                f"{match.group(1)}</a>"
            )
        ),
        rendered,
    )
    rendered = _linkify_escaped_urls(rendered)
    rendered = re.sub(
        r"\*\*([^*]+)\*\*",
        lambda match: f"<strong>{match.group(1)}</strong>",
        rendered,
    )
    rendered = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)",
        lambda match: f"<em>{match.group(1)}</em>",
        rendered,
    )
    for token, html in image_html.items():
        rendered = rendered.replace(token, html)
    for token, fragment in placeholders.items():
        rendered = rendered.replace(token, fragment)
    return rendered


def _render_markdown_subset_to_html(
    markdown_text: str,
    *,
    image_html: dict[str, str],
) -> str:
    """Render the supported Markdown subset into HTML."""
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    i = 0
    pending_blank_line = False

    def append_block(block: str) -> None:
        nonlocal pending_blank_line
        if pending_blank_line and blocks and blocks[-1] != "<div><br></div>":
            blocks.append("<div><br></div>")
        pending_blank_line = False
        blocks.append(block)

    def render_heading(level: int, text: str) -> str:
        body = _render_inline_markdown(text, image_html=image_html)
        if level == 1:
            return (
                '<div><h1 style="font-size: 15.0pt; font-weight: bold;">'
                f"{body}</h1></div>"
            )
        if level == 2:
            return (
                '<div><h2 style="font-size: 13.5pt; font-weight: bold;">'
                f"{body}</h2></div>"
            )
        return (
            '<div><h3 style="font-size: 12pt; font-weight: bold;">'
            f"{body}</h3></div>"
        )

    def flush_paragraph(paragraph_lines: list[str]) -> None:
        if not paragraph_lines:
            return
        body = "<br>".join(
            _render_inline_markdown(line, image_html=image_html)
            for line in paragraph_lines
        )
        append_block(f"<div>{body}</div>")

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            pending_blank_line = True
            i += 1
            continue

        if stripped.startswith("```"):
            fence_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                fence_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            append_block(
                f"<pre><code>{html_escape('\n'.join(fence_lines))}</code></pre>"
            )
            continue

        if _TABLE_SEPARATOR_RE.match(lines[i + 1].strip()) if i + 1 < len(lines) else False:
            table_lines = [line]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                candidate = lines[i].strip()
                if not candidate:
                    break
                if candidate.startswith("#") or candidate.startswith("```"):
                    break
                table_lines.append(lines[i])
                i += 1
            header_cells = _split_table_row(table_lines[0])
            body_rows = [_split_table_row(row) for row in table_lines[1:]]
            table_parts = ["<table><thead><tr>"]
            for cell in header_cells:
                table_parts.append(
                    f"<th>{_render_inline_markdown(cell, image_html=image_html)}</th>"
                )
            table_parts.append("</tr></thead><tbody>")
            for row in body_rows:
                table_parts.append("<tr>")
                for cell in row:
                    table_parts.append(
                        f"<td>{_render_inline_markdown(cell, image_html=image_html)}</td>"
                    )
                table_parts.append("</tr>")
            table_parts.append("</tbody></table>")
            append_block("".join(table_parts))
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            append_block(render_heading(level, heading_match.group(2)))
            i += 1
            continue

        if re.match(r"^\s*[-*]\s+.+$", line):
            items: list[str] = []
            while i < len(lines):
                match = re.match(r"^\s*[-*]\s+(.+)$", lines[i])
                if not match:
                    break
                items.append(
                    f"<div>- {_render_inline_markdown(match.group(1), image_html=image_html)}</div>"
                )
                i += 1
            for item in items:
                append_block(item)
            continue

        if _ORDERED_LIST_RE.match(line):
            items: list[str] = []
            number = 1
            while i < len(lines):
                match = _ORDERED_LIST_RE.match(lines[i])
                if not match:
                    break
                items.append(
                    f"<div>{number}. {_render_inline_markdown(match.group('body'), image_html=image_html)}</div>"
                )
                number += 1
                i += 1
            for item in items:
                append_block(item)
            continue

        if re.match(r"^\s*>\s*.+$", line):
            items: list[str] = []
            while i < len(lines):
                match = re.match(r"^\s*>\s*(.+)$", lines[i])
                if not match:
                    break
                items.append(
                    "<div><font color=\"#666666\">&gt; "
                    + _render_inline_markdown(match.group(1), image_html=image_html)
                    + "</font></div>"
                )
                i += 1
            for item in items:
                append_block(item)
            continue

        paragraph_lines = [line]
        i += 1
        while i < len(lines):
            candidate = lines[i]
            candidate_stripped = candidate.strip()
            if not candidate_stripped:
                break
            if candidate_stripped.startswith("```"):
                break
            if re.match(r"^(#{1,3})\s+.+$", candidate_stripped):
                break
            if re.match(r"^\s*[-*]\s+.+$", candidate):
                break
            if _ORDERED_LIST_RE.match(candidate):
                break
            if i + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[i + 1].strip()):
                break
            paragraph_lines.append(candidate)
            i += 1
        flush_paragraph(paragraph_lines)

    return "".join(blocks) or "<div><br></div>"


def _read_template_image_data(
    *,
    image_key: str,
    image_path: str,
    allowed_paths: list[str],
    base_dir: Path,
    alt_text: str,
) -> str:
    """Load one template image and return an HTML img tag."""
    target = resolve_allowed_path(image_path, allowed_paths, base_dir)
    if target is None:
        raise ValueError(f"images.{image_key} is outside allowed paths: {image_path}")
    if not target.exists():
        raise FileNotFoundError(f"image not found: {image_path}")
    media_type = IMAGE_MIME_TYPES.get(target.suffix.lower())
    if media_type is None:
        raise ValueError(
            f"unsupported image format for {image_key}: {target.suffix.lower()}"
        )
    payload = base64.b64encode(target.read_bytes()).decode("ascii")
    escaped_alt = html_escape(alt_text or image_key, quote=True)
    return (
        f'<img src="data:{media_type};base64,{payload}" alt="{escaped_alt}">'
    )


def _render_note_template_html(
    *,
    template_markdown: str,
    variables: dict[str, str],
    images: dict[str, str],
    allowed_paths: list[str],
    base_dir: Path,
) -> str:
    """Render a Markdown template plus variables/images into Notes HTML."""
    image_tokens: dict[str, str] = {}
    image_counter = 0

    def allocate_image_token(image_key: str, alt_text: str) -> str:
        nonlocal image_counter
        if image_key not in images:
            raise ValueError(f"template references unknown image placeholder: {image_key}")
        token = f"__CHAT_AGENT_IMAGE_{image_counter}__"
        image_counter += 1
        image_tokens[token] = _read_template_image_data(
            image_key=image_key,
            image_path=images[image_key],
            allowed_paths=allowed_paths,
            base_dir=base_dir,
            alt_text=alt_text,
        )
        return token

    template_with_markdown_images = _MARKDOWN_IMAGE_RE.sub(
        lambda match: allocate_image_token(
            match.group("ref"),
            match.group("alt"),
        ),
        template_markdown,
    )

    def replace_template_var(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in variables:
            return variables[name]
        if name in images:
            return allocate_image_token(name, name)
        raise ValueError(f"template references unknown placeholder: {name}")

    rendered_markdown = _TEMPLATE_VAR_RE.sub(
        replace_template_var,
        template_with_markdown_images,
    )
    return _render_markdown_subset_to_html(
        rendered_markdown,
        image_html=image_tokens,
    )


def _ensure_note_title_html(note_html: str, title: str | None) -> str:
    """Guarantee that Notes sees the requested title as the first visible line."""
    if not title:
        return note_html
    first_line = _first_visible_markdown_line(_html_to_markdown(note_html))
    if first_line == title.strip():
        return note_html
    return (
        '<div><h1 style="font-size: 15.0pt; font-weight: bold;">'
        f"{html_escape(title)}</h1></div><div><br></div>{note_html}"
    )


def _apple_notes_cache_filename(note_id: str) -> str:
    """Build a stable cache filename for a note id."""
    digest = hashlib.sha256(note_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist JSON data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _load_json_file(path: Path) -> dict[str, Any] | None:
    """Load JSON from disk when present and valid."""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _coerce_note_content_kind(*, source_url: str | None, has_images: bool) -> str:
    """Classify the rendered note content for the LLM."""
    if source_url and has_images:
        return "web_clip_image"
    if source_url:
        return "web_clip_text"
    if has_images:
        return "mixed_note"
    return "plain_note"


def _applescript_utf8_file_read(name: str) -> str:
    """Return AppleScript that reads a UTF-8 temp file for the given variable."""
    return f'my readUtf8EnvFile("{name}")'
