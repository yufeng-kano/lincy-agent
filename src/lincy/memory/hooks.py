"""Daily snapshot of the temp-memory state file into memory/archive/temp-memory/."""

from datetime import date, datetime
from pathlib import Path
import logging

from ..json_store import save_text
from ..timezone_utils import get_tz, now as tz_now

logger = logging.getLogger(__name__)

_STATE_FILE_REL_PATH = "memory/agent/temp-memory.md"
_ARCHIVE_SUBDIR = "memory/archive/temp-memory"


def snapshot_state_file(agent_os_dir: Path, *, day: date) -> Path | None:
    """Copy the current state file into the archive partition for ``day``.

    The state file is rewritten in place during the day, so this snapshot is
    the only durable record of what it said. A partition that already exists
    (legacy dated log entries, or an earlier snapshot) is appended to, never
    replaced. Returns the written path, or None when there was nothing new
    to write.
    """
    source = agent_os_dir / _STATE_FILE_REL_PATH
    if not source.is_file():
        return None
    content = source.read_text(encoding="utf-8").strip()
    if not content:
        return None

    archive_dir = agent_os_dir / _ARCHIVE_SUBDIR
    target = archive_dir / f"{day.isoformat()}.md"
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if content in existing:
            return None
        save_text(target, f"{existing.rstrip()}\n\n---\n\n{content}\n")
    else:
        archive_dir.mkdir(parents=True, exist_ok=True)
        save_text(target, f"# {day.isoformat()}\n\n{content}\n")

    _update_archive_index(archive_dir)
    logger.info("Snapshot %s -> %s", _STATE_FILE_REL_PATH, target.name)
    return target


def snapshot_stale_state_file(agent_os_dir: Path) -> Path | None:
    """Archive the state file under the local date it was last written on.

    Nothing happens while that date is today: the file still describes the
    current day and is archived once the day is over. Used before the first
    rewrite of a new day and at the start of daily maintenance, so a version
    written after midnight never lands in the previous day's partition.
    """
    source = agent_os_dir / _STATE_FILE_REL_PATH
    if not source.is_file():
        return None
    written = datetime.fromtimestamp(source.stat().st_mtime, tz=get_tz()).date()
    if written >= tz_now().date():
        return None
    return snapshot_state_file(agent_os_dir, day=written)


def _update_archive_index(archive_dir: Path) -> None:
    """Rebuild index.md listing all date files in the archive directory."""
    md_files = sorted(
        f for f in archive_dir.iterdir()
        if f.suffix == ".md" and f.name != "index.md"
    )
    lines = [f"# {archive_dir.name} archive\n", "\n"]
    for f in md_files:
        lines.append(f"- [{f.stem}]({f.name})\n")
    (archive_dir / "index.md").write_text("".join(lines), encoding="utf-8")
