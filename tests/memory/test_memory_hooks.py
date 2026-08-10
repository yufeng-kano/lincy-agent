"""Tests for memory.hooks -- rolling buffer auto-archive."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from lincy.core.schema import MemoryArchiveConfig
from lincy.memory import hooks as hooks_module
from lincy.memory.hooks import (
    check_and_archive_buffers,
    _parse_recent_by_date,
)


# -- helpers -------------------------------------------------------------------

def _make_workspace(tmp_path: Path) -> Path:
    """Create minimal workspace directory structure."""
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "agent").mkdir()
    (tmp_path / "memory" / "agent" / "journal").mkdir()
    return tmp_path


def _write(tmp_path: Path, rel_path: str, content: str):
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _today() -> date:
    return date.today()


def _days_ago(n: int) -> date:
    return _today() - timedelta(days=n)


def _build_recent(entries: dict[date, list[str]]) -> str:
    """Build a recent.md from {date: [event_lines]}."""
    parts = ["# 近期記憶\n\n"]
    for d in sorted(entries):
        for event in entries[d]:
            parts.append(f"- [{d.isoformat()} 12:00] {event}\n")
    return "".join(parts)


# -- parser unit tests ---------------------------------------------------------

class TestParseRecent:
    def test_basic_grouping(self):
        content = (
            "# 近期記憶\n"
            "\n"
            "- [2026-02-08 12:00] Event A\n"
            "- [2026-02-08 13:00] Event B\n"
            "- [2026-02-09 10:00] Event C\n"
        )
        preamble, result = _parse_recent_by_date(content)
        assert preamble == "# 近期記憶\n\n"
        assert set(result.keys()) == {date(2026, 2, 8), date(2026, 2, 9)}
        assert result[date(2026, 2, 8)].count("\n") == 2
        assert result[date(2026, 2, 9)].count("\n") == 1

    def test_preamble_preserved(self):
        content = "# 近期記憶\n\n- [2026-02-10 09:00] First\n"
        preamble, result = _parse_recent_by_date(content)
        assert preamble == "# 近期記憶\n\n"
        assert date(2026, 2, 10) in result

    def test_empty_content(self):
        preamble, result = _parse_recent_by_date("")
        assert preamble == ""
        assert result == {}

    def test_preamble_only(self):
        content = "# 近期記憶\n\n"
        preamble, result = _parse_recent_by_date(content)
        assert preamble == "# 近期記憶\n\n"
        assert result == {}

    def test_same_date_multiple_entries(self):
        content = (
            "- [2026-02-08 10:00] Morning event\n"
            "- [2026-02-08 14:00] Afternoon event\n"
            "- [2026-02-08 20:00] Evening event\n"
        )
        _, result = _parse_recent_by_date(content)
        assert len(result) == 1
        assert "Morning" in result[date(2026, 2, 8)]
        assert "Evening" in result[date(2026, 2, 8)]


# -- integration tests ---------------------------------------------------------

class TestCheckAndArchive:
    def test_skip_when_file_missing(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        config = MemoryArchiveConfig(retain_days=3)

        result = check_and_archive_buffers(wd, config)
        assert not result.archived

    def test_archive_recent_by_date(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        today = _today()
        entries = {
            _days_ago(5): [f"old_event_{i}" for i in range(20)],
            _days_ago(4): [f"old_event_4_{i}" for i in range(20)],
            _days_ago(2): [f"recent_event_{i}" for i in range(10)],
            today: [f"today_event_{i}" for i in range(10)],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        result = check_and_archive_buffers(wd, config)

        assert result.archived
        assert result.total_lines > 0

        # Original should only contain recent + today, with preamble preserved
        remaining = (wd / "memory/agent/temp-memory.md").read_text()
        assert remaining.startswith("# 近期記憶\n")
        assert "old_event_0" not in remaining
        assert "old_event_4_0" not in remaining
        assert "recent_event_0" in remaining
        assert "today_event_0" in remaining

        # Archive files created
        archive_dir = wd / "memory/archive/temp-memory"
        assert archive_dir.is_dir()
        assert (archive_dir / f"{_days_ago(5).isoformat()}.md").is_file()
        assert (archive_dir / f"{_days_ago(4).isoformat()}.md").is_file()
        assert not (archive_dir / f"{_days_ago(2).isoformat()}.md").exists()

        # Index updated
        index = (archive_dir / "index.md").read_text()
        assert _days_ago(5).isoformat() in index
        assert _days_ago(4).isoformat() in index

    def test_new_archive_file_has_date_title(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        old_date = _days_ago(5)
        entries = {
            old_date: ["old_event"],
            _today(): ["today_event"],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        check_and_archive_buffers(wd, config)

        archive_file = wd / "memory/archive/temp-memory" / f"{old_date.isoformat()}.md"
        content = archive_file.read_text()
        assert content.startswith(f"# {old_date.isoformat()}\n")

    def test_archive_appends_to_existing(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        old_date = _days_ago(5)

        # Pre-existing archive file
        archive_dir = wd / "memory/archive/temp-memory"
        archive_dir.mkdir(parents=True)
        existing_file = archive_dir / f"{old_date.isoformat()}.md"
        existing_file.write_text("# Previously archived\n", encoding="utf-8")

        entries = {old_date: [f"new_old_event_{i}" for i in range(5)], _today(): ["today"]}
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        check_and_archive_buffers(wd, config)

        content = existing_file.read_text()
        assert "Previously archived" in content
        assert "new_old_event_0" in content

    def test_idempotent_rerun(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        entries = {
            _days_ago(5): [f"old_{i}" for i in range(10)],
            _today(): [f"today_{i}" for i in range(5)],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        # First run
        r1 = check_and_archive_buffers(wd, config)
        assert r1.archived

        # Second run: old entries already moved, nothing to archive
        r2 = check_and_archive_buffers(wd, config)
        assert not r2.archived

    def test_no_old_entries_to_archive(self, tmp_path: Path):
        """All entries within retain window -- no archival."""
        wd = _make_workspace(tmp_path)
        entries = {
            _days_ago(2): [f"event_{i}" for i in range(30)],
            _days_ago(1): [f"event_{i}" for i in range(30)],
            _today(): [f"event_{i}" for i in range(30)],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        result = check_and_archive_buffers(wd, config)
        assert not result.archived

    def test_archive_result_summary(self, tmp_path: Path):
        wd = _make_workspace(tmp_path)
        entries = {
            _days_ago(5): [f"old_{i}" for i in range(20)],
            _today(): [f"today_{i}" for i in range(5)],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        result = check_and_archive_buffers(wd, config)
        assert "archived" in result.summary
        assert _days_ago(5).isoformat() in result.summary

    def test_preamble_preserved_after_archive(self, tmp_path: Path):
        """Verify # 近期記憶 header is retained after archival."""
        wd = _make_workspace(tmp_path)
        entries = {
            _days_ago(5): ["old_event"],
            _today(): ["today_event"],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))
        config = MemoryArchiveConfig(retain_days=3)

        check_and_archive_buffers(wd, config)

        remaining = (wd / "memory/agent/temp-memory.md").read_text()
        assert remaining.startswith("# 近期記憶\n\n")
        assert "today_event" in remaining
        assert "old_event" not in remaining

    def test_archive_uses_app_timezone_date(self, tmp_path: Path, monkeypatch):
        """Archive cutoff must follow app timezone, not process-local date.today()."""
        wd = _make_workspace(tmp_path)
        fake_now = datetime(2030, 1, 5, 0, 30, tzinfo=timezone(timedelta(hours=8)))
        monkeypatch.setattr(hooks_module, "tz_now", lambda: fake_now)

        entries = {
            date(2030, 1, 3): ["old_event"],
            date(2030, 1, 4): ["yesterday_event"],
            date(2030, 1, 5): ["today_event"],
        }
        _write(wd, "memory/agent/temp-memory.md", _build_recent(entries))

        result = check_and_archive_buffers(wd, MemoryArchiveConfig(retain_days=1))

        assert [item.date for item in result.archived] == [date(2030, 1, 3)]
        remaining = (wd / "memory/agent/temp-memory.md").read_text()
        assert "old_event" not in remaining
        assert "yesterday_event" in remaining
        assert "today_event" in remaining
