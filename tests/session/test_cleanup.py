"""Tests for session/cleanup.py: shared cleanup function."""

from datetime import datetime, timedelta
from pathlib import Path

from lincy.timezone_utils import now as tz_now
from lincy.session.cleanup import cleanup_sessions


def _make_session_id(days_ago: int) -> str:
    """Build a session ID with a timestamp N days in the past (local time)."""
    ts = tz_now() - timedelta(days=days_ago)
    return ts.strftime("%Y%m%d_%H%M%S") + "_abc123"


class TestCleanupSessions:
    def test_no_dirs_returns_zero(self, tmp_path: Path):
        base = tmp_path / "session"
        base.mkdir()
        assert cleanup_sessions(base, retention_days=30) == 0

    def test_deletes_old_brain_sessions(self, tmp_path: Path):
        base = tmp_path / "session"
        brain_dir = base / "brain"
        brain_dir.mkdir(parents=True)

        old_id = _make_session_id(60)
        new_id = _make_session_id(5)
        (brain_dir / old_id).mkdir()
        (brain_dir / old_id / "meta.json").write_text("{}")
        (brain_dir / new_id).mkdir()
        (brain_dir / new_id / "meta.json").write_text("{}")

        deleted = cleanup_sessions(base, retention_days=30)
        assert deleted == 1
        assert not (brain_dir / old_id).exists()
        assert (brain_dir / new_id).exists()

    def test_deletes_old_gui_sessions(self, tmp_path: Path):
        base = tmp_path / "session"
        gui_dir = base / "gui"
        gui_dir.mkdir(parents=True)

        old_id = _make_session_id(45)
        new_id = _make_session_id(2)
        (gui_dir / f"{old_id}.json").write_text("{}")
        (gui_dir / f"{old_id}.steps.jsonl").write_text("")
        (gui_dir / f"{old_id}.jpg").write_bytes(b"")
        (gui_dir / f"{new_id}.json").write_text("{}")
        (gui_dir / f"{new_id}.steps.jsonl").write_text("")

        deleted = cleanup_sessions(base, retention_days=30)
        assert deleted == 3
        assert not (gui_dir / f"{old_id}.json").exists()
        assert not (gui_dir / f"{old_id}.steps.jsonl").exists()
        assert not (gui_dir / f"{old_id}.jpg").exists()
        assert (gui_dir / f"{new_id}.json").exists()
        assert (gui_dir / f"{new_id}.steps.jsonl").exists()

    def test_mixed_brain_and_gui(self, tmp_path: Path):
        base = tmp_path / "session"
        brain_dir = base / "brain"
        gui_dir = base / "gui"
        brain_dir.mkdir(parents=True)
        gui_dir.mkdir(parents=True)

        old_brain = _make_session_id(90)
        (brain_dir / old_brain).mkdir()
        old_gui = _make_session_id(100)
        (gui_dir / f"{old_gui}.json").write_text("{}")
        new_brain = _make_session_id(1)
        (brain_dir / new_brain).mkdir()

        deleted = cleanup_sessions(base, retention_days=30)
        assert deleted == 2

    def test_skips_unparseable_names(self, tmp_path: Path):
        base = tmp_path / "session"
        brain_dir = base / "brain"
        brain_dir.mkdir(parents=True)

        (brain_dir / "not_a_timestamp").mkdir()
        deleted = cleanup_sessions(base, retention_days=1)
        assert deleted == 0
        assert (brain_dir / "not_a_timestamp").exists()

    def test_retention_days_boundary(self, tmp_path: Path):
        base = tmp_path / "session"
        gui_dir = base / "gui"
        gui_dir.mkdir(parents=True)

        # Well within retention (should NOT be deleted)
        recent_id = _make_session_id(10)
        (gui_dir / f"{recent_id}.json").write_text("{}")

        # Well past retention (should be deleted)
        past_id = _make_session_id(60)
        (gui_dir / f"{past_id}.json").write_text("{}")

        deleted = cleanup_sessions(base, retention_days=30)
        assert deleted == 1
        assert (gui_dir / f"{recent_id}.json").exists()
        assert not (gui_dir / f"{past_id}.json").exists()

    def test_legacy_utc_session_ids_still_cleaned(self, tmp_path: Path):
        """Legacy UTC IDs should still be cleaned up under normal retention."""
        from datetime import timezone

        base = tmp_path / "session"
        brain_dir = base / "brain"
        brain_dir.mkdir(parents=True)

        # Legacy UTC ID from 60 days ago
        utc_old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%d_%H%M%S") + "_utc001"
        (brain_dir / utc_old).mkdir()

        # Legacy UTC ID from 2 days ago (should be kept)
        utc_new = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d_%H%M%S") + "_utc002"
        (brain_dir / utc_new).mkdir()

        deleted = cleanup_sessions(base, retention_days=30)
        assert deleted == 1
        assert not (brain_dir / utc_old).exists()
        assert (brain_dir / utc_new).exists()

    def test_legacy_utc_boundary_not_deleted_early(self, tmp_path: Path):
        """Legacy UTC IDs near cutoff should not be deleted too early."""
        from datetime import timezone

        base = tmp_path / "session"
        brain_dir = base / "brain"
        brain_dir.mkdir(parents=True)

        now_utc = datetime.now(timezone.utc)
        recent_utc = (now_utc - timedelta(hours=20)).strftime("%Y%m%d_%H%M%S") + "_utc_recent"
        old_utc = (now_utc - timedelta(hours=30)).strftime("%Y%m%d_%H%M%S") + "_utc_old"
        (brain_dir / recent_utc).mkdir()
        (brain_dir / old_utc).mkdir()

        deleted = cleanup_sessions(base, retention_days=1)
        assert deleted == 1
        assert (brain_dir / recent_utc).exists()
        assert not (brain_dir / old_utc).exists()
