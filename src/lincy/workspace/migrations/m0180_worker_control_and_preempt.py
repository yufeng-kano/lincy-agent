"""Deploy prompts for worker_cancel / worker_append and same-conversation preempt."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "agents/worker/prompts/system.md",
]


class M0180WorkerControlAndPreempt(Migration):
    """Brain can steer running workers; new messages fold into the running turn."""

    version = "0.80.0"
    summary = (
        "新增 worker_append 與 worker_cancel：brain 可以把補充資訊塞給執行中的 worker，"
        "或叫它停下並以 [WORKER CANCELLED] 回報進度，不用重派。"
        "同一個對話（Discord DM 或頻道、Gmail thread、cli）在處理中又來新訊息時，"
        "brain 會取消還沒執行的 tool call，把新訊息接進同一輪直接繼續，不再等下一輪。"
        "Discord DM 的等待時間從 18 秒縮為 8 秒。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
