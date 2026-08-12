"""Tests for lincy.memory.curation.scan -- periodic full scan (design 2b).

The queue only fills on memory_edit write, so a stock over-budget file
that is never written again never gets queued through the normal path.
scan_over_budget_files exists to close that gap.
"""

from __future__ import annotations

from pathlib import Path

from lincy.core.schema import MemoryEditWarningsConfig
from lincy.memory.curation.queue import load_queue, upsert_queue_entry
from lincy.memory.curation.scan import scan_over_budget_files


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_queues_stock_over_budget_file_never_written_again(tmp_path: Path):
    _write(tmp_path / "memory/agent/long-term.md", "a" * 20000)
    config = MemoryEditWarningsConfig(max_chars=10000)

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == ["memory/agent/long-term.md"]
    entries = load_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0]["path"] == "memory/agent/long-term.md"
    assert entries[0]["chars"] == 20000


def test_scan_skips_files_within_budget(tmp_path: Path):
    _write(tmp_path / "memory/agent/short.md", "a" * 100)
    config = MemoryEditWarningsConfig(max_chars=10000)

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == []
    assert load_queue(tmp_path) == []


def test_scan_respects_ignore_patterns(tmp_path: Path):
    _write(tmp_path / "memory/agent/temp-memory.md", "a" * 20000)
    config = MemoryEditWarningsConfig(max_chars=10000, ignore=["temp-memory.md"])

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == []
    assert load_queue(tmp_path) == []


def test_scan_never_queues_files_under_memory_archive_even_if_ignore_is_empty(tmp_path: Path):
    _write(tmp_path / "memory/archive/temp-memory/2030-01-01.md", "a" * 20000)
    config = MemoryEditWarningsConfig(max_chars=10000, ignore=[])

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == []
    assert load_queue(tmp_path) == []


def test_scan_respects_per_pattern_budget_override(tmp_path: Path):
    _write(tmp_path / "memory/people/alice/profile.md", "a" * 1500)
    config = MemoryEditWarningsConfig(
        max_chars=10000,
        budgets=[{"pattern": "people/", "max_chars": 1000}],
    )

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == ["memory/people/alice/profile.md"]


def test_scan_dedupes_against_existing_queue_entry(tmp_path: Path):
    _write(tmp_path / "memory/agent/long-term.md", "a" * 20000)
    config = MemoryEditWarningsConfig(max_chars=10000)
    upsert_queue_entry(
        agent_os_dir=tmp_path,
        rel_path="memory/agent/long-term.md",
        chars=19000,
        budget=10000,
    )

    queued = scan_over_budget_files(tmp_path, config)

    assert queued == ["memory/agent/long-term.md"]
    entries = load_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0]["chars"] == 20000


def test_scan_missing_memory_dir_returns_empty(tmp_path: Path):
    config = MemoryEditWarningsConfig(max_chars=10000)

    assert scan_over_budget_files(tmp_path, config) == []
