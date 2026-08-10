"""Tests for memory.backup -- periodic memory zip backup."""

from datetime import datetime, timedelta

from lincy.timezone_utils import get_tz
from pathlib import Path
import zipfile

import pytest

from lincy.core.schema import MemoryBackupConfig
from lincy.memory.backup import MemoryBackupManager, _parse_filename_timestamp


# -- helpers -------------------------------------------------------------------

def _make_workspace(tmp_path: Path) -> Path:
    """Create minimal workspace with memory dir and sample files."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "agent").mkdir()
    (mem / "agent" / "persona.md").write_text("# persona\n", encoding="utf-8")
    (mem / "agent" / "recent.md").write_text("some notes\n", encoding="utf-8")
    return tmp_path


def _make_backup_file(backup_dir: Path, name: str) -> Path:
    """Create a dummy zip in the backup directory."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("dummy.txt", "placeholder")
    return p


# -- _parse_filename_timestamp -------------------------------------------------

class TestParseFilenameTimestamp:
    def test_valid(self):
        ts = _parse_filename_timestamp("memory_20260214_153045.zip")
        assert ts == datetime(2026, 2, 14, 15, 30, 45, tzinfo=get_tz())

    def test_invalid_format(self):
        assert _parse_filename_timestamp("random_file.zip") is None

    def test_short_name(self):
        assert _parse_filename_timestamp("a.zip") is None


# -- MemoryBackupManager.check_and_backup --------------------------------------

class TestCheckAndBackup:
    def test_creates_backup_on_first_call(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig())
        result = mgr.check_and_backup()
        assert result is not None
        assert result.exists()
        assert result.suffix == ".zip"

    def test_skips_when_interval_not_elapsed(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig(interval_minutes=60))
        first = mgr.check_and_backup()
        assert first is not None
        second = mgr.check_and_backup()
        assert second is None

    def test_creates_again_after_interval(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig(interval_minutes=1))
        first = mgr.check_and_backup()
        assert first is not None
        # Simulate time passing
        from lincy.timezone_utils import now as tz_now
        mgr._last_backup = tz_now() - timedelta(minutes=2)
        second = mgr.check_and_backup()
        assert second is not None
        # Both are valid zip files in the backup dir
        assert second.exists()
        backup_dir = ws / "backups" / "memory"
        zips = list(backup_dir.glob("*.zip"))
        assert len(zips) >= 1

    def test_returns_none_when_no_memory_dir(self, tmp_path):
        # No memory/ directory
        mgr = MemoryBackupManager(tmp_path, MemoryBackupConfig())
        assert mgr.check_and_backup() is None


# -- MemoryBackupManager._create_backup ----------------------------------------

class TestCreateBackup:
    def test_zip_contains_all_files(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig())
        now = datetime.now()
        path = mgr._create_backup(now)

        with zipfile.ZipFile(path, "r") as zf:
            names = sorted(zf.namelist())
        assert "agent/persona.md" in names
        assert "agent/recent.md" in names

    def test_backup_dir_created(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig())
        mgr._create_backup(datetime.now())
        assert (ws / "backups" / "memory").is_dir()

    def test_filename_format(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig())
        now = datetime(2026, 2, 14, 10, 30, 0, tzinfo=get_tz())
        path = mgr._create_backup(now)
        assert path.name == "memory_20260214_103000.zip"


# -- MemoryBackupManager._cleanup_expired --------------------------------------

class TestCleanupExpired:
    def test_deletes_expired_files(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig(retention_minutes=60))
        backup_dir = ws / "backups" / "memory"

        # Old file (2 hours ago)
        _make_backup_file(backup_dir, "memory_20260214_080000.zip")
        # Recent file
        _make_backup_file(backup_dir, "memory_20260214_100000.zip")

        now = datetime(2026, 2, 14, 10, 30, 0, tzinfo=get_tz())
        deleted = mgr._cleanup_expired(now)
        assert deleted == 1
        assert not (backup_dir / "memory_20260214_080000.zip").exists()
        assert (backup_dir / "memory_20260214_100000.zip").exists()

    def test_keeps_all_when_none_expired(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig(retention_minutes=1440))
        backup_dir = ws / "backups" / "memory"

        _make_backup_file(backup_dir, "memory_20260214_100000.zip")

        now = datetime(2026, 2, 14, 10, 30, 0, tzinfo=get_tz())
        deleted = mgr._cleanup_expired(now)
        assert deleted == 0

    def test_no_backup_dir(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig())
        assert mgr._cleanup_expired(datetime.now()) == 0

    def test_ignores_non_zip_files(self, tmp_path):
        ws = _make_workspace(tmp_path)
        mgr = MemoryBackupManager(ws, MemoryBackupConfig(retention_minutes=1))
        backup_dir = ws / "backups" / "memory"
        backup_dir.mkdir(parents=True)
        (backup_dir / "notes.txt").write_text("keep me")

        deleted = mgr._cleanup_expired(datetime(2026, 12, 31, 23, 59, 59))
        assert deleted == 0
        assert (backup_dir / "notes.txt").exists()


# -- Config integration --------------------------------------------------------

class TestConfig:
    def test_default_values(self):
        cfg = MemoryBackupConfig()
        assert cfg.enabled is True
        assert cfg.interval_minutes == 30
        assert cfg.retention_minutes == 1440

    def test_disabled(self):
        cfg = MemoryBackupConfig(enabled=False)
        assert cfg.enabled is False

    def test_custom_values(self):
        cfg = MemoryBackupConfig(interval_minutes=5, retention_minutes=60)
        assert cfg.interval_minutes == 5
        assert cfg.retention_minutes == 60

    def test_rejects_zero_interval(self):
        with pytest.raises(Exception):
            MemoryBackupConfig(interval_minutes=0)
