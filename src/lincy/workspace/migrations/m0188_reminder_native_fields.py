"""Reminders tool writes early reminder, urgent, recurrence, tags, URL, location, subtasks, and images."""

from pathlib import Path
import shutil

from .base import Migration

_FILES = ["agents/brain/prompts/system.md"]


class M0188ReminderNativeFields(Migration):
    """Deploy the brain prompt that describes the new reminders_tool fields."""

    version = "0.87.0"
    summary = (
        "reminders_tool 新增欄位：early_reminder（提前提醒，如 1d、2h）、urgent（到期時在他 iPhone 響鬧鐘）、"
        "recurrence、tags、url、location、subtasks、images；due 傳 YYYY-MM-DD 會建成整天提醒；"
        "update 可用 clear 移除 early_reminder、recurrence、location、url、tags。"
        "有截止日的提醒，due 設在截止當天，提早通知改用 early_reminder，不必再另外建行事曆事件，"
        "也不必只靠 schedule_action 自己傳訊息提醒。"
        "urgent 是唯一能主動讓他手機出聲的方法：要叫醒他時建一筆 urgent=true 的提醒，確認後用 complete 收掉。"
        "請用 memory_edit 更新 memory/agent/long-term-ops-calendar.md、long-term-ops-mail.md、"
        "long-term-tooling-apple.md 與 personal-skills 裡相關的規則："
        "移除「提醒事項工具沒有提前提醒欄位」「提前提醒只能自己傳訊息」這類說法，改成上述做法。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
