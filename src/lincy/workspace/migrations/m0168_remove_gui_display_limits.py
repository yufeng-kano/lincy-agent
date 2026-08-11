"""Remove retired GUI display-limit settings."""

from pathlib import Path

import yaml

from .base import Migration


class M0168RemoveGuiDisplayLimits(Migration):
    """Retire unused GUI display-limit configuration."""

    version = "0.76.3"
    summary = "移除已停用的 GUI 顯示字元上限設定"

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        workspace_dir = kernel_dir.parent
        for config_path in (
            workspace_dir / "agent.yaml",
            workspace_dir / "cfgs" / "agent.yaml",
        ):
            if not config_path.exists():
                continue
            with config_path.open(encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            agents = config.get("agents")
            if not isinstance(agents, dict):
                continue
            gui_manager = agents.get("gui_manager")
            if not isinstance(gui_manager, dict):
                continue
            changed = False
            for field in (
                "gui_intent_max_chars",
                "gui_instruction_max_chars",
                "gui_text_max_chars",
                "gui_worker_result_max_chars",
                "gui_result_max_chars",
            ):
                if field in gui_manager:
                    del gui_manager[field]
                    changed = True
            if changed:
                with config_path.open("w", encoding="utf-8") as handle:
                    yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
