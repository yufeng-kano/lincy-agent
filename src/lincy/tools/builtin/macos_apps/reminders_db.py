"""Read-only access to the Reminders Core Data stores for the fields EventKit lacks."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
import re
import sqlite3
from typing import TypeVar
import uuid

STORES_DIR = Path.home() / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"

# Index is the ReminderKit REMDueDateDeltaUnit value; verified on macOS 26.7.1
# through REMDueDateDeltaInterval.description (min, hr, d, wk, mo).
EARLY_REMINDER_UNITS = ("m", "h", "d", "w", "mo")

# ZIDENTIFIER (16-byte UUID) is the EventKit calendarItemIdentifier. Entity
# numbers differ between store files, so REMCDReminder is resolved per store.
_FLAGS_SQL = """
SELECT ZIDENTIFIER, ZFLAGGED FROM ZREMCDREMINDER
WHERE Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'REMCDReminder')
"""
_REMINDERS_SQL = """
SELECT Z_PK, ZIDENTIFIER, {urgent}, ZPARENTREMINDER FROM ZREMCDREMINDER
WHERE Z_ENT = (SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'REMCDReminder') AND ZMARKEDFORDELETION = 0
"""
_EARLY_REMINDERS_SQL = """
SELECT ZREMINDERIDENTIFIER, ZIDENTIFIER, ZDUEDATEDELTACOUNT, ZDUEDATEDELTAUNIT FROM ZREMCDDUEDATEDELTAALERT
ORDER BY ZCREATIONDATE
"""
_URGENT_COLUMN = "ZISURGENTSTATEENABLEDFORCURRENTUSER"
# ZREMCDOBJECT holds many entities in one table, so Core Data suffixes the
# columns that share a name: on macOS 26.7.1 an attachment points at its
# reminder through ZREMINDER2, a hashtag through ZREMINDER3 and keeps its
# name in ZNAME1. {owner} and {name} are filled per store from the columns
# that exist, so a renumbering does not break the query.
_OWNER_COLUMN_RE = re.compile(r"^ZREMINDER\d*$")
_NAME_COLUMN_RE = re.compile(r"^ZNAME\d*$")
_OBJECTS_SQL = """
SELECT p.Z_NAME, {owner}, o.ZURL, {name} FROM ZREMCDOBJECT o
JOIN Z_PRIMARYKEY p ON p.Z_ENT = o.Z_ENT
WHERE p.Z_NAME IN ('REMCDHashtag', 'REMCDURLAttachment', 'REMCDImageAttachment') AND o.ZMARKEDFORDELETION = 0
ORDER BY o.Z_PK
"""

T = TypeVar("T")


@dataclass
class ReminderExtras:
    """Reminder fields that only the Reminders database exposes."""

    urgent: bool = False
    early_reminders: list[str] = field(default_factory=list)
    # Identifiers of the early reminder alerts; ReminderKit removes them by id.
    early_reminder_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    url: str | None = None
    image_count: int = 0
    parent_id: str | None = None
    subtask_ids: list[str] = field(default_factory=list)


def _unreadable(detail: str) -> PermissionError:
    return PermissionError(
        f"Reminders database is not readable ({detail}). Grant Full Disk Access in "
        "System Settings > Privacy & Security > Full Disk Access to the app that runs lincy (e.g. Terminal). "
        "If the detail names a missing table or column, access is fine and macOS changed the Reminders "
        "schema: update src/lincy/tools/builtin/macos_apps/reminders_db.py."
    )


def _read_each_store(read: Callable[[sqlite3.Connection], T]) -> list[T]:
    """Run read against every Reminders store file, opened read-only."""
    # Without Full Disk Access the directory listing comes back empty.
    paths = sorted(STORES_DIR.glob("Data-*.sqlite"))
    if not paths:
        raise _unreadable(f"no Data-*.sqlite under {STORES_DIR}")
    results = []
    for path in paths:
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                results.append(read(connection))
        except sqlite3.Error as exc:
            raise _unreadable(f"{path.name}: {exc}") from exc
    return results


def _event_kit_id(identifier: bytes) -> str:
    return str(uuid.UUID(bytes=identifier)).upper()


def read_flags() -> dict[str, bool]:
    """Map every reminder id to its flagged state, one query per store file."""
    flags: dict[str, bool] = {}
    for rows in _read_each_store(lambda connection: connection.execute(_FLAGS_SQL).fetchall()):
        flags.update((_event_kit_id(ident), bool(flagged)) for ident, flagged in rows if ident)
    return flags


def _read_store_extras(connection: sqlite3.Connection) -> dict[str, ReminderExtras]:
    extras: dict[str, ReminderExtras] = {}
    id_by_pk: dict[int, str] = {}
    # The on-device store (Data-local.sqlite) can keep a schema from before
    # urgent reminders existed; such a store cannot hold an urgent reminder.
    has_urgent = any(row[1] == _URGENT_COLUMN for row in connection.execute("PRAGMA table_info(ZREMCDREMINDER)"))
    reminders = connection.execute(_REMINDERS_SQL.format(urgent=_URGENT_COLUMN if has_urgent else "0")).fetchall()
    for pk, ident, urgent, _parent in reminders:
        if ident:
            id_by_pk[pk] = _event_kit_id(ident)
            extras[id_by_pk[pk]] = ReminderExtras(urgent=bool(urgent))
    for pk, _ident, _urgent, parent in reminders:
        if pk in id_by_pk and parent in id_by_pk:
            extras[id_by_pk[pk]].parent_id = id_by_pk[parent]
            extras[id_by_pk[parent]].subtask_ids.append(id_by_pk[pk])
    for reminder_ident, alert_ident, count, unit in connection.execute(_EARLY_REMINDERS_SQL):
        entry = extras.get(_event_kit_id(reminder_ident)) if reminder_ident else None
        if entry is not None:
            token = EARLY_REMINDER_UNITS[unit] if 0 <= unit < len(EARLY_REMINDER_UNITS) else f"unit{unit}"
            # A negative count is before the due date. A positive one fires after
            # it; Reminders.app never writes that, so it is marked, not shown as early.
            entry.early_reminders.append(f"{-count}{token}" if count < 0 else f"after-due:{count}{token}")
            entry.early_reminder_ids.append(_event_kit_id(alert_ident))
    columns = [row[1] for row in connection.execute("PRAGMA table_info(ZREMCDOBJECT)")]
    owner_columns = [f"o.{column}" for column in columns if _OWNER_COLUMN_RE.match(column)]
    name_columns = [f"o.{column}" for column in columns if _NAME_COLUMN_RE.match(column)]
    if not owner_columns or not name_columns:
        raise sqlite3.OperationalError("ZREMCDOBJECT lacks the ZREMINDER or ZNAME columns")
    objects_sql = _OBJECTS_SQL.format(
        owner=f"COALESCE({', '.join(owner_columns)})", name=f"COALESCE({', '.join(name_columns)})"
    )
    for entity, owner_pk, url, tag in connection.execute(objects_sql):
        entry = extras.get(id_by_pk.get(owner_pk, ""))
        if entry is None:
            continue
        if entity == "REMCDHashtag":
            if tag:
                entry.tags.append(tag)
        elif entity == "REMCDURLAttachment":
            entry.url = url
        else:
            entry.image_count += 1
    return extras


def read_extras() -> dict[str, ReminderExtras]:
    """Map every reminder id to the fields EventKit cannot read."""
    merged: dict[str, ReminderExtras] = {}
    for extras in _read_each_store(_read_store_extras):
        merged.update(extras)
    return merged
