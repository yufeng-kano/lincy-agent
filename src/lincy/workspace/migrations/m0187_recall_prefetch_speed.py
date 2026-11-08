"""Recall prefetch speed: brace globs, prefiltered transcript grep, direct first turn."""

from pathlib import Path
import shutil

from .base import Migration

_FILES = ["agents/recall/prompts/system.md"]


class M0187RecallPrefetchSpeed(Migration):
    """Deploy the recall prompt that greps recent transcripts directly."""

    version = "0.86.1"
    summary = (
        "recall 子代理提速：grep 的 path_glob 支援 {a,b} 大括號；"
        "transcript grep 先以字面詞預篩再解析 JSON，全量掃描約快四成；"
        "recall prompt 改為第一輪直接 grep 記憶檔與當月對話紀錄，"
        "不先對全部 session 做 count_only，答案最多 5 條。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
