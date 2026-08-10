"""Shared JSON persistence helpers for small local stores."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json(path: Path, *, default: Any) -> Any:
    """Load JSON, returning default when the file is absent or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as error:
        logger.warning("Failed to load JSON store %s: %s", path, error)
        return default


def save_json(path: Path, data: Any) -> None:
    """Atomically replace a JSON store so interruption cannot corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_datetime(value: str | None, timezone) -> datetime | None:
    """Parse a persisted timestamp, assigning the store timezone if needed."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def format_source_tag(source_app: str | None, source_label: str | None) -> str | None:
    """Format the common source-app/source-label display tag."""
    if not source_app:
        return None
    if source_label:
        return f"{source_app}:{source_label}"
    return source_app
