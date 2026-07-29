"""Deploy brain shell delegation prompts and skill guidance."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "agents/worker/prompts/system.md",
    "builtin-skills/memory-maintenance/SKILL.md",
]


class M0163BrainShellDelegation(Migration):
    """Copy worker-delegation guidance into live kernel files."""

    version = "0.75.0"
    summary = (
        "brain 不再有 execute_shell / shell_task，指令執行一律委派 worker"
        "（任務單需自包含、skill 以 context_files 帶入）；"
        "請順勢把 personal-skills 裡教你直接跑 shell 的段落改成委派 worker 的寫法"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
