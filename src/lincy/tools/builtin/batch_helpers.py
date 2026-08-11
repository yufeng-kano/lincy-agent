"""Shared validation helpers for built-in batch and source fields."""

from __future__ import annotations

import json
from typing import Any


def validate_source_fields(
    *,
    source_app: str | None,
    source_id: str | None,
    source_label: str | None,
) -> str | None:
    """Require a source app when source details are present."""
    if (source_id or source_label) and not source_app:
        return "Error: 'source_app' is required when source_id or source_label is set"
    return None


def format_source_result(
    source_app: str | None,
    source_label: str | None,
    source_id: str | None,
) -> str | None:
    """Format optional source metadata for a successful tool response."""
    if not source_app:
        return None
    text = source_app
    if source_label:
        text = f"{text}:{source_label}"
    if source_id:
        text = f"{text} ({source_id})"
    return text


def normalize_batch_items(
    raw: list[dict[str, Any]] | str | None,
    field_name: str,
    max_items: int,
) -> list[dict[str, Any]] | str:
    """Decode and validate a bounded array of object items."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return f"Error: '{field_name}' must be an array"
    if not isinstance(raw, list):
        return f"Error: '{field_name}' must be an array"
    if not raw:
        return f"Error: '{field_name}' must contain at least one item"
    if len(raw) > max_items:
        return f"Error: '{field_name}' supports at most {max_items} items"
    if not all(isinstance(item, dict) for item in raw):
        return f"Error: each {field_name} item must be an object"
    return raw
