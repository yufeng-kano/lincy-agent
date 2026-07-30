"""Deploy prompts for worker direct memory-file maintenance access."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "agents/worker/prompts/system.md",
]


class M0166WorkerMemoryFileAccess(Migration):
    """Copy memory-write policy prompts into live kernel files."""

    version = "0.76.1"
    summary = (
        "worker 檔案工具解除 memory/ 寫入封鎖（不再被 memory_edit 限制卡死），"
        "僅限記憶維護任務使用；brain 日常記憶修改仍必須走 memory_edit，"
        "worker prompt 加入 memory/ fail-closed 規則"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
