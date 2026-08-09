"""Refresh brain prompt for delta-only temp-memory writes."""

from pathlib import Path
import shutil

from .base import Migration


class M0167TempMemoryDeltaWrite(Migration):
    """Deploy temp-memory delta / pointer / open-loop write rules."""

    version = "0.76.2"
    summary = (
        "Brain prompt: temp-memory 改為 delta-only、"
        "durable pointer、open loops only，禁止整日重述"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        src = templates_dir / "agents" / "brain" / "prompts" / "system.md"
        dst = kernel_dir / "agents" / "brain" / "prompts" / "system.md"
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
