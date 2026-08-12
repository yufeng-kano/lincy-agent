"""Tests for lincy.memory.curation.worker_dispatch -- worker-driven execution.

Replaces the old MemoryCurator-based tests: file curation is now dispatched
through a WorkerRunner (real class in production, faked here) instead of a
raw LLM client whose text the code applies directly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from lincy.memory.curation.queue import load_queue
from lincy.memory.curation.worker_dispatch import (
    curate_queue_via_worker,
    digest_day_via_worker,
)
from lincy.worker.runner import WorkerResult


def _ok_result(text: str = "done") -> WorkerResult:
    return WorkerResult(
        success=True, text=text, turns_used=1, tokens_used=10, duration_ms=1, truncated=False
    )


def _fail_result(error: str = "boom") -> WorkerResult:
    return WorkerResult(
        success=False,
        text="",
        turns_used=1,
        tokens_used=10,
        duration_ms=1,
        truncated=False,
        error=error,
    )


class FakeWorkerRunner:
    """Stand-in for WorkerRunner: records calls, returns a fixed result."""

    def __init__(self, result: WorkerResult, *, on_call=None):
        self._result = result
        self.calls: list[dict] = []
        self._on_call = on_call

    def run(self, prompt, **kwargs):
        if self._on_call is not None:
            self._on_call(prompt, kwargs)
        self.calls.append({"prompt": prompt, **kwargs})
        return self._result


# -- digest_day_via_worker (component 3a: read-only, code assembles the line) --


def test_digest_day_via_worker_returns_worker_text():
    runner = FakeWorkerRunner(_ok_result("a concise digest"))

    digest = digest_day_via_worker(runner, date(2030, 1, 1), "raw content", 500)

    assert digest == "a concise digest"
    assert len(runner.calls) == 1
    assert "raw content" in runner.calls[0]["prompt"]
    assert runner.calls[0]["worker_label"] == "maintenance-digest"


def test_digest_day_via_worker_raises_on_failure():
    runner = FakeWorkerRunner(_fail_result("provider timeout"))

    with pytest.raises(ValueError, match="provider timeout"):
        digest_day_via_worker(runner, date(2030, 1, 1), "raw content", 500)


def test_digest_day_via_worker_raises_on_empty_output():
    runner = FakeWorkerRunner(_ok_result("   "))

    with pytest.raises(ValueError):
        digest_day_via_worker(runner, date(2030, 1, 1), "raw content", 500)


# -- curate_queue_via_worker (component 3b: snapshot then dispatch worker) -----


def _workspace_with_queue(tmp_path: Path, rel_path: str, content: str) -> Path:
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "memory-curation-queue.json").write_text(json.dumps([{"path": rel_path}]))
    return target


def test_curate_queue_snapshots_before_dispatching_worker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "lincy.memory.curation.worker_dispatch.tz_now",
        lambda: datetime(2030, 1, 10, tzinfo=timezone.utc),
    )
    original = "# Alice\n\nLong history\n"
    _workspace_with_queue(tmp_path, "memory/people/alice.md", original)

    seen: dict[str, object] = {}

    def _on_call(prompt, kwargs):
        snapshot = tmp_path / "memory/archive/curation/memory/people/alice.md/2030-01-10.md"
        seen["exists"] = snapshot.exists()
        seen["content"] = snapshot.read_text() if snapshot.exists() else None

    runner = FakeWorkerRunner(_ok_result(), on_call=_on_call)

    curate_queue_via_worker(runner, tmp_path)

    assert seen["exists"] is True
    assert seen["content"] == original
    assert len(runner.calls) == 1
    assert runner.calls[0]["context_files"] == [
        "kernel/builtin-skills/memory-maintenance/references/rules.md"
    ]
    assert "memory/people/alice.md" in runner.calls[0]["prompt"]


def test_curate_queue_success_removes_queue_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "lincy.memory.curation.worker_dispatch.tz_now",
        lambda: datetime(2030, 1, 10, tzinfo=timezone.utc),
    )
    _workspace_with_queue(tmp_path, "memory/people/alice.md", "content\n")
    runner = FakeWorkerRunner(_ok_result())

    curate_queue_via_worker(runner, tmp_path)

    assert load_queue(tmp_path) == []


def test_curate_queue_worker_failure_leaves_file_and_queue_entry_intact(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "lincy.memory.curation.worker_dispatch.tz_now",
        lambda: datetime(2030, 1, 10, tzinfo=timezone.utc),
    )
    original = "content that must survive\n"
    target = _workspace_with_queue(tmp_path, "memory/people/alice.md", original)
    runner = FakeWorkerRunner(_fail_result("worker crashed"))

    curate_queue_via_worker(runner, tmp_path)

    assert target.read_text() == original
    entries = load_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0]["path"] == "memory/people/alice.md"


def test_curate_queue_archive_path_is_rejected_and_never_dispatched(tmp_path: Path):
    archive_file = tmp_path / "memory/archive/temp-memory/2030-01-01.md"
    archive_file.parent.mkdir(parents=True)
    archive_file.write_text("archived content\n")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "memory-curation-queue.json").write_text(
        json.dumps([{"path": "memory/archive/temp-memory/2030-01-01.md"}])
    )
    runner = FakeWorkerRunner(_ok_result())

    curate_queue_via_worker(runner, tmp_path)

    assert runner.calls == []
    entries = load_queue(tmp_path)
    assert len(entries) == 1


def test_curate_queue_skips_malformed_entry(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "memory-curation-queue.json").write_text(json.dumps([{"path": 123}]))
    runner = FakeWorkerRunner(_ok_result())

    curate_queue_via_worker(runner, tmp_path)

    assert runner.calls == []
