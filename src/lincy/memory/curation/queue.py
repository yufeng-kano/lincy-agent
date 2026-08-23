"""Deterministic queue of over-budget memory files awaiting curation.

Structured JSON at state/memory-curation-queue.json, keyed by path. No LLM
involved: writers are the memory_edit per-write warning check and the
periodic full scan (see scan.py); the consumer is worker_dispatch.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from ...json_store import save_json

logger = logging.getLogger(__name__)

_QUEUE_REL_PATH = "state/memory-curation-queue.json"


def load_queue(agent_os_dir: Path) -> list[dict[str, object]]:
    """Return queue entries, ignoring a missing or malformed file."""
    path = agent_os_dir / _QUEUE_REL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load memory curation queue %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("Memory curation queue is not a list: %s", path)
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def remove_queue_entry(agent_os_dir: Path, rel_path: str) -> None:
    """Remove one entry by path (idempotent -- no-op if already absent)."""
    path = agent_os_dir / _QUEUE_REL_PATH
    entries = load_queue(agent_os_dir)
    retained = [entry for entry in entries if entry.get("path") != rel_path]
    save_json(path, retained)


def upsert_queue_entry(
    *,
    agent_os_dir: Path,
    rel_path: str,
    chars: int,
    budget: int,
) -> None:
    """Insert or refresh one over-budget file entry, deduped by path.

    Refreshing only bumps chars/last_seen; budget stays at the value first
    recorded so a mid-day config change does not retroactively rewrite an
    already-queued entry's history.
    """
    path = agent_os_dir / _QUEUE_REL_PATH
    now = datetime.now(timezone.utc).isoformat()
    entries = load_queue(agent_os_dir)

    for entry in entries:
        if entry.get("path") != rel_path:
            continue
        entry["chars"] = chars
        entry["last_seen"] = now
        break
    else:
        entries.append(
            {
                "path": rel_path,
                "chars": chars,
                "budget": budget,
                "first_seen": now,
                "last_seen": now,
            }
        )
    save_json(path, entries)


