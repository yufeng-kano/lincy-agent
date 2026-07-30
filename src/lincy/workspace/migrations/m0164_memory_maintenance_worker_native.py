"""Deploy worker-native memory maintenance skill files."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "builtin-skills/memory-maintenance/SKILL.md",
    "builtin-skills/memory-maintenance/references/rules.md",
]


class M0164MemoryMaintenanceWorkerNative(Migration):
    """Copy reworked memory-maintenance skill into the live kernel."""

    version = "0.75.1"
    summary = (
        "memory-maintenance 大規模維護改由 worker 用檔案工具直接整理，"
        "不再開 claude CLI subprocess；rules.md 補上 long-term.md "
        "解讀原則/核心價值 區塊的保護規則"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
