from pathlib import Path

from lincy.core.schema import AppConfig
from lincy.web.settings import WebSettings


def test_from_config_derives_paths_from_agent_os_dir(tmp_path: Path):
    config = AppConfig.model_validate(
        {
            "app": {"agent_os_dir": str(tmp_path / "agent")},
            "context": {"soft_max_prompt_tokens": 200_000},
            "agents": {},
        }
    )

    settings = WebSettings.from_config(config)

    agent_os_dir = (tmp_path / "agent").resolve()
    assert settings.sessions_dir == agent_os_dir / "session" / "brain"
    assert settings.ui_events_path == agent_os_dir / "state" / "ui_events" / "events.jsonl"
    assert settings.pricing_cache_path == agent_os_dir / "state" / "model_pricing_cache.json"
    assert settings.soft_limit_tokens == 200_000


def test_static_dir_points_at_web_ui_dist_in_repo():
    config = AppConfig.model_validate({"agents": {}})

    settings = WebSettings.from_config(config)

    repo_root = Path(__file__).resolve().parents[2]
    assert settings.static_dir == repo_root / "src" / "web_ui" / "dist"
