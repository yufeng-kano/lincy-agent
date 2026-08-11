"""Shared JSON persistence helpers for small local stores."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
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
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
