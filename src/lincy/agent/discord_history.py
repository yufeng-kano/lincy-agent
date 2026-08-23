"""Discord runtime history/registry store.

Stores Discord channel policies, cursors, and append-only message history under:
    .agent/state/discord/
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..timezone_utils import now as tz_now
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MAX_PREVIEW_CHARS = 200
_BAD_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _utc_now() -> datetime:
    return tz_now()


def _parse_iso(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        parsed = datetime.fromisoformat(dt)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_name(name: str) -> str:
    cleaned = _BAD_FILENAME_RE.sub("_", name).strip("._")
    return cleaned or "file"


class DiscordHistoryStore:
    """Persistent runtime store for Discord channel state and history."""

    def __init__(self, cache_dir: Path) -> None:
        self.base_dir = cache_dir / "discord"
        self.history_dir = self.base_dir / "history"
        self.media_dir = self.base_dir / "media"
        self.image_summaries_dir = self.base_dir / "image_summaries"
        self._registry_path = self.base_dir / "channel_registry.json"
        self._cursors_path = self.base_dir / "cursors.json"
        self._lock = threading.Lock()
        self._registry: dict[str, dict[str, Any]] = {}
        self._cursors: dict[str, dict[str, Any]] = {}
        self._ensure_dirs()
        self._load()

    @property
    def allowed_paths(self) -> list[str]:
        """Extra file paths allowed for tools (images/history media)."""
        return [str(self.base_dir), str(self.media_dir)]

    def _ensure_dirs(self) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.image_summaries_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> None:
        self._registry = self._load_json_dict(self._registry_path)
        self._cursors = self._load_json_dict(self._cursors_path)

    def _load_json_dict(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load Discord cache %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw

    def _persist_registry(self) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _persist_cursors(self) -> None:
        self._cursors_path.parent.mkdir(parents=True, exist_ok=True)
        self._cursors_path.write_text(
            json.dumps(self._cursors, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def get_channel_entry(self, channel_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._registry.get(channel_id)
            return dict(entry) if entry is not None else None

    def list_registered_channels(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._registry.values()]

    def upsert_channel(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        guild_name: str | None,
        channel_name: str,
        alias: str,
        filter_mode: str,
        source: str,
        review_interval_seconds: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utc_now().isoformat()
        with self._lock:
            existing = self._registry.get(channel_id, {})
            created_at = existing.get("created_at", now)
            entry = {
                "channel_id": channel_id,
                "guild_id": guild_id,
                "guild_name": guild_name,
                "channel_name": channel_name,
                "alias": alias,
                "filter": filter_mode,
                "source": source,
                "review_interval_seconds": review_interval_seconds,
                "created_at": created_at,
                "updated_at": now,
                "last_seen_at": existing.get("last_seen_at"),
            }
            if extra:
                entry.update(extra)
            self._registry[channel_id] = entry
            self._persist_registry()
            self._ensure_cursor(channel_id)
            return dict(entry)

    def update_channel_last_seen(
        self,
        channel_id: str,
        *,
        message_id: str | None = None,
        seen_at: str | None = None,
    ) -> None:
        now = seen_at or _utc_now().isoformat()
        with self._lock:
            if channel_id in self._registry:
                self._registry[channel_id]["last_seen_at"] = now
                self._registry[channel_id]["updated_at"] = now
                self._persist_registry()
            cursor = self._ensure_cursor(channel_id)
            cursor["last_seen_at"] = now
            if message_id is not None:
                cursor["last_seen_message_id"] = message_id
            self._persist_cursors()

    def _ensure_cursor(self, channel_id: str) -> dict[str, Any]:
        entry = self._cursors.get(channel_id)
        if not isinstance(entry, dict):
            entry = {}
            self._cursors[channel_id] = entry
        entry.setdefault("next_event_seq", 1)
        entry.setdefault("last_reviewed_seq", 0)
        entry.setdefault("last_immediate_seq", 0)
        entry.setdefault("last_seen_message_id", None)
        entry.setdefault("last_seen_at", None)
        entry.setdefault("last_review_at", None)
        return entry

    def get_cursor(self, channel_id: str) -> dict[str, Any]:
        with self._lock:
            cursor = self._ensure_cursor(channel_id)
            return dict(cursor)

    def mark_reviewed(
        self,
        channel_id: str,
        *,
        seq: int,
        immediate: bool = False,
    ) -> None:
        with self._lock:
            cursor = self._ensure_cursor(channel_id)
            cursor["last_reviewed_seq"] = max(int(cursor["last_reviewed_seq"]), seq)
            if immediate:
                cursor["last_immediate_seq"] = max(int(cursor["last_immediate_seq"]), seq)
            cursor["last_review_at"] = _utc_now().isoformat()
            self._persist_cursors()

    def _history_path(self, channel_id: str) -> Path:
        return self.history_dir / f"{channel_id}.jsonl"

    def append_event(self, channel_id: str, payload: dict[str, Any]) -> int:
        """Append an event to channel history and return assigned seq."""
        with self._lock:
            cursor = self._ensure_cursor(channel_id)
            seq = int(cursor.get("next_event_seq", 1))
            cursor["next_event_seq"] = seq + 1
            payload = dict(payload)
            payload["seq"] = seq
            line = json.dumps(payload, ensure_ascii=False)
            path = self._history_path(channel_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # Update cursor last seen fields if present
            msg_id = payload.get("message_id")
            evt_time = payload.get("event_time")
            if msg_id is not None:
                cursor["last_seen_message_id"] = msg_id
            if evt_time is not None:
                cursor["last_seen_at"] = evt_time
            self._persist_cursors()
            if channel_id in self._registry:
                self._registry[channel_id]["last_seen_at"] = payload.get("event_time")
                self._registry[channel_id]["updated_at"] = payload.get("event_time")
                self._persist_registry()
            return seq

    def append_message_create(self, *, channel_id: str, event: dict[str, Any]) -> int:
        payload = dict(event)
        payload["event_type"] = "message_create"
        return self.append_event(channel_id, payload)

    def append_message_edit(self, *, channel_id: str, event: dict[str, Any]) -> int:
        payload = dict(event)
        payload["event_type"] = "message_edit"
        return self.append_event(channel_id, payload)

    def read_events(self, channel_id: str) -> list[dict[str, Any]]:
        path = self._history_path(channel_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping corrupt Discord history line %s:%d",
                    path.name, lineno,
                )
                continue
            if isinstance(evt, dict):
                events.append(evt)
        return events

    def get_events_after_seq(self, channel_id: str, after_seq: int) -> list[dict[str, Any]]:
        return [e for e in self.read_events(channel_id) if int(e.get("seq", 0)) > after_seq]

    def fold_latest_messages(
        self,
        events: list[dict[str, Any]],
        *,
        since_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        by_message_id: dict[str, dict[str, Any]] = {}
        cutoff: datetime | None = None
        if since_minutes is not None:
            cutoff = _utc_now() - timedelta(minutes=since_minutes)

        for evt in events:
            message_id = str(evt.get("message_id") or "").strip()
            if not message_id:
                continue
            msg_time = _parse_iso(str(evt.get("message_time") or "")) or _parse_iso(
                str(evt.get("event_time") or "")
            )
            if cutoff is not None and msg_time is not None and msg_time < cutoff:
                continue

            event_type = evt.get("event_type")
            row = by_message_id.get(message_id)
            if row is None:
                row = {
                    "message_id": message_id,
                    "timestamp": evt.get("message_time") or evt.get("event_time"),
                    "author": evt.get("author_display_name") or evt.get("author_name") or evt.get("author_id") or "",
                    "author_id": evt.get("author_id"),
                    "content": evt.get("raw_content") or "",
                    "edited": False,
                    "edited_at": None,
                    "reply": {
                        "message_id": evt.get("reply_to_message_id"),
                        "author_id": evt.get("reply_to_author_id"),
                        "author_name": evt.get("reply_to_author_name"),
                        "preview": evt.get("reply_to_preview_text"),
                    },
                    "embeds": evt.get("embeds") or [],
                    "stickers": evt.get("stickers") or [],
                    "attachments": evt.get("attachments") or [],
                    "normalized_text": evt.get("normalized_text") or "",
                    "_seq": int(evt.get("seq", 0)),
                }
                by_message_id[message_id] = row
                continue

            if int(evt.get("seq", 0)) < int(row.get("_seq", 0)):
                continue
            row["content"] = evt.get("raw_content") or row["content"]
            row["embeds"] = evt.get("embeds") or row["embeds"]
            row["stickers"] = evt.get("stickers") or row["stickers"]
            row["attachments"] = evt.get("attachments") or row["attachments"]
            row["normalized_text"] = evt.get("normalized_text") or row["normalized_text"]
            row["reply"] = {
                "message_id": evt.get("reply_to_message_id"),
                "author_id": evt.get("reply_to_author_id"),
                "author_name": evt.get("reply_to_author_name"),
                "preview": evt.get("reply_to_preview_text"),
            }
            row["author"] = evt.get("author_display_name") or evt.get("author_name") or row["author"]
            row["author_id"] = evt.get("author_id") or row["author_id"]
            row["timestamp"] = evt.get("message_time") or evt.get("event_time") or row["timestamp"]
            row["_seq"] = int(evt.get("seq", 0))
            if event_type == "message_edit":
                row["edited"] = True
                row["edited_at"] = evt.get("edited_at") or evt.get("event_time")
            elif row["edited_at"] is None and evt.get("edited_at"):
                row["edited_at"] = evt.get("edited_at")

        rows = list(by_message_id.values())
        rows.sort(
            key=lambda r: (
                _parse_iso(str(r.get("timestamp") or "")) or datetime.min.replace(tzinfo=timezone.utc),
                int(r.get("_seq", 0)),
            )
        )
        for row in rows:
            row.pop("_seq", None)
        return rows

    def get_channel_history(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        since_minutes: int | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        events = self.read_events(channel_id)
        folded = self.fold_latest_messages(events, since_minutes=since_minutes)
        if limit >= 0:
            folded = folded[-limit:]
        return {
            "channel": "discord",
            "channel_id": channel_id,
            "target": target or channel_id,
            "count": len(folded),
            "messages": folded,
        }

    def make_media_path(self, channel_id: str, message_id: str, filename: str) -> Path:
        base = self.media_dir / channel_id / message_id
        base.mkdir(parents=True, exist_ok=True)
        safe = _safe_name(filename)
        path = base / safe
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        i = 1
        while True:
            cand = base / f"{stem}_{i}{suffix}"
            if not cand.exists():
                return cand
            i += 1

    def compute_sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def get_image_summary(self, sha256_hex: str) -> dict[str, Any] | None:
        path = self.image_summaries_dir / f"{sha256_hex}.json"
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to load image summary cache %s", path.name)
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def put_image_summary(self, sha256_hex: str, data: dict[str, Any]) -> None:
        path = self.image_summaries_dir / f"{sha256_hex}.json"
        payload = dict(data)
        payload.setdefault("sha256", sha256_hex)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def preview_text(text: str | None, *, max_chars: int = _MAX_PREVIEW_CHARS) -> str | None:
        if not text:
            return None
        clean = " ".join(text.split())
        if len(clean) <= max_chars:
            return clean
        return clean[: max_chars - 3] + "..."
