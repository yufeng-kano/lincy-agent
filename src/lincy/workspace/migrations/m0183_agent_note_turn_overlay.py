"""Delta [Agent Notes] in kept per-turn blocks, and a 100-note cap."""

from pathlib import Path
import shutil

import yaml

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
]


class M0183AgentNoteTurnOverlay(Migration):
    """Deploy the brain prompt for kept turn blocks and raise tools.agent_note.max_notes."""

    version = "0.83.0"
    summary = (
        "每輪附在用戶訊息後的 [Runtime Context]、[Timing Notice]、[Agent Notes] 改為原樣留在歷史，"
        "不再於下一輪刪除；判斷「現在」一律看最新一則用戶訊息上的 current_local_time。"
        "[Agent Notes] 只列出新增或變動的 note，沒列出的沿用對話中最近一次出現的那一行，"
        "`key: (removed)` 代表已刪除，要看完整清單用 agent_note list；注入的行不再帶 source。"
        "note 數量上限從 12 條提高到 100 條。系統不會自動清除 note，"
        "已閉環或不再需要的狀態要自己用 agent_note remove 刪掉。"
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
            tools = config.get("tools")
            if not isinstance(tools, dict):
                continue
            agent_note = tools.get("agent_note")
            # Only the old shipped cap moves; a deliberately chosen value stays.
            if not isinstance(agent_note, dict) or agent_note.get("max_notes") != 12:
                continue
            agent_note["max_notes"] = 100
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
