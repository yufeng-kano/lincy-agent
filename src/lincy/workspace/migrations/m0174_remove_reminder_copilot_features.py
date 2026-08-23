"""Remove format_reminders, decision_reminder, and copilot feature flags."""

from pathlib import Path

import yaml

from .base import Migration

_REMOVED_FEATURES = ("copilot", "format_reminders", "decision_reminder")


class M0174RemoveReminderCopilotFeatures(Migration):
    """Strip removed feature blocks so strict config validation keeps passing."""

    version = "0.76.9"
    summary = (
        "移除 features.format_reminders（訊息格式提醒）、features.decision_reminder"
        "（決策提醒 overlay）與 features.copilot（initiator policy 路由）；"
        "Copilot 請求 initiator 改由 dispatch mode 靜態決定"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        del templates_dir

        workspace_dir = kernel_dir.parent
        for config_path in (
            workspace_dir / "agent.yaml",
            workspace_dir / "cfgs" / "agent.yaml",
            workspace_dir / "cfgs" / "agent.override.yaml",
        ):
            if not config_path.exists():
                continue
            with config_path.open(encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            if not isinstance(config, dict):
                continue
            features = config.get("features")
            if not isinstance(features, dict):
                continue

            changed = False
            for name in _REMOVED_FEATURES:
                if name in features:
                    del features[name]
                    changed = True
            if not changed:
                continue

            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
