"""Deploy the prompts for the native GUI backend (real mouse and keyboard input)."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/gui_manager/prompts/system.md",
    "agents/worker/prompts/system.md",
    "agents/brain/prompts/system.md",
    "builtin-skills/skill-installer/SKILL.md",
]


class M0179NativeGuiBackend(Migration):
    """GUI agent drives the desktop with real input; gui_task gains PAUSED."""

    version = "0.79.0"
    summary = (
        "GUI 子代理改用自寫的原生後端：讀焦點視窗的 AX 樹與截圖，用真實滑鼠鍵盤操作，"
        "可處理開檔面板等跨程序對話框；沒有 set_value、shell，也不能存檔或貼路徑，"
        "準備檔案、算好欄位值與事後驗證都歸 worker。"
        "gui_task 新增 app 參數與 [GUI PAUSED] 結果：步數用完會暫停，"
        "worker 預設帶同一 session_id 續跑；worker 回報中提到 PAUSED 的 GUI session 時，"
        "brain 以原任務單加「續跑 GUI session <id>」重新委派，不當成失敗。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
