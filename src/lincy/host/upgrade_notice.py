"""Persist the kernel upgrade summary between `lincy check` and the real start.

Upgrade runs `lincy check` as a gate; its validate stage applies kernel
migrations and would otherwise be the only process that ever saw the
summary. The real start after execv finds the kernel already current, so
the summary is stashed here and consumed by the agent that actually runs.
"""

from __future__ import annotations

from pathlib import Path

_NOTICE = Path("state") / "pending_upgrade_notice.txt"


def stash(agent_os_dir: Path, message: str) -> None:
    path = agent_os_dir / _NOTICE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message)


def load(agent_os_dir: Path) -> str:
    path = agent_os_dir / _NOTICE
    return path.read_text() if path.is_file() else ""


def clear(agent_os_dir: Path) -> None:
    (agent_os_dir / _NOTICE).unlink(missing_ok=True)
