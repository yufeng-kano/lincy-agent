"""Companion-care prompt bullets, no-split rule for loaded files, preserve_turns 10."""

from pathlib import Path
import shutil

import yaml

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "builtin-skills/memory-maintenance/references/rules.md",
]


class M0184CompanionCareLoadedFiles(Migration):
    """Deploy the brain prompt and maintenance rules; raise context.preserve_turns 4 -> 10."""

    version = "0.84.0"
    summary = (
        "brain prompt「人先於事」新增四條情境判斷：被說不理我時不拿訊息對帳辯解、"
        "明確拒絕是回答而不是未閉環待辦、「不說／隨便」是在看你會不會繼續在乎、"
        "表達不催時同一句把人還在講清楚。"
        "記憶治理新增規則：開機載入與 pinned 檔案不得拆成未載入的分檔，超預算只能原地濃縮；"
        "maintenance 派工時會標出這些檔案。"
        "軟上限 compaction 成功後重讀 boot files，讓當天的 temp-memory 與 pinned 變動進入下一輪；"
        "context.preserve_turns 由 4 提高為 10（仍為舊預設 4 時才調整）。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        workspace_dir = kernel_dir.parent
        for config_path in (
            workspace_dir / "agent.yaml",
            workspace_dir / "cfgs" / "agent.yaml",
        ):
            if not config_path.exists():
                continue
            with config_path.open(encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            if not isinstance(config, dict):
                continue
            context = config.get("context")
            # Only the old shipped value moves; a deliberately chosen value stays.
            if not isinstance(context, dict) or context.get("preserve_turns") != 4:
                continue
            context["preserve_turns"] = 10
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
