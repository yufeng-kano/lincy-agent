"""macOS personal-app tools for Calendar, Reminders, Notes, Photos, and Mail."""

from __future__ import annotations

import base64
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape as html_escape
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any

from markdownify import markdownify

from ....llm.schema import ContentPart, Message, ToolDefinition, ToolParameter
from ....timezone_utils import get_tz
from ...security import resolve_allowed_path

logger = logging.getLogger(__name__)

_SLOW_APP_TOOL_SECONDS = 5.0
_APPLE_NOTES_CACHE_VERSION = "2"
_APPLE_NOTES_DEFAULT_SEARCH_LIMIT = 5
_APPLE_NOTES_SUMMARY_MAX_INPUT_CHARS = 20_000
_APPLE_NOTES_MAX_NOTE_WORKERS = 4
_APPLE_MAIL_DEFAULT_SCAN_LIMIT = 300
_APPLE_MAIL_MAX_SCAN_LIMIT = 2_000
_APPLE_MAIL_GET_CONTENT_MAX_CHARS = 20_000
_APPLE_MAIL_TRASH_MAX_MESSAGES = 20
_APPLE_MAIL_SCOPES = {"inbox", "sent", "drafts", "trash", "junk", "outbox", "all"}
_APPLE_MAIL_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_APPLE_NOTES_IMAGE_PROMPT = (
    "這是 Apple 備忘錄裡的內嵌圖片。"
    "請用繁體中文提取可讀文字，並簡短說明這張圖對筆記內容最重要的資訊。"
    "只回純文字，不要加前言，控制在 200 字內。"
)
_APPLE_NOTES_SUMMARY_SYSTEM_PROMPT = (
    "你是 Apple Notes 搜尋摘要器。"
    "請用繁體中文輸出 2 到 3 句短摘要，幫主模型快速判斷這則筆記值不值得打開。"
    "優先保留主題、關鍵名詞、時間、人名、待辦或決策。"
    "只回摘要，不要列點，不要補充多餘前言。"
)
_DATA_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc=(?P<quote>[\"'])(?P<src>data:image/[^\"']+)(?P=quote)[^>]*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(
    r"""href=(?P<quote>["'])(?P<href>https?://.+?)(?P=quote)""",
    flags=re.IGNORECASE | re.DOTALL,
)
_URL_TEXT_RE = re.compile(r"https?://[^\s<>\"]+")
_TEMPLATE_VAR_RE = re.compile(r"\{(?P<name>[A-Za-z0-9_]+)\}")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<ref>[A-Za-z0-9_]+)\)")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_ORDERED_LIST_RE = re.compile(r"^\s*\d+\.\s+(?P<body>.+)$")
_INLINE_URL_RE = re.compile(r"(?P<url>https?://[^\s<>\"]+)")
_NOTE_HEADING_BLOCK_RE = re.compile(
    r"""
    <div>\s*
    (?:
      <h(?P<h_level>[1-3])(?P<h_attrs>[^>]*)>(?P<h_body>.*?)</h(?P=h_level)>
      |
      <(?:b|strong)>\s*<span(?P<span_attrs>[^>]*)>(?P<span_body>.*?)</span>\s*</(?:b|strong)>
    )
    \s*(?:<br\s*/?>)?\s*
    </div>
    """,
    flags=re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

def _json_output(payload: dict[str, Any]) -> str:
    """Render tool output as stable JSON text or the standard error form."""
    if not payload.get("ok", True):
        return _error(str(payload.get("error") or "macOS app operation failed"))
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def build_partial_update_payload(values: dict[str, Any]) -> dict[str, Any]:
    """Build has-field markers and values for optional update payloads."""
    payload: dict[str, Any] = {}
    for name, value in values.items():
        payload[f"has_{name}"] = value is not None
        payload[name] = value
    return payload


def _error(message: str) -> str:
    """Build a standard tool error string."""
    return f"Error: {message}"


def _parse_local_datetime(value: str, *, field_name: str) -> datetime:
    """Parse an ISO datetime string."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


def _datetime_in_app_tz(value: datetime) -> datetime:
    """Normalize a datetime to the configured app timezone."""
    app_tz = get_tz()
    if value.tzinfo is None:
        return value.replace(tzinfo=app_tz)
    return value.astimezone(app_tz)


def _datetime_to_app_iso(value: datetime) -> str:
    """Render a datetime with the configured app timezone and explicit offset."""
    return _datetime_in_app_tz(value).isoformat(timespec="seconds")


def _parse_calendar_payload_datetime(value: str | None, *, field_name: str) -> str | None:
    """Parse a user-supplied calendar datetime and render it with app offset."""
    if value is None:
        return None
    return _datetime_to_app_iso(_parse_local_datetime(value, field_name=field_name))


def _parse_mail_range_datetime(value: str | None, *, field_name: str) -> str | None:
    """Parse a Mail date bound as local time and render it with app offset."""
    if value is None:
        return None
    text = value.strip()
    if _APPLE_MAIL_DATE_ONLY_RE.match(text):
        if field_name.endswith("_before"):
            text = f"{text}T23:59:59"
        else:
            text = f"{text}T00:00:00"
    return _datetime_to_app_iso(_parse_local_datetime(text, field_name=field_name))


def _parse_tool_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime returned by macOS tooling."""
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=get_tz())
    return parsed


_CALENDAR_DATETIME_KEYS = frozenset({"start", "end"})
_REMINDER_DATETIME_KEYS = frozenset({"due"})
_MAIL_DATETIME_KEYS = frozenset({"date", "date_received", "date_sent"})


def _localize_datetime_fields(payload: Any, *, keys: frozenset[str]) -> Any:
    """Convert tool-emitted UTC datetime fields to app-local ISO strings."""
    if isinstance(payload, list):
        return [_localize_datetime_fields(item, keys=keys) for item in payload]
    if not isinstance(payload, dict):
        return payload

    localized: dict[str, Any] = {}
    for key, value in payload.items():
        if key in keys and isinstance(value, str):
            parsed = _parse_tool_iso_datetime(value)
            localized[key] = _datetime_to_app_iso(parsed) if parsed else value
        else:
            localized[key] = _localize_datetime_fields(value, keys=keys)
    return localized


def _localize_calendar_datetime_fields(payload: Any) -> Any:
    """Convert calendar start/end fields to app-local ISO strings."""
    return _localize_datetime_fields(payload, keys=_CALENDAR_DATETIME_KEYS)


def _localize_reminder_datetime_fields(payload: Any) -> Any:
    """Convert reminder due fields to app-local ISO strings."""
    return _localize_datetime_fields(payload, keys=_REMINDER_DATETIME_KEYS)


def _localize_mail_datetime_fields(payload: Any) -> Any:
    """Convert Mail date fields to app-local ISO strings."""
    return _localize_datetime_fields(payload, keys=_MAIL_DATETIME_KEYS)


def _format_app_tool_log_details(details: dict[str, Any] | None) -> str:
    """Render a privacy-aware summary for Apple app tool diagnostics."""
    if not details:
        return "-"
    safe_pairs: list[str] = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if key in {
            "account",
            "calendar",
            "folder_path",
            "folder_id",
            "target_folder_path",
            "target_folder_id",
            "list_name",
            "list_path",
            "list_id",
            "album_name",
            "album_path",
            "album_id",
            "parent_folder_path",
            "parent_folder_id",
            "event_uid",
            "exclude_event_uid",
            "reminder_id",
            "note_id",
            "sort_by",
            "limit",
            "start",
            "end",
            "due",
            "due_start",
            "due_end",
            "favorite",
            "all_day",
            "completed",
            "flagged",
        }:
            safe_pairs.append(f"{key}={value!r}")
            continue
        if isinstance(value, str):
            safe_pairs.append(f"{key}_chars={len(value)}")
            continue
        if isinstance(value, list):
            safe_pairs.append(f"{key}_count={len(value)}")
            continue
        safe_pairs.append(f"{key}={value!r}")
    return ", ".join(safe_pairs) if safe_pairs else "-"


__all__ = [name for name in globals() if not name.startswith("__")]
