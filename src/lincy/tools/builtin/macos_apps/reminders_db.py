"""Read-only access to the Reminders Core Data stores for the flag EventKit lacks."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

STORES_DIR = Path.home() / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"

# ZIDENTIFIER (16-byte UUID) is the EventKit calendarItemIdentifier. Entity
# numbers differ between store files, so REMCDReminder is resolved per store.
_FLAGS_SQL = """
SELECT ZIDENTIFIER, ZFLAGGED FROM ZREMCDREMINDER
WHERE Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'REMCDReminder')
"""


def _unreadable(detail: str) -> PermissionError:
    return PermissionError(
        f"Reminders database is not readable ({detail}). Grant Full Disk Access in "
        "System Settings > Privacy & Security > Full Disk Access to the app that runs lincy (e.g. Terminal)."
    )


def read_flags() -> dict[str, bool]:
    """Map every reminder id to its flagged state, one query per store file."""
    # Without Full Disk Access the directory listing comes back empty.
    paths = sorted(STORES_DIR.glob("Data-*.sqlite"))
    if not paths:
        raise _unreadable(f"no Data-*.sqlite under {STORES_DIR}")
    flags: dict[str, bool] = {}
    for path in paths:
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                rows = connection.execute(_FLAGS_SQL).fetchall()
        except sqlite3.Error as exc:
            raise _unreadable(f"{path.name}: {exc}") from exc
        flags.update((str(uuid.UUID(bytes=ident)).upper(), bool(flagged)) for ident, flagged in rows if ident)
    return flags
