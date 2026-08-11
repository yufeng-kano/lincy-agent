"""Migrate memory_edit warnings from line budgets to character budgets."""

from pathlib import Path

import yaml

from .base import Migration


class M0169MemoryCurationWarnings(Migration):
    """Replace the retired memory_edit warning setting in workspace config."""

    version = "0.76.4"
    summary = "記憶檔案 warning 改用字元預算，超標檔案會排入整理佇列"

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        del templates_dir
        workspace_dir = kernel_dir.parent
        for config_path in (
            workspace_dir / "agent.yaml",
            workspace_dir / "cfgs" / "agent.yaml",
        ):
            if not config_path.exists():
                continue
            with config_path.open(encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            tools = config.get("tools")
            if not isinstance(tools, dict):
                continue
            memory_edit = tools.get("memory_edit")
            if not isinstance(memory_edit, dict):
                continue
            warnings = memory_edit.get("warnings")
            if not isinstance(warnings, dict) or "max_lines" not in warnings:
                continue
            del warnings["max_lines"]
            warnings.setdefault("max_chars", 10000)
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
