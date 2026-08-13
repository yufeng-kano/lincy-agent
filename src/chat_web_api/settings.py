"""Environment-backed settings for the monitoring web API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lincy.core.config import load_config


_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm"
    "/main/model_prices_and_context_window.json"
)


@dataclass(frozen=True)
class WebApiSettings:
    host: str = "127.0.0.1"
    port: int = 9002
    sessions_dir: Path = Path()
    static_dir: Path | None = None
    web_chat_events_path: Path = Path()
    ui_events_path: Path = Path()
    control_base_url: str = "http://127.0.0.1:9001"
    claude_proxy_base_url: str = "http://127.0.0.1:4142"
    codex_proxy_base_url: str = "http://127.0.0.1:4143"
    soft_limit_tokens: int = 128_000
    pricing_url: str = _PRICING_URL
    pricing_cache_path: Path = Path()
    pricing_cache_ttl_hours: int = 24

    @classmethod
    def from_env(cls) -> WebApiSettings:
        """Build settings from the validated agent config (+ its local override)."""
        cfg = load_config()

        agent_os_dir = cfg.get_agent_os_dir()
        soft_limit = cfg.context.soft_max_prompt_tokens
        control_host = cfg.app.control.host
        control_port = cfg.app.control.port

        sessions_dir = agent_os_dir / "session" / "brain"
        pricing_cache_path = agent_os_dir / "state" / "model_pricing_cache.json"
        web_chat_events_path = agent_os_dir / "state" / "web_chat" / "events.jsonl"
        ui_events_path = agent_os_dir / "state" / "ui_events" / "events.jsonl"

        # Static dir: look for sibling chat_web_ui/dist
        ui_dist = Path(__file__).resolve().parent.parent / "chat_web_ui" / "dist"
        static_dir = ui_dist if ui_dist.is_dir() else None

        return cls(
            sessions_dir=sessions_dir,
            static_dir=static_dir,
            web_chat_events_path=web_chat_events_path,
            ui_events_path=ui_events_path,
            control_base_url=f"http://{control_host}:{control_port}",
            soft_limit_tokens=soft_limit,
            pricing_cache_path=pricing_cache_path,
        )
