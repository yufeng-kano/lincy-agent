"""Remove chat_proxy-backed providers; route agents through Kano Proxy."""

from pathlib import Path

import yaml

from ...core.config import CFGS_DIR
from .base import Migration

# Providers removed from LLMConfig: even an existing custom profile under
# these dirs (or an inline config) can no longer validate, so always rewrite.
_REMOVED_PROVIDER_NAMES = frozenset({"claude_code", "codex", "copilot", "grok"})
_REMOVED_PROVIDER_DIRS = (
    "cfgs/llm/claude_code/",
    "cfgs/llm/codex/",
    "cfgs/llm/copilot/",
    "cfgs/llm/grok/",
)

# Dirs whose shipped profiles were deleted but whose provider is still
# supported: an existing file there is a user-created custom profile.
_RETIRED_PROFILE_DIRS = (
    "cfgs/llm/deepseek/",
    "cfgs/llm/gemini/",
    "cfgs/llm/heyroute/",
    "cfgs/llm/litellm/",
)

# Known old paths mapped to kept profiles; anything else under a removed
# provider dir falls back to the Kano Proxy worker profile.
_LLM_PATH_MAP = {
    "cfgs/llm/heyroute/claude-opus-5/thinking.yaml": "cfgs/llm/anthropic/claude-opus-5/thinking.yaml",
    "cfgs/llm/codex/gpt-5.5/thinking.yaml": "cfgs/llm/kano-proxy/worker.yaml",
    "cfgs/llm/codex/gpt-5.5/low-thinking.yaml": "cfgs/llm/anthropic/claude-haiku-4.5/no-thinking.yaml",
    "cfgs/llm/deepseek/deepseek-v4-flash/no-thinking.yaml": "cfgs/llm/kano-proxy/utility.yaml",
    "cfgs/llm/deepseek/deepseek-v4-pro/thinking.yaml": "cfgs/llm/anthropic/claude-opus-5/thinking.yaml",
    "cfgs/llm/deepseek/deepseek-v4-pro/no-thinking.yaml": "cfgs/llm/anthropic/claude-haiku-4.5/no-thinking.yaml",
}
_FALLBACK_LLM_PATH = "cfgs/llm/kano-proxy/worker.yaml"


def _profile_exists(value: str) -> bool:
    relative = Path(value)
    if relative.parts[:1] == ("cfgs",):
        relative = Path(*relative.parts[1:])
    return (CFGS_DIR / relative).exists()


def _map_llm_path(value: object) -> object:
    if isinstance(value, dict):
        # Inline provider config (previously valid): a removed provider no
        # longer validates, so reroute the whole entry to a kept profile.
        if value.get("provider") in _REMOVED_PROVIDER_NAMES:
            return _FALLBACK_LLM_PATH
        return value
    if not isinstance(value, str):
        return value
    if any(value.startswith(prefix) for prefix in _REMOVED_PROVIDER_DIRS):
        # Removed provider: even an existing custom file cannot validate.
        return _LLM_PATH_MAP.get(value, _FALLBACK_LLM_PATH)
    # A file that still exists is a user-created custom profile (deepseek,
    # gemini, heyroute and litellm providers remain supported) -- keep it.
    if _profile_exists(value):
        return value
    if value in _LLM_PATH_MAP:
        return _LLM_PATH_MAP[value]
    if any(value.startswith(prefix) for prefix in _RETIRED_PROFILE_DIRS):
        return _FALLBACK_LLM_PATH
    return value


class M0174RemoveChatProxyProviders(Migration):
    """Drop chat_proxy provider references from workspace agent configs."""

    version = "0.77.0"
    summary = (
        "移除 chat_proxy 及 claude_code/codex/copilot/grok 四個本機 proxy provider；"
        "agent 模型改走 kano-proxy 設定檔，舊 provider 設定路徑自動改寫為 "
        "kano-proxy 或 anthropic 對應設定"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        del templates_dir

        workspace_dir = kernel_dir.parent
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

            changed = False

            features = config.get("features")
            if isinstance(features, dict):
                for name in ("copilot", "codex_remote_compaction"):
                    if name in features:
                        del features[name]
                        changed = True

            agents = config.get("agents")
            if isinstance(agents, dict):
                for agent_config in agents.values():
                    if not isinstance(agent_config, dict):
                        continue
                    new_llm = _map_llm_path(agent_config.get("llm"))
                    if new_llm != agent_config.get("llm"):
                        agent_config["llm"] = new_llm
                        changed = True
                    fallbacks = agent_config.get("llm_fallbacks")
                    if isinstance(fallbacks, list):
                        mapped = [_map_llm_path(item) for item in fallbacks]
                        if mapped != fallbacks:
                            # Only a rewritten list gets deduped: mapping
                            # several retired profiles can collapse onto the
                            # same kept profile (or the primary).
                            deduped: list[object] = []
                            for item in mapped:
                                if item == agent_config.get("llm") or item in deduped:
                                    continue
                                deduped.append(item)
                            agent_config["llm_fallbacks"] = deduped
                            changed = True

            if not changed:
                continue
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
