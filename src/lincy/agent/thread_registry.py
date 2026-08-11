"""Thread context registry: persistent per-contact thread lookup cache.

Stores the most recent thread context for each (channel, contact) pair,
allowing adapters to continue existing conversation threads when sending
proactive messages.  Each adapter decides what payload to store.

Storage: .agent/state/thread_registry.json
"""

from pathlib import Path
from typing import Any

from ..json_store import load_json, save_json

# {"gmail": {"email@addr": {"thread_id": "...", ...}}}
ThreadRegistryData = dict[str, dict[str, dict[str, Any]]]


class ThreadRegistry:
    """Channel-agnostic thread context cache.

    Reads/writes a JSON file at ``cache_dir/thread_registry.json``.
    Tolerates missing or corrupt files (degrades to empty map).
    """

    _FILENAME = "thread_registry.json"

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / self._FILENAME
        self._data: ThreadRegistryData = {}
        self._load()

    def get(self, channel: str, contact: str) -> dict[str, Any] | None:
        """Get thread context for a contact.  Returns a copy, or None on miss."""
        entry = self._data.get(channel, {}).get(contact)
        if entry is None:
            return None
        return dict(entry)

    def update(self, channel: str, contact: str, data: dict[str, Any]) -> None:
        """Update thread context for a contact and persist to disk."""
        if channel not in self._data:
            self._data[channel] = {}
        self._data[channel][contact] = data
        self._persist()

    def _load(self) -> None:
        raw = load_json(self._path, default={})
        if isinstance(raw, dict):
            self._data = raw

    def _persist(self) -> None:
        save_json(self._path, self._data)
