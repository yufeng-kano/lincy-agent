"""Add memory curator maintenance configuration to workspace config."""

from pathlib import Path
import shutil

import yaml

from .base import Migration


class M0170MemoryCuration(Migration):
    """Enable memory distillation defaults for existing workspaces."""

    version = "0.76.5"
    summary = "新增記憶蒸餾整理器與每日維護設定，保留全文封存並自動產生摘要"

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        source_prompt = templates_dir / "agents/memory_curator/prompts/system.md"
        target_prompt = kernel_dir / "agents/memory_curator/prompts/system.md"
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
            changed = False
            maintenance = config.get("maintenance")
            if not isinstance(maintenance, dict):
                maintenance = {}
                config["maintenance"] = maintenance
                changed = True
            if "curate" not in maintenance:
                maintenance["curate"] = {
                    "enabled": True,
                    "digest_retain_days": 14,
                    "digest_max_chars": 1200,
                }
                changed = True
            agents = config.get("agents")
            if not isinstance(agents, dict):
                agents = {}
                config["agents"] = agents
                changed = True
            if "memory_curator" not in agents:
                agents["memory_curator"] = {
                    "enabled": True,
                    "llm": "cfgs/llm/deepseek/deepseek-v4-flash/no-thinking.yaml",
                    "llm_fallbacks": [
                        "cfgs/llm/deepseek/deepseek-v4-pro/no-thinking.yaml",
                        "cfgs/llm/codex/gpt-5.5/low-thinking.yaml",
                    ],
                    "llm_request_timeout": 600,
                    "llm_transient_retries": 2,
                    "llm_rate_limit_retries": 5,
                    "warn_on_failure": True,
                }
                changed = True
            if changed:
                with config_path.open("w", encoding="utf-8") as handle:
                    yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
