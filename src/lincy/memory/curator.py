"""LLM-backed maintenance curation for memory files."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..llm.base import LLMClient
from ..llm.schema import Message
from ..timezone_utils import now as tz_now

logger = logging.getLogger(__name__)

_QUEUE_REL_PATH = "state/memory-curation-queue.json"
_ARCHIVE_ROOT_REL_PATH = "memory/archive"
_CURATION_ARCHIVE_REL_PATH = "memory/archive/curation"


class MemoryCurator:
    """Generate compact durable memory representations during maintenance."""

    def __init__(self, client: LLMClient, system_prompt: str) -> None:
        self.client = client
        self.system_prompt = system_prompt

    def digest_day(self, day: date, content: str, max_chars: int) -> str:
        """Return one digest for a dated temp-memory partition."""
        payload = {
            "task": "daily_digest",
            "date": day.isoformat(),
            "max_chars": max_chars,
            "source_content": content,
        }
        return self._chat(payload)

    def curate_queue(self, agent_os_dir: Path) -> None:
        """Snapshot and rewrite each queued over-budget memory file."""
        entries = _load_queue(agent_os_dir)
        for entry in entries:
            rel_path = entry.get("path")
            if not isinstance(rel_path, str):
                logger.warning("Ignoring malformed memory curation queue entry")
                continue
            try:
                self._curate_file(agent_os_dir, rel_path)
            except Exception as exc:
                logger.warning("Memory curation failed for %s: %s", rel_path, exc)
                continue
            _remove_queue_entry(agent_os_dir, rel_path)

    def _curate_file(self, agent_os_dir: Path, rel_path: str) -> None:
        target = _workspace_file(agent_os_dir, rel_path)
        source = target.read_text(encoding="utf-8")
        archive_rel_path = _snapshot_path(rel_path, tz_now().date())
        archive_path = agent_os_dir / archive_rel_path
        _write_snapshot(archive_path, source)

        payload = {
            "task": "file_curation",
            "path": rel_path,
            "source_content": source,
            "archive_snapshot": archive_rel_path.as_posix(),
        }
        rewritten = self._chat(payload)
        final_content = (
            f"{rewritten.rstrip()}\n\nFull archive: {archive_rel_path.as_posix()}\n"
        )
        _atomic_write_text(target, final_content)

    def _chat(self, payload: dict[str, object]) -> str:
        response = self.client.chat(
            [
                Message(role="system", content=self.system_prompt),
                Message(
                    role="user",
                    content="MEMORY_CURATION_INPUT_JSON\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2),
                ),
            ]
        )
        result = response.strip()
        if not result:
            raise ValueError("curator returned empty output")
        return result


def _workspace_file(agent_os_dir: Path, rel_path: str) -> Path:
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


def _snapshot_path(rel_path: str, today: date) -> Path:
    return Path(_CURATION_ARCHIVE_REL_PATH) / Path(rel_path) / f"{today.isoformat()}.md"


def _write_snapshot(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"refusing to replace existing archive snapshot: {path}")
    else:
        _atomic_write_text(path, content)
    if path.read_text(encoding="utf-8") != content:
        raise OSError(f"archive snapshot verification failed: {path}")


def _load_queue(agent_os_dir: Path) -> list[dict[str, object]]:
    path = agent_os_dir / _QUEUE_REL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load memory curation queue %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        logger.warning("Memory curation queue is not a list: %s", path)
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _remove_queue_entry(agent_os_dir: Path, rel_path: str) -> None:
    path = agent_os_dir / _QUEUE_REL_PATH
    entries = _load_queue(agent_os_dir)
    retained = [entry for entry in entries if entry.get("path") != rel_path]
    _atomic_write_text(
        path,
        json.dumps(retained, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
