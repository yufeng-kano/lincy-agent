"""Remove the retired memory_curator agent from workspace config and kernel."""

from pathlib import Path
import shutil

import yaml

from .base import Migration


class M0171RemoveMemoryCuratorAgent(Migration):
    """Drop agents.memory_curator; file curation now runs through agents.worker."""

    version = "0.76.6"
    summary = (
        "移除 memory_curator agent，記憶檔案治理改由 maintenance 直接驅動 "
        "worker 執行；新增超標檔案定期全掃，over-budget warning 不再提示 "
        "brain 自行處理"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        del templates_dir

        prompt_dir = kernel_dir / "agents" / "memory_curator"
        if prompt_dir.exists():
            shutil.rmtree(prompt_dir)

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
            if not isinstance(agents, dict) or "memory_curator" not in agents:
                continue
            del agents["memory_curator"]
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
