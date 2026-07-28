"""Deploy Discord structured inbound message blocks."""

import shutil
from pathlib import Path

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "builtin-skills/discord-messaging/SKILL.md",
]


class M0162DiscordInboundStructure(Migration):
    """Copy Discord inbound structure guidance into live kernel files."""

    version = "0.74.20"
    summary = (
        "Discord inbound 改為結構化區塊："
        "[Message] 為當前正文，[Reply To] 為被回覆訊息（含 message_id/author/preview）"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
