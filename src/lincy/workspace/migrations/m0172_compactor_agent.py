"""Add the compactor agent (tier-2 conversation compaction) to workspaces."""

from pathlib import Path
import shutil

import yaml

from .base import Migration


class M0172CompactorAgent(Migration):
    """Deploy agents.compactor and its prompt for summarizing compaction."""

    version = "0.76.7"
    summary = (
        "新增 compactor 子代理，在 codex remote compaction 不可用或失敗時，"
        "以摘要式壓縮取代直接丟棄訊息，保留教訓、約定、情感脈絡與待追事項"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        source_prompt = templates_dir / "agents/compactor/prompts/system.md"
        target_prompt = kernel_dir / "agents/compactor/prompts/system.md"
        if source_prompt.exists():
            target_prompt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_prompt, target_prompt)

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
            agents = config.get("agents")
            if not isinstance(agents, dict):
                agents = {}
                config["agents"] = agents
            if "compactor" in agents:
                continue
            agents["compactor"] = {
                "enabled": True,
                "llm": "cfgs/llm/deepseek/deepseek-v4-flash/no-thinking.yaml",
                "llm_fallbacks": [
                    "cfgs/llm/deepseek/deepseek-v4-pro/no-thinking.yaml",
                    "cfgs/llm/codex/gpt-5.5/low-thinking.yaml",
                ],
                "llm_request_timeout": 600,
                "llm_transient_retries": 2,
                "warn_on_failure": True,
            }
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
