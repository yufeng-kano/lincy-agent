"""Add the audio sub-agent and the read_audio_by_subagent tool."""

from pathlib import Path
import shutil

import yaml

from .base import Migration

_FILES = [
    "agents/audio/prompts/system.md",
    "agents/brain/prompts/system.md",
    "builtin-skills/discord-messaging/SKILL.md",
]


class M0181AudioAgent(Migration):
    """Deploy agents.audio, its prompt, and the brain guidance for voice messages."""

    version = "0.81.0"
    summary = (
        "新增 audio 子代理與 read_audio_by_subagent 工具：語音訊息交給多模態模型"
        "回逐字稿與語氣、情緒、態度等線索。Discord 的 audio 附件會自動下載並附上"
        "[pending] 提示，brain 聽完再回覆。需在 kano-proxy 的 lincy group 設定"
        "audio-agent 模型並指向可吃音訊的 upstream。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        workspace_dir = kernel_dir.parent
        (workspace_dir / "cache" / "audio").mkdir(parents=True, exist_ok=True)
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
            if not isinstance(agents, dict):
                agents = {}
                config["agents"] = agents
            if "audio" in agents:
                continue
            agents["audio"] = {
                "enabled": True,
                "llm": "cfgs/llm/kano-proxy/audio.yaml",
                "llm_request_timeout": 180,
                "llm_transient_retries": 1,
                "warn_on_failure": True,
            }
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
