"""Generic append-only JSONL store for small pydantic event streams."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class JsonlStore(Generic[ModelT]):
    """Append-only JSONL store; malformed lines are skipped with a warning."""

    model_type: type[ModelT]
    max_recent_limit: int = 1000

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, record: ModelT) -> ModelT:
        """Validate and append one record atomically enough for local JSONL use."""
        validated = self.model_type.model_validate(record)
        line = validated.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return validated

    def recent_events(self, limit: int) -> list[ModelT]:
        """Return the most recent valid records in file order."""
        if not self.path.exists():
            return []
        bounded_limit = max(1, min(limit, self.max_recent_limit))
        records: list[ModelT] = []
        skipped = 0
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(self.model_type.model_validate_json(line))
            except Exception:
                skipped += 1
        if skipped:
            logger.warning("Skipped %d malformed lines in %s", skipped, self.path)
        return records[-bounded_limit:]

    def read_from_offset(self, offset: int) -> tuple[list[ModelT], int]:
        """Read valid records appended after *offset* and return the new byte offset."""
        if not self.path.exists():
            return [], 0
        file_size = self.path.stat().st_size
        start = offset if offset <= file_size else 0
        records: list[ModelT] = []
        skipped = 0
        with self.path.open("r", encoding="utf-8") as fh:
            fh.seek(start)
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    records.append(self.model_type.model_validate(json.loads(line)))
                except Exception:
                    skipped += 1
            new_offset = fh.tell()
        if skipped:
            logger.warning("Skipped %d malformed lines in %s", skipped, self.path)
        return records, new_offset
