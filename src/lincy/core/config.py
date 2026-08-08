import os
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

import yaml
from dotenv import dotenv_values
from pydantic import TypeAdapter

from .schema import (
    AnthropicConfig,
    AppConfig,
    CodexConfig,
    CopilotConfig,
    DeepSeekConfig,
    GeminiConfig,
    GrokConfig,
    HeyrouteConfig,
    LLMConfig,
    LiteLLMConfig,
    OllamaNativeConfig,
    OpenAIConfig,
    OpenRouterConfig,
)
from ..timezone_utils import validate_timezone_spec

_dotenv_values = dotenv_values()

CFGS_DIR = Path(__file__).parent.parent.parent.parent / "cfgs"

T = TypeVar(
    "T",
    OllamaNativeConfig,
    CopilotConfig,
    CodexConfig,
    GrokConfig,
    DeepSeekConfig,
    OpenAIConfig,
    AnthropicConfig,
    HeyrouteConfig,
    GeminiConfig,
    OpenRouterConfig,
    LiteLLMConfig,
)


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_api_key(config: T) -> T:
    """Resolve api_key from environment variable if api_key_env is set."""
    if not hasattr(config, "api_key_env") or config.api_key_env is None:
        return config

    api_key = _dotenv_values.get(config.api_key_env) or os.getenv(config.api_key_env)
    return config.model_copy(update={"api_key": api_key, "api_key_env": None})


def _derive_agent_site_url(base_url: str, agent_name: str) -> str:
    """Append agent path for per-agent OpenRouter attribution."""
    base = base_url.strip()
    if not base:
        return agent_name

    parts = urlsplit(base)
    if parts.scheme and parts.netloc:
        path = parts.path.rstrip("/")
        new_path = f"{path}/{agent_name}" if path else f"/{agent_name}"
        return urlunsplit(
            (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
        )

    trimmed = base.rstrip("/")
    if not trimmed:
        return agent_name
    return f"{trimmed}/{agent_name}"


def _resolve_cfg_relative_path(config_path: str) -> Path:
    """Resolve config path under CFGS_DIR.

    Accepts both paths relative to cfgs/ (e.g. ``llm/x.yaml``) and paths
    copied from the repo root with a leading ``cfgs/`` segment.
    """
    relative = Path(config_path)
    if relative.parts[:1] == ("cfgs",):
        relative = Path(*relative.parts[1:])
    return CFGS_DIR / relative


def resolve_llm_config(llm_path: str) -> LLMConfig:
    """Load and validate LLM config from path relative to cfgs/."""
    full_path = _resolve_cfg_relative_path(llm_path)
    raw = _load_yaml(full_path)

    adapter = TypeAdapter(LLMConfig)
    config = adapter.validate_python(raw)
    config = config.validate_reasoning(source_path=full_path)
    return _resolve_api_key(config)


def _apply_agent_openrouter_defaults(
    config: LLMConfig,
    *,
    raw_root: dict,
    agent_name: str,
) -> LLMConfig:
    if not isinstance(config, OpenRouterConfig):
        return config

    app_site_name = raw_root.get("app", {}).get(
        "openrouter_site_name",
    )

    site_name = config.site_name
    if site_name is None:
        site_name = app_site_name or agent_name

    site_url = config.site_url
    if site_url is not None:
        site_url = _derive_agent_site_url(site_url, agent_name)

    return config.model_copy(
        update={"site_name": site_name, "site_url": site_url}
    )


def _resolve_agent_llm_reference(
    raw_value: object,
    *,
    raw_root: dict,
    agent_name: str,
    field_path: str,
) -> object:
    if not isinstance(raw_value, str):
        return raw_value

    try:
        config = resolve_llm_config(raw_value)
    except FileNotFoundError:
        raise SystemExit(
            f"Config error: {field_path} references '{raw_value}' which does not exist"
        )

    config = _apply_agent_openrouter_defaults(
        config,
        raw_root=raw_root,
        agent_name=agent_name,
    )
    return config.model_dump()


def load_config(config_path: str = "agent.yaml") -> AppConfig:
    """Load and validate main config."""
    full_path = _resolve_cfg_relative_path(config_path)
    raw = _load_yaml(full_path)

    # Resolve LLM config paths to actual configs
    if "agents" in raw:
        for agent_name, agent_config in raw["agents"].items():
            if not isinstance(agent_config, dict):
                continue
            if "llm" in agent_config:
                agent_config["llm"] = _resolve_agent_llm_reference(
                    agent_config["llm"],
                    raw_root=raw,
                    agent_name=agent_name,
                    field_path=f"agents.{agent_name}.llm",
                )
            raw_fallbacks = agent_config.get("llm_fallbacks")
            if isinstance(raw_fallbacks, list):
                agent_config["llm_fallbacks"] = [
                    _resolve_agent_llm_reference(
                        item,
                        raw_root=raw,
                        agent_name=agent_name,
                        field_path=f"agents.{agent_name}.llm_fallbacks[{index}]",
                    )
                    for index, item in enumerate(raw_fallbacks)
                ]

    config = AppConfig.model_validate(raw)
    _validate_vision_coverage(config)
    return config


def _validate_vision_coverage(config: AppConfig) -> None:
    """Fail fast when the primary LLM cannot see images but the agent is
    configured to read them itself.

    Fallbacks are not checked. With ``use_own_vision_ability=true``, each
    failover candidate is handled on its own: vision-capable models keep own
    vision; non-vision models are treated like ``use_own_vision_ability=false``
    (sub-agent path). Failover already misses content cache, so mixed chains
    are allowed.
    """
    for agent_name, agent_config in config.agents.items():
        if not agent_config.use_own_vision_ability:
            continue
        model = agent_config.llm
        if not model.get_vision():
            raise SystemExit(
                f"Config error: agents.{agent_name}.llm "
                f"(provider={model.provider}, model={model.model}) does not "
                f"support vision, but agents.{agent_name}.use_own_vision_ability "
                "is true"
            )


def load_app_timezone(config_path: str = "agent.yaml") -> str:
    """Load only ``app.timezone`` from the main config."""
    full_path = _resolve_cfg_relative_path(config_path)
    raw = _load_yaml(full_path) or {}
    app_raw = raw.get("app")
    if not isinstance(app_raw, dict):
        raise ValueError("Config error: app section is required in agent config")
    timezone = app_raw.get("timezone", "UTC")
    if not isinstance(timezone, str):
        raise ValueError("Config error: app.timezone must be a string")
    return validate_timezone_spec(timezone)
