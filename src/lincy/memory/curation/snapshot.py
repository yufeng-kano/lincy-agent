"""Archive-snapshot guarantee for file curation (design invariants 1 & 2).

Any rewrite of an over-budget memory file must first successfully write
(and verify) a full-text snapshot under memory/archive/curation/. Paths
under memory/archive/ itself are never valid curation targets -- the
archive is a write-once destination, not something curation reorganizes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

_ARCHIVE_ROOT_REL_PATH = "memory/archive"
_CURATION_ARCHIVE_REL_PATH = "memory/archive/curation"


def resolve_curation_target(agent_os_dir: Path, rel_path: str) -> Path:
    """Resolve and validate a queue path: inside workspace, a file, not archive."""
    candidate = (agent_os_dir / rel_path).resolve(strict=False)
    workspace = agent_os_dir.resolve()
    archive_root = (agent_os_dir / _ARCHIVE_ROOT_REL_PATH).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("queue path escapes workspace") from exc
    try:
        candidate.relative_to(archive_root)
    except ValueError:
        pass
    else:
        raise ValueError("queue path points into memory/archive")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def snapshot_path(rel_path: str, today: date) -> Path:
    """Return the workspace-relative archive path for one curation snapshot."""
    return Path(_CURATION_ARCHIVE_REL_PATH) / Path(rel_path) / f"{today.isoformat()}.md"


def write_verified_snapshot(path: Path, content: str) -> None:
    """Write the pre-rewrite snapshot and verify it landed byte-for-byte.

    Refuses to silently overwrite a differing snapshot already on disk --
    a same-day re-run with different content would otherwise corrupt the
    zero-loss guarantee this snapshot exists to provide.
    """
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to replace existing archive snapshot: {path}")
    else:
        _atomic_write_text(path, content)
    if path.read_text(encoding="utf-8") != content:
        raise OSError(f"archive snapshot verification failed: {path}")


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
