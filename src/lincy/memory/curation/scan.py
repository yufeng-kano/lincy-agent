"""Periodic full scan for stock over-budget memory files (design doc 2b).

The queue is normally populated only when memory_edit writes a file, so a
file that is already over budget but never written again would sit
unqueued forever (e.g. long-term.md, injected as a boot file every turn,
found ~2x over budget and never once curated). Maintenance calls
scan_over_budget_files before consuming the queue to close that gap.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...core.schema import MemoryEditWarningsConfig
from .budget import matches_ignore_patterns, resolve_budget
from .queue import upsert_queue_entry

logger = logging.getLogger(__name__)

_MEMORY_REL_PATH = "memory"
_ARCHIVE_PREFIX = "memory/archive/"


def scan_over_budget_files(
    agent_os_dir: Path,
    config: MemoryEditWarningsConfig,
) -> list[str]:
    """Upsert every over-budget file under memory/ into the curation queue.

    Returns the workspace-relative paths that were queued (freshly or
    refreshed). memory/archive/ is always excluded regardless of ignore
    config -- curation must never treat an archived snapshot as a rewrite
    target.
    """
    memory_root = agent_os_dir / _MEMORY_REL_PATH
    if not memory_root.is_dir():
        return []

    queued: list[str] = []
    for path in sorted(memory_root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(agent_os_dir).as_posix()
        if rel_path.startswith(_ARCHIVE_PREFIX):
            continue
        if matches_ignore_patterns(path, rel_path, config.ignore):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Skipping unreadable file during curation scan %s: %s", path, exc
            )
            continue

        chars = len(content)
        budget = resolve_budget(path, rel_path, config)
        if chars > budget:
            upsert_queue_entry(
                agent_os_dir=agent_os_dir,
                rel_path=rel_path,
                chars=chars,
                budget=budget,
            )
            queued.append(rel_path)
    return queued
