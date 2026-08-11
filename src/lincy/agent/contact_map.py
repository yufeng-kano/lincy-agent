"""Universal contact map: fast sender-to-name resolution runtime state.

Two-layer sender resolution:
  Layer 1 (this module): Read from .agent/state/contact_map.json
  Layer 2 (brain LLM):   memory_search + update_contact_mapping tool
"""

from pathlib import Path

from ..json_store import load_json, save_json


# {"gmail": {"email@addr": "name"}, "line": {"display": "name"}}
ContactMapData = dict[str, dict[str, str]]


class ContactMap:
    """Channel-agnostic sender-to-name cache.

    Reads/writes a JSON file at ``cache_dir/contact_map.json``.
    Tolerates missing or corrupt files (degrades to empty map).
    """

    _FILENAME = "contact_map.json"

    def __init__(self, cache_dir: Path) -> None:
        self._path = cache_dir / self._FILENAME
        self._data: ContactMapData = {}
        self._load()

    def _load(self) -> None:
        raw = load_json(self._path, default={})
        if isinstance(raw, dict):
            self._data = raw

    def resolve(self, channel: str, sender_key: str) -> str | None:
        """Look up cached name for a sender. Returns None on miss."""
        return self._data.get(channel, {}).get(sender_key)

    def reverse_lookup(self, channel: str, name: str) -> str | None:
        """Find sender_key by name for a given channel. Returns None on miss."""
        for key, val in self._data.get(channel, {}).items():
            if val == name:
                return key
        return None

    def update(self, channel: str, sender_key: str, name: str) -> None:
        """Add or overwrite a mapping and persist to disk."""
        if channel not in self._data:
            self._data[channel] = {}
        self._data[channel][sender_key] = name
        self._persist()

    def _persist(self) -> None:
        save_json(self._path, self._data)
