"""Pinned context registry — agent-managed files loaded at boot time."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_PINS = 8
_REGISTRY_REL = "state/pinned_context.json"


def _registry_path(agent_os_dir: Path) -> Path:
    return agent_os_dir / _REGISTRY_REL


def _load_registry(agent_os_dir: Path) -> dict:
    path = _registry_path(agent_os_dir)
    if not path.exists():
        return {"version": 1, "pins": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "pins": []}
        return data
    except Exception:
        logger.warning("Failed to load pinned context registry", exc_info=True)
        return {"version": 1, "pins": []}


def _save_registry(agent_os_dir: Path, data: dict) -> None:
    path = _registry_path(agent_os_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_pinned_paths(agent_os_dir: Path) -> list[str]:
    """Return registered pin paths (workspace-relative), skipping malformed entries."""
    pins = _load_registry(agent_os_dir).get("pins", [])
    return [p["path"] for p in pins if isinstance(p, dict) and p.get("path")]


def pin_context(
    agent_os_dir: Path,
    *,
    rel_path: str,
    reason: str,
    pinned_at: str,
) -> str:
    """Pin a file for boot-time loading. Returns status message."""
    full_path = agent_os_dir / rel_path
    if not full_path.exists():
        return f"Error: file not found: {rel_path}"
    if not rel_path.startswith("memory/"):
        return "Error: only memory/ files can be pinned"

    data = _load_registry(agent_os_dir)
    pins = data.get("pins", [])

    # Check for duplicate
    for pin in pins:
        if pin.get("path") == rel_path:
            return f"OK: already pinned: {rel_path}"

    if len(pins) >= _MAX_PINS:
        return f"Error: max {_MAX_PINS} pinned files reached. Unpin something first."

    pins.append({
        "path": rel_path,
        "reason": reason,
        "pinned_at": pinned_at,
    })
    data["pins"] = pins
    _save_registry(agent_os_dir, data)
    return f"OK: pinned {rel_path} (takes effect on next session reload)"


def unpin_context(agent_os_dir: Path, *, rel_path: str) -> str:
    """Unpin a file. Returns status message."""
    data = _load_registry(agent_os_dir)
    pins = data.get("pins", [])
    original_count = len(pins)
    pins = [p for p in pins if p.get("path") != rel_path]

    if len(pins) == original_count:
        return f"OK: {rel_path} was not pinned"

    data["pins"] = pins
    _save_registry(agent_os_dir, data)
    return f"OK: unpinned {rel_path}"


def list_pinned_context(agent_os_dir: Path) -> str:
    """List all pinned context files. Returns formatted string."""
    data = _load_registry(agent_os_dir)
    pins = data.get("pins", [])
    if not pins:
        return "No pinned context files."

    lines = [f"Pinned context files ({len(pins)}/{_MAX_PINS}):"]
    for pin in pins:
        path = pin.get("path", "?")
        reason = pin.get("reason", "")
        lines.append(f"  - {path}: {reason}")
    return "\n".join(lines)
