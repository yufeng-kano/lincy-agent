"""Shared session cleanup for brain and GUI sessions."""

import logging
import shutil
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from ..timezone_utils import now as tz_now

logger = logging.getLogger(__name__)

# Session ID prefix format: YYYYMMDD_HHMMSS
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_TIMESTAMP_PREFIX_LEN = 15  # len("20260215_120000")


def _parse_session_timestamp(name: str) -> datetime | None:
    """Extract creation timestamp from a session ID / filename.

    The ID format has no timezone marker, so the parsed value is naive.
    """
    prefix = name[:_TIMESTAMP_PREFIX_LEN]
    try:
        return datetime.strptime(prefix, _TIMESTAMP_FORMAT)
    except (ValueError, IndexError):
        return None


def _is_expired_session_timestamp(
    session_ts_naive: datetime,
    *,
    cutoff_local: datetime,
    app_tz: tzinfo,
) -> bool:
    """Return True only when both timestamp interpretations are expired.

    Session IDs are ambiguous:
    - New IDs are generated in app timezone.
    - Legacy IDs were generated in UTC.

    To avoid early deletion around retention boundaries, delete only when
    both interpretations are older than ``cutoff_local``.
    """
    ts_as_local = session_ts_naive.replace(tzinfo=app_tz)
    ts_as_legacy_utc = session_ts_naive.replace(tzinfo=timezone.utc).astimezone(app_tz)
    return ts_as_local < cutoff_local and ts_as_legacy_utc < cutoff_local


def cleanup_sessions(
    session_base_dir: Path,
    retention_days: int = 30,
) -> int:
    """Remove expired sessions under session/brain/ and session/gui/.

    Returns the number of deleted entries.
    """
    now_local = tz_now()
    app_tz = now_local.tzinfo
    if app_tz is None:
        app_tz = timezone.utc
    cutoff = now_local - timedelta(days=retention_days)
    deleted = 0

    # Brain sessions: each session is a directory
    brain_dir = session_base_dir / "brain"
    if brain_dir.is_dir():
        for entry in brain_dir.iterdir():
            if not entry.is_dir():
                continue
            ts = _parse_session_timestamp(entry.name)
            if ts is None:
                continue
            if _is_expired_session_timestamp(ts, cutoff_local=cutoff, app_tz=app_tz):
                try:
                    shutil.rmtree(entry)
                    deleted += 1
                except OSError:
                    logger.warning("Failed to remove brain session: %s", entry)

    # GUI sessions: <id>.json plus the <id>.steps.jsonl and <id>.jpg
    # files written alongside it; the id is the part before the first dot.
    gui_dir = session_base_dir / "gui"
    if gui_dir.is_dir():
        for entry in gui_dir.iterdir():
            if not entry.is_file():
                continue
            ts = _parse_session_timestamp(entry.name.split(".", 1)[0])
            if ts is None:
                continue
            if _is_expired_session_timestamp(ts, cutoff_local=cutoff, app_tz=app_tz):
                try:
                    entry.unlink()
                    deleted += 1
                except OSError:
                    logger.warning("Failed to remove GUI session file: %s", entry)

    return deleted
