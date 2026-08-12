"""Shared char-budget and ignore-pattern rules for memory file governance.

Used by both the per-write memory_edit warning check (editor/service.py)
and the periodic full scan (scan.py, design doc component 2b) so the two
apply identical rules -- the scan exists specifically to catch stock files
that never go through a write-triggered check.
"""

from __future__ import annotations

from pathlib import Path

from ...core.schema import MemoryEditWarningsConfig


def matches_ignore_patterns(target: Path, rel_path: str, patterns: list[str]) -> bool:
    """Match warning/scan ignore patterns against workspace-relative paths."""
    for pattern in patterns:
        if pattern.endswith("/"):
            if f"/{pattern}" in f"/{rel_path}" or rel_path.startswith(pattern):
                return True
        elif target.name == pattern:
            return True
    return False


def resolve_budget(
    target: Path,
    rel_path: str,
    config: MemoryEditWarningsConfig,
) -> int:
    """Return the first matching per-pattern budget override or the default."""
    for override in config.budgets:
        if matches_ignore_patterns(target, rel_path, [override.pattern]):
            return override.max_chars
    return config.max_chars
