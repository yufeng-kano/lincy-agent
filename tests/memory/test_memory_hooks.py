"""Tests for the daily snapshot of the state file into memory/archive/temp-memory/."""

import os
from datetime import date, timedelta
from pathlib import Path

from lincy.memory.hooks import snapshot_stale_state_file, snapshot_state_file
from lincy.timezone_utils import now as tz_now

STATE = (
    "# 今日現況\n\n"
    "## 最後互動\n\n- 10:00 早安\n\n"
    "## 今日時間線\n\n- 10:00 早安\n\n"
    "## 未閉環\n\n## 下一步\n"
)


def _workspace(tmp_path: Path, content: str = STATE) -> Path:
    agent_dir = tmp_path / "memory" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "temp-memory.md").write_text(content, encoding="utf-8")
    return tmp_path


def test_snapshot_creates_the_partition_and_rebuilds_the_index(tmp_path: Path):
    ws = _workspace(tmp_path)

    target = snapshot_state_file(ws, day=date(2026, 11, 7))

    assert target == ws / "memory" / "archive" / "temp-memory" / "2026-11-07.md"
    assert target.read_text(encoding="utf-8") == f"# 2026-11-07\n\n{STATE.strip()}\n"
    index = (target.parent / "index.md").read_text(encoding="utf-8")
    assert "- [2026-11-07](2026-11-07.md)" in index


def test_snapshot_appends_to_an_existing_partition_and_skips_identical_content(tmp_path: Path):
    ws = _workspace(tmp_path)
    archive = ws / "memory" / "archive" / "temp-memory"
    archive.mkdir(parents=True)
    legacy = "# 2026-11-07\n\n- [2026-11-07 00:01] legacy entry\n"
    (archive / "2026-11-07.md").write_text(legacy, encoding="utf-8")

    target = snapshot_state_file(ws, day=date(2026, 11, 7))
    text = target.read_text(encoding="utf-8")
    assert text == f"{legacy.rstrip()}\n\n---\n\n{STATE.strip()}\n"

    assert snapshot_state_file(ws, day=date(2026, 11, 7)) is None
    assert target.read_text(encoding="utf-8") == text


def test_snapshot_skips_a_missing_or_empty_state_file(tmp_path: Path):
    assert snapshot_state_file(tmp_path, day=date(2026, 11, 7)) is None

    ws = _workspace(tmp_path, content="   \n")
    assert snapshot_state_file(ws, day=date(2026, 11, 7)) is None
    assert not (ws / "memory" / "archive").exists()


def test_stale_snapshot_uses_the_day_the_file_was_written(tmp_path: Path):
    ws = _workspace(tmp_path)
    source = ws / "memory" / "agent" / "temp-memory.md"
    two_days_ago = tz_now() - timedelta(days=2)
    os.utime(source, (two_days_ago.timestamp(), two_days_ago.timestamp()))

    target = snapshot_stale_state_file(ws)

    assert target == ws / "memory" / "archive" / "temp-memory" / f"{two_days_ago.date().isoformat()}.md"
    assert target.read_text(encoding="utf-8") == f"# {two_days_ago.date().isoformat()}\n\n{STATE.strip()}\n"
    # Nothing new to archive on a second run.
    assert snapshot_stale_state_file(ws) is None

    # A file written today still describes the current day.
    source.write_text(STATE + "- 11:00 later\n", encoding="utf-8")
    assert snapshot_stale_state_file(ws) is None
    assert not (target.parent / f"{tz_now().date().isoformat()}.md").exists()
