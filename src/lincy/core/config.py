import logging
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
    DeepSeekConfig,
    GeminiConfig,
    HeyrouteConfig,
    KanoProxyConfig,
    LLMConfig,
    LiteLLMConfig,
    OllamaNativeConfig,
    OpenAIConfig,
    OpenRouterConfig,
)
from ..timezone_utils import validate_timezone_spec

_dotenv_values = dotenv_values()

CFGS_DIR = Path(__file__).parent.parent.parent.parent / "cfgs"

logger = logging.getLogger(__name__)

T = TypeVar(
    "T",
    OllamaNativeConfig,
    DeepSeekConfig,
    OpenAIConfig,
    AnthropicConfig,
    HeyrouteConfig,
    KanoProxyConfig,
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


def _override_path_for(config_path: Path) -> Path:
    """Return the sibling ``<name>.override.yaml`` for a config file."""
    return config_path.with_name(f"{config_path.stem}.override.yaml")


def _merge_override(base: dict, override: dict, *, prefix: str = "") -> list[str]:
    """Deep-merge ``override`` into ``base`` in place; return overridden paths.

    Only dicts merge recursively. Lists and scalars replace wholesale, because
    element-level merging of things like ``llm_fallbacks`` has no predictable
    semantics -- write the full list (or ``[]``) to change it.
    """
    applied: list[str] = []
    for key, value in override.items():
        path = f"{prefix}{key}"
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            applied.extend(_merge_override(current, value, prefix=f"{path}."))
            continue
        base[key] = value
        applied.append(path)
    return applied


# Config paths removed in kernel 0.76.8 (staged planning, skill_checker,
# conscience). Kernel migrations cannot repair these: load_config() runs at
# startup before the migrator, and an untracked cfgs/agent.override.yaml is
# outside the workspace the migrator scans. Stripping them here keeps an
# existing install bootable. Unknown keys outside this list still fail strict
# validation, so typos are not silently ignored -- see
# docs/dev/local-config-override.md.
_RETIRED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("agents", "brain", "staged_planning"),
    ("agents", "skill_checker"),
    ("agents", "conscience"),
    # Removed in kernel 0.77.0 (chat_proxy providers dropped).
    ("features", "copilot"),
    ("features", "codex_remote_compaction"),
)


def _drop_retired_config_paths(raw: dict, *, source: Path) -> None:
    """Delete config keys retired by a past release, warning for each one."""
    for path in _RETIRED_CONFIG_PATHS:
        node: object = raw
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict) or path[-1] not in node:
            continue
        del node[path[-1]]
        logger.warning(
            "Ignoring retired config key %s in %s; remove it from the file.",
            ".".join(path),
            source,
        )


# LLM profile dirs removed in kernel 0.77.0 (chat_proxy providers dropped).
# Same rationale as _RETIRED_CONFIG_PATHS: the migrator cannot reach an
# untracked cfgs/agent.override.yaml, and load_config() would otherwise exit
# on the missing profile file before any migration runs. Kept in sync with
# migrations/m0174_remove_chat_proxy_providers.py (that one rewrites workspace
# copies on disk; this one keeps the merged runtime config loadable).
_RETIRED_LLM_PROFILE_DIRS = (
    "cfgs/llm/claude_code/",
    "cfgs/llm/codex/",
    "cfgs/llm/copilot/",
    "cfgs/llm/grok/",
    "cfgs/llm/deepseek/",
    "cfgs/llm/gemini/",
    "cfgs/llm/heyroute/",
    "cfgs/llm/litellm/",
)
_RETIRED_LLM_PATH_MAP = {
    "cfgs/llm/heyroute/claude-opus-5/thinking.yaml": "cfgs/llm/anthropic/claude-opus-5/thinking.yaml",
    "cfgs/llm/codex/gpt-5.5/thinking.yaml": "cfgs/llm/kano-proxy/worker.yaml",
    "cfgs/llm/codex/gpt-5.5/low-thinking.yaml": "cfgs/llm/anthropic/claude-haiku-4.5/no-thinking.yaml",
    "cfgs/llm/deepseek/deepseek-v4-flash/no-thinking.yaml": "cfgs/llm/kano-proxy/utility.yaml",
    "cfgs/llm/deepseek/deepseek-v4-pro/thinking.yaml": "cfgs/llm/anthropic/claude-opus-5/thinking.yaml",
    "cfgs/llm/deepseek/deepseek-v4-pro/no-thinking.yaml": "cfgs/llm/anthropic/claude-haiku-4.5/no-thinking.yaml",
}
_RETIRED_LLM_FALLBACK_PATH = "cfgs/llm/kano-proxy/worker.yaml"


def _map_retired_llm_path(value: object) -> object:
    if not isinstance(value, str):
        return value
    # A file that exists is a user-created custom profile (deepseek, gemini,
    # heyroute and litellm providers are still supported, only their shipped
    # profiles were deleted) -- never reroute it.
    if _resolve_cfg_relative_path(value).exists():
        return value
    normalized = value if value.startswith("cfgs/") else f"cfgs/{value}"
    if normalized in _RETIRED_LLM_PATH_MAP:
        return _RETIRED_LLM_PATH_MAP[normalized]
    if any(normalized.startswith(prefix) for prefix in _RETIRED_LLM_PROFILE_DIRS):
        return _RETIRED_LLM_FALLBACK_PATH
    return value


def _rewrite_retired_llm_paths(raw: dict, *, source: Path) -> None:
    """Rewrite retired LLM profile paths in place, warning for each one."""
    agents = raw.get("agents")
    if not isinstance(agents, dict):
        return
    for agent_name, agent_config in agents.items():
        if not isinstance(agent_config, dict):
            continue

        def _replace(field: str, value: object) -> object:
            mapped = _map_retired_llm_path(value)
            if mapped != value:
                logger.warning(
                    "Rewriting retired LLM profile %s -> %s for agents.%s.%s "
                    "in %s; update the file.",
                    value,
                    mapped,
                    agent_name,
                    field,
                    source,
                )
            return mapped

        if "llm" in agent_config:
            agent_config["llm"] = _replace("llm", agent_config["llm"])
        fallbacks = agent_config.get("llm_fallbacks")
        if isinstance(fallbacks, list):
            mapped_fallbacks = [
                _replace("llm_fallbacks", item) for item in fallbacks
            ]
            if mapped_fallbacks != fallbacks:
                # Only a rewritten list gets deduped: mapping several retired
                # profiles can collapse onto the same kept profile (or the
                # primary), which the failover chain must not repeat.
                deduped: list[object] = []
                for item in mapped_fallbacks:
                    if item == agent_config.get("llm") or item in deduped:
                        continue
                    deduped.append(item)
                agent_config["llm_fallbacks"] = deduped


def load_raw_agent_config(
    config_path: str = "agent.yaml",
    *,
    apply_override: bool = True,
) -> dict:
    """Read the agent config as a raw dict, merging its local override file.

    All agent.yaml readers go through here so the agent process, supervisor and
    web API never disagree about values such as ``app.agent_os_dir``.
    """
    full_path = _resolve_cfg_relative_path(config_path)
    raw = _load_yaml(full_path) or {}
    _drop_retired_config_paths(raw, source=full_path)
    _rewrite_retired_llm_paths(raw, source=full_path)

    if not apply_override:
        return raw

    override_path = _override_path_for(full_path)
    if not override_path.exists():
        return raw

    override = _load_yaml(override_path)
    if override is None:
        return raw
    if not isinstance(override, dict):
        raise SystemExit(f"Config error: {override_path} must contain a YAML mapping")

    _drop_retired_config_paths(override, source=override_path)
    _rewrite_retired_llm_paths(override, source=override_path)
    applied = _merge_override(raw, override)
    if applied:
        logger.info("Applied %s: %s", override_path.name, ", ".join(sorted(applied)))
    return raw


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


def load_config(
    config_path: str = "agent.yaml",
    *,
    apply_override: bool = True,
) -> AppConfig:
    """Load and validate main config."""
    # Merge before resolving LLM references, while `llm` fields are still
    # plain path strings on both sides.
    raw = load_raw_agent_config(config_path, apply_override=apply_override)

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
    raw = load_raw_agent_config(config_path)
    app_raw = raw.get("app")
    if not isinstance(app_raw, dict):
        raise ValueError("Config error: app section is required in agent config")
    timezone = app_raw.get("timezone", "UTC")
    if not isinstance(timezone, str):
        raise ValueError("Config error: app.timezone must be a string")
    return validate_timezone_spec(timezone)
