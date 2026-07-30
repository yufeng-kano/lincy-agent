"""Deploy async worker dispatch prompts and skill guidance."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "builtin-skills/memory-maintenance/SKILL.md",
]


class M0165WorkerAsyncDispatch(Migration):
    """Copy async worker protocol guidance into live kernel files."""

    version = "0.76.0"
    summary = (
        "worker 改為非同步派工：呼叫立即回傳 [WORKER DISPATCHED]，"
        "結果以 [worker, from system] 訊息送達，等待期間照常對話；"
        "併發上限 task_max_concurrency，滿載回傳 [WORKER BUSY]"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
