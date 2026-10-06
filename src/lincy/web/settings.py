"""Settings for the web dashboard, derived from the already-loaded agent config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lincy.core.schema import AppConfig

_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm"
    "/main/model_prices_and_context_window.json"
)

# src/lincy/web/settings.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class WebSettings:
    sessions_dir: Path
    static_dir: Path
    ui_events_path: Path
    soft_limit_tokens: int
    pricing_url: str
    pricing_cache_path: Path
    pricing_cache_ttl_hours: int

    @classmethod
    def from_config(cls, config: AppConfig) -> WebSettings:
        agent_os_dir = config.get_agent_os_dir()
        return cls(
            sessions_dir=agent_os_dir / "session" / "brain",
            # The host's validate stage fails fast when dist/index.html is missing.
            static_dir=_REPO_ROOT / "src" / "web_ui" / "dist",
            ui_events_path=agent_os_dir / "state" / "ui_events" / "events.jsonl",
            soft_limit_tokens=config.context.soft_max_prompt_tokens,
            pricing_url=_PRICING_URL,
            pricing_cache_path=agent_os_dir / "state" / "model_pricing_cache.json",
            pricing_cache_ttl_hours=24,
        )
