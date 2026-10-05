"""Deploy the prompts that use the shared worker notes.

The notes file itself is created lazily on the first worker_note append.
"""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/worker/prompts/system.md",
    "builtin-skills/skill-installer/SKILL.md",
]


class M0178WorkerNotes(Migration):
    """Worker keeps one free-form notes file across tasks."""

    version = "0.78.1"
    summary = (
        "worker 現在有一份自由格式的共用筆記 worker-notes/notes.md，"
        "每次任務開頭整份注入、任務結束可用 worker_note 補記，超過閾值由 compactor 壓縮；"
        "brain 不需讀它也不需維護它。skill-installer 不再建議安裝 agent-browser。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
