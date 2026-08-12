"""Tests for temp-memory digest distillation (design doc component 3a).

digest_day here is a plain callable (Callable[[date, str, int], str]) --
in production it is worker_dispatch.digest_day_via_worker bound to a
WorkerRunner (see test_memory_curation_worker_dispatch.py), but
check_and_archive_buffers itself does not know or care what produces the
text, so these tests exercise it with simple stubs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from lincy.core.schema import MaintenanceCurateConfig, MemoryArchiveConfig
from lincy.memory import hooks as hooks_module
from lincy.memory.hooks import _format_digest, _parse_recent_by_date, check_and_archive_buffers


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

    result = check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=lambda day, content, max_chars: "Keep the agreement and follow-up.",
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

    def _raise(day, content, max_chars):
        raise RuntimeError("worker unavailable")

    result = check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=_raise,
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


# -- digest marker dedupe (item 5: code must strip what the model echoes back) --


def test_format_digest_strips_doubled_marker_from_model_output():
    day = date(2030, 1, 1)
    doubled = f"- [digest {day.isoformat()}] - [digest {day.isoformat()}] real digest body"

    line = _format_digest(day, doubled)

    assert line.count("[digest") == 1
    assert "real digest body" in line


def test_format_digest_strips_bracket_only_marker_without_dash():
    day = date(2030, 1, 1)
    text = f"[digest {day.isoformat()}] body without a leading dash"

    line = _format_digest(day, text)

    assert line.count("[digest") == 1
    assert "body without a leading dash" in line


def test_check_and_archive_buffers_normalizes_doubled_marker_end_to_end(
    tmp_path: Path, monkeypatch
):
    workspace = _workspace(tmp_path)
    today = date(2030, 1, 10)
    old = today - timedelta(days=4)
    temp = workspace / "memory/agent/temp-memory.md"
    temp.write_text("# Recent\n\n" + _entry(old, "important agreement"))
    monkeypatch.setattr(hooks_module, "tz_now", lambda: datetime(2030, 1, 10, tzinfo=timezone.utc))

    doubled = f"- [digest {old.isoformat()}] - [digest {old.isoformat()}] real digest text"
    result = check_and_archive_buffers(
        workspace,
        MemoryArchiveConfig(retain_days=3),
        curate_config=_curate_config(),
        digest_day=lambda *_args: doubled,
    )

    remaining = temp.read_text()
    assert len(result.archived) == 1
    digest_line = next(line for line in remaining.splitlines() if line.startswith("- [digest"))
    assert digest_line.count("[digest") == 1
    assert "real digest text" in digest_line
