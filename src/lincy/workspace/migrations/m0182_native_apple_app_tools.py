"""Apple app tools read through EventKit, PhotoKit and the apps' databases."""

from pathlib import Path
import shutil

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
]


class M0182NativeAppleAppTools(Migration):
    """Deploy the brain prompt for the rewritten calendar/reminders/notes/photos/mail tools."""

    version = "0.82.0"
    summary = (
        "行事曆、提醒事項、備忘錄、照片、郵件工具改寫：不再逐筆透過 AppleScript 讀資料，"
        "改用 EventKit、PhotoKit 與各 app 的資料庫，查詢都在一秒內，之前的 30 秒逾時已修好。"
        "reminders_tool search 恢復可用，reminder_id 是裸 UUID（舊的 x-apple-reminder:// 也接受），"
        "行事曆的「已排程的提醒事項」不再出現，提醒一律用 reminders_tool 查與改。"
        "calendar_tool 不必再一週一段查；週期事件改時間會被拒絕。"
        "請用 memory_edit 更新 memory/agent/long-term-tooling.md 與 long-term-tooling-env.md："
        "移除或標註過時的逾時繞路規則（一週一段查行事曆、提醒逾時改派 gui_task 點 App、"
        "search 失敗就停手、notes_tool 逾時處理），改成「工具已修好，正常使用」。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
