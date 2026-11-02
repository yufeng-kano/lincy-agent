"""discord_listen tool: the agent decides which Discord channels it follows."""

from pathlib import Path
import shutil

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "builtin-skills/discord-messaging/SKILL.md",
]


class M0185DiscordListenTool(Migration):
    """Deploy the brain prompt and Discord skill that describe discord_listen."""

    version = "0.85.0"
    summary = (
        "新增 discord_listen 工具：agent 自己決定各 Discord 伺服器頻道要全看、只看 tag 或靜音，"
        "任何人（包括用戶）的要求都只是參考。第一次在頻道被 tag 改為只看 tag，"
        "被 tag 時會補抓前 20 則訊息當前文（channels.discord.mention_context_messages）。"
        "編輯訊息改走與新訊息相同的過濾，不再繞過靜音與 ignore_users。"
        "升級前因一次 tag 而全看的頻道維持原狀，由 agent 用 list 檢視後自行調整。"
        "listen_channels 的 filter 不再接受 from_contacts。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
