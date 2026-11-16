"""Prompt cache for the recall loop; recall and worker cache up to the latest tool result."""

from pathlib import Path

import yaml

from .base import Migration

_RECALL_CACHE = {"enabled": True, "ttl": "ephemeral"}


class M0189RecallWorkerToolResultCache(Migration):
    """Enable agents.recall.cache in workspace-level agent configs."""

    version = "0.87.1"
    summary = (
        "recall 子代理啟用 prompt cache（agents.recall.cache，5 分鐘 TTL）；"
        "recall 與 worker 的 tool loop 把 cache breakpoint 推到最新的 tool result，"
        "每一輪重送的搜尋與工具結果改從 cache 讀取，不再整段重新計費。"
        "行為與輸出不變，只影響費用與回應時間。"
    )

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
            agents = config.get("agents") if isinstance(config, dict) else None
            recall = agents.get("recall") if isinstance(agents, dict) else None
            if not isinstance(recall, dict) or "cache" in recall:
                continue
            recall["cache"] = dict(_RECALL_CACHE)
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
