"""Persistent note store: structured key-value state tracking.

Notes are stored in ``state/notes.json`` and injected into every turn's
context so the agent always has access to real-time user state.  Each
note can have trigger phrases; when a user message matches a trigger,
the system prompts the agent to review and update the note.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..json_store import format_source_tag, load_json, parse_datetime, save_json
from ..timezone_utils import get_tz, localise as tz_localise, now as tz_now

logger = logging.getLogger(__name__)

_FILENAME = "notes.json"


@dataclass
class Note:
    key: str
    value: str
    triggers: list[str]
    description: str | None
    source_app: str | None
    source_id: str | None
    source_label: str | None
    updated_at: datetime


def _format_age(updated_at: datetime) -> str:
    """Human-readable age string like '2h ago', '3d ago'."""
    diff = tz_now() - updated_at
    total_sec = diff.total_seconds()
    if total_sec < 60:
        return "just now"
    if total_sec < 3600:
        return f"{int(total_sec / 60)}m ago"
    if total_sec < 86400:
        hours = total_sec / 3600
        return f"{hours:.0f}h ago"
    days = total_sec / 86400
    return f"{days:.0f}d ago"


def _format_context_updated_at(updated_at: datetime) -> str:
    """Render a stable local timestamp for prompt context injection."""
    return tz_localise(updated_at).strftime("%m-%d %H:%M")


class NoteStore:
    """Persistent store for agent notes."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / _FILENAME
        self._notes: dict[str, Note] = {}
        self._load()

    # -- CRUD ---------------------------------------------------------------

    def create(
        self,
        key: str,
        value: str,
        triggers: list[str] | None = None,
        description: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
    ) -> Note | str:
        """Create a note. Returns the Note on success, or an error string."""
        if key in self._notes:
            return f"Error: note '{key}' already exists; use update to change it"
        note = Note(
            key=key,
            value=value,
            triggers=triggers or [],
            description=description,
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
            updated_at=tz_now(),
        )
        self._notes[key] = note
        self._save()
        logger.info("Created note: %s", key)
        return note

    def update(
        self,
        key: str,
        value: str | None = None,
        triggers: list[str] | None = None,
        description: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
    ) -> Note | None:
        """Update an existing note. Returns None if key not found."""
        note = self._notes.get(key)
        if note is None:
            return None
        changed = False
        if value is not None:
            changed = changed or note.value != value
            note.value = value
        if triggers is not None:
            changed = changed or note.triggers != triggers
            note.triggers = triggers
        if description is not None:
            changed = changed or note.description != description
            note.description = description
        if source_app is not None:
            changed = changed or note.source_app != source_app
            note.source_app = source_app
        if source_id is not None:
            changed = changed or note.source_id != source_id
            note.source_id = source_id
        if source_label is not None:
            changed = changed or note.source_label != source_label
            note.source_label = source_label
        if changed:
            note.updated_at = tz_now()
            self._save()
        return note

    def upsert(
        self,
        *,
        key: str,
        value: str,
        triggers: list[str] | None = None,
        description: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
    ) -> Note:
        """Create or replace a note without churning timestamps on no-op updates."""
        normalized_triggers = triggers or []
        note = self._notes.get(key)
        if note is None:
            created = self.create(
                key=key,
                value=value,
                triggers=normalized_triggers,
                description=description,
                source_app=source_app,
                source_id=source_id,
                source_label=source_label,
            )
            assert not isinstance(created, str)
            return created
        changed = (
            note.value != value
            or note.triggers != normalized_triggers
            or note.description != description
            or note.source_app != source_app
            or note.source_id != source_id
            or note.source_label != source_label
        )
        if not changed:
            return note
        note.value = value
        note.triggers = normalized_triggers
        note.description = description
        note.source_app = source_app
        note.source_id = source_id
        note.source_label = source_label
        note.updated_at = tz_now()
        self._save()
        return note

    def get(self, key: str) -> Note | None:
        return self._notes.get(key)

    def list_all(self) -> list[Note]:
        return sorted(self._notes.values(), key=lambda n: n.key)

    def remove(self, key: str) -> bool:
        if key not in self._notes:
            return False
        del self._notes[key]
        self._save()
        logger.info("Removed note: %s", key)
        return True

    # -- Trigger matching ---------------------------------------------------

    def find_matching_triggers(self, text: str) -> list[Note]:
        """Return notes whose triggers match *text* (case-insensitive substring)."""
        if not text:
            return []
        lower = text.lower()
        matched: list[Note] = []
        for note in self._notes.values():
            for trigger in note.triggers:
                if trigger.lower() in lower:
                    matched.append(note)
                    break
        return matched

    # -- Display ------------------------------------------------------------

    def format_context_block(self) -> str | None:
        """Build a compact notes block for context injection.

        Returns None if there are no notes.

        Use stable absolute timestamps here. Relative ages would require
        wall-clock reads during prompt rebuild and churn the latest-turn
        cache prefix every minute.
        """
        notes = self.list_all()
        if not notes:
            return None
        lines = ["[Agent Notes]"]
        for n in notes:
            source = _format_source_tag(n)
            lines.append(
                f'{n.key}: "{n.value}"'
                f"{f' | source {source}' if source else ''}"
                f" | updated_at {_format_context_updated_at(n.updated_at)}"
            )
        return "\n".join(lines)

    def format_list_detail(self) -> str:
        """Detailed listing for the tool's list action."""
        notes = self.list_all()
        if not notes:
            return "No notes."
        lines: list[str] = []
        for n in notes:
            triggers_str = ", ".join(f'"{t}"' for t in n.triggers) if n.triggers else "none"
            desc = f" ({n.description})" if n.description else ""
            source = _format_source_detail(n)
            source_line = f"\n  source: {source}" if source else ""
            lines.append(
                f'- {n.key}: "{n.value}"{desc}\n'
                f"  triggers: [{triggers_str}] | updated {_format_age(n.updated_at)}"
                f"{source_line}"
            )
        return "\n".join(lines)

    # -- Persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = load_json(self._path, default={})
            tz = get_tz()
            for key, item in raw.get("notes", {}).items():
                self._notes[key] = Note(
                    key=key,
                    value=item.get("value", ""),
                    triggers=item.get("triggers", []),
                    description=item.get("description"),
                    source_app=item.get("source_app"),
                    source_id=item.get("source_id"),
                    source_label=item.get("source_label"),
                    updated_at=parse_datetime(item.get("updated_at"), tz) or tz_now(),
                )
            logger.info("Loaded %d notes from %s", len(self._notes), self._path)
        except Exception:
            logger.warning("Failed to load notes from %s", self._path, exc_info=True)

    def _save(self) -> None:
        data: dict = {"notes": {}}
        for key, note in self._notes.items():
            d = asdict(note)
            del d["key"]  # key is the dict key
            d["updated_at"] = d["updated_at"].isoformat()
            data["notes"][key] = d
        save_json(self._path, data)


def _format_source_tag(note: Note) -> str | None:
    return format_source_tag(note.source_app, note.source_label)


def _format_source_detail(note: Note) -> str | None:
    tag = _format_source_tag(note)
    if tag is None:
        return None
    if note.source_id:
        return f"{tag} ({note.source_id})"
    return tag
