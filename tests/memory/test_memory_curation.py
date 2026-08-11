"""Tests for LLM-backed memory curation."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from lincy.core.schema import MaintenanceCurateConfig, MemoryArchiveConfig
from lincy.memory import hooks as hooks_module
from lincy.memory.curator import MemoryCurator
from lincy.memory.hooks import _parse_recent_by_date, check_and_archive_buffers


class StubCuratorClient:
    def __init__(self, response: str | Exception):
        self.response = response
        self.calls: list[list[object]] = []

    def chat(self, messages, **kwargs):
        del kwargs
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "memory" / "agent").mkdir(parents=True)
    return tmp_path


def _entry(day: date, text: str) -> str:
    return f"- [{day.isoformat()} 12:00] {text}\n"


def _curate_config(**overrides: object) -> MaintenanceCurateConfig:
    return MaintenanceCurateConfig.model_validate({"digest_retain_days": 14, **overrides})


def test_digest_success_archives_full_text_and_replaces_it(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    today = date(2030, 1, 10)
    old = today - timedelta(days=4)
    temp = workspace / "memory/agent/temp-memory.md"
    temp.write_text("# Recent\n\n" + _entry(old, "important agreement") + _entry(today, "today"))
    monkeypatch.setattr(hooks_module, "tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))

    curator = MemoryCurator(
        StubCuratorClient("Keep the agreement and follow-up."),
        "system",
    )
    result = check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=curator.digest_day,
    )

    remaining = temp.read_text()
    assert len(result.archived) == 1
    assert "important agreement" not in remaining
    assert f"[digest {old.isoformat()}] Keep the agreement" in remaining
    archive = workspace / f"memory/archive/temp-memory/{old.isoformat()}.md"
    assert "important agreement" in archive.read_text()


def test_digest_llm_failure_leaves_full_text_in_temp_memory(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    today = date(2030, 1, 10)
    old = today - timedelta(days=4)
    temp = workspace / "memory/agent/temp-memory.md"
    original = "# Recent\n\n" + _entry(old, "important agreement")
    temp.write_text(original)
    monkeypatch.setattr(hooks_module, "tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))

    curator = MemoryCurator(StubCuratorClient(RuntimeError("LLM unavailable")), "system")
    result = check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=curator.digest_day,
    )

    assert not result.archived
    assert temp.read_text() == original


def test_digest_line_does_not_match_archive_date_parser():
    digest = "- [digest 2030-01-01] retained context\n"
    preamble, dated = _parse_recent_by_date("# Recent\n\n" + digest)
    assert digest in preamble
    assert dated == {}


def test_expired_digests_are_removed(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    today = date(2030, 1, 20)
    expired = today - timedelta(days=15)
    retained = today - timedelta(days=14)
    temp = workspace / "memory/agent/temp-memory.md"
    temp.write_text(
        "# Recent\n\n"
        f"- [digest {expired.isoformat()}] expired\n"
        f"- [digest {retained.isoformat()}] retained\n"
    )
    monkeypatch.setattr(hooks_module, "tz_now", lambda: datetime(2030, 1, 20, tzinfo=timezone.utc))

    check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=lambda *_args: "unused",
    )

    remaining = temp.read_text()
    assert "expired" not in remaining
    assert "retained" in remaining


def test_disabled_curation_preserves_legacy_archive_behavior(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    today = date(2030, 1, 10)
    old = today - timedelta(days=4)
    temp = workspace / "memory/agent/temp-memory.md"
    temp.write_text("# Recent\n\n" + _entry(old, "old full text"))
    monkeypatch.setattr(hooks_module, "tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))

    check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(enabled=False),
    )

    assert "old full text" not in temp.read_text()
    assert "old full text" in (workspace / f"memory/archive/temp-memory/{old.isoformat()}.md").read_text()


def test_queue_curation_writes_snapshot_before_rewrite(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    source = workspace / "memory/people/alice.md"
    source.parent.mkdir(parents=True)
    original = "# Alice\n\nLong history\n"
    source.write_text(original)
    (workspace / "state").mkdir()
    (workspace / "state/memory-curation-queue.json").write_text(
        json.dumps([{"path": "memory/people/alice.md"}])
    )
    monkeypatch.setattr("lincy.memory.curator.tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))
    curator = MemoryCurator(StubCuratorClient("# Current\n\nCondensed history"), "system")

    curator.curate_queue(workspace)

    snapshot = workspace / "memory/archive/curation/memory/people/alice.md/2030-01-10.md"
    assert snapshot.read_text() == original
    assert source.read_text().startswith("# Current")
    assert "Full archive: memory/archive/curation/memory/people/alice.md/2030-01-10.md" in source.read_text()
    assert json.loads((workspace / "state/memory-curation-queue.json").read_text()) == []


def test_snapshot_failure_aborts_rewrite_and_keeps_queue(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    source = workspace / "memory/people/alice.md"
    source.parent.mkdir(parents=True)
    original = "original\n"
    source.write_text(original)
    (workspace / "state").mkdir()
    queue = workspace / "state/memory-curation-queue.json"
    queue.write_text(json.dumps([{"path": "memory/people/alice.md"}]))
    snapshot = workspace / "memory/archive/curation/memory/people/alice.md/2030-01-10.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("different existing snapshot\n")
    monkeypatch.setattr("lincy.memory.curator.tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))
    client = StubCuratorClient("rewritten")

    MemoryCurator(client, "system").curate_queue(workspace)

    assert source.read_text() == original
    assert json.loads(queue.read_text()) == [{"path": "memory/people/alice.md"}]
    assert client.calls == []
