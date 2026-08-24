"""Split the shared Kano Proxy utility profile into per-agent profiles."""

from pathlib import Path

import yaml

from .base import Migration

_RETIRED_UTILITY_PATH = "cfgs/llm/kano-proxy/utility.yaml"

# Each agent that used the shared utility profile now has its own gateway
# model, so the rewrite is keyed by agent name.
_AGENT_PROFILES = {
    "compactor": "cfgs/llm/kano-proxy/compactor.yaml",
    "gui_worker": "cfgs/llm/kano-proxy/gui-worker.yaml",
    "init": "cfgs/llm/kano-proxy/init.yaml",
    "memory_editor": "cfgs/llm/kano-proxy/memory-editor.yaml",
    "vision": "cfgs/llm/kano-proxy/vision.yaml",
    "web_fetch_summarizer": "cfgs/llm/kano-proxy/web-fetch-summarizer.yaml",
}
# An agent with no dedicated profile (or a fallback entry, which has no agent
# identity) keeps the old utility behaviour: the low-effort profile.
_DEFAULT_PROFILE = "cfgs/llm/kano-proxy/web-fetch-summarizer.yaml"


class M0176SplitKanoProxyUtilityProfile(Migration):
    """Repoint agent configs off the deleted kano-proxy utility profile."""

    version = "0.77.1"
    summary = (
        "kano-proxy 共用的 utility profile 已拆成每個 agent 專屬設定檔；"
        "workspace 設定中指向 utility.yaml 的 agent 會依 agent 名稱改寫為對應設定檔，"
        "無對應者沿用低 effort 的 web_fetch_summarizer 設定"
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
            agents = config.get("agents")
            if not isinstance(agents, dict):
                continue

            changed = False
            for name, agent_config in agents.items():
                if not isinstance(agent_config, dict):
                    continue
                if agent_config.get("llm") == _RETIRED_UTILITY_PATH:
                    agent_config["llm"] = _AGENT_PROFILES.get(name, _DEFAULT_PROFILE)
                    changed = True
                fallbacks = agent_config.get("llm_fallbacks")
                if isinstance(fallbacks, list) and _RETIRED_UTILITY_PATH in fallbacks:
                    rewritten: list[object] = []
                    for item in fallbacks:
                        mapped = _DEFAULT_PROFILE if item == _RETIRED_UTILITY_PATH else item
                        # The primary may already be what utility mapped to.
                        if mapped == agent_config.get("llm") or mapped in rewritten:
                            continue
                        rewritten.append(mapped)
                    agent_config["llm_fallbacks"] = rewritten
                    changed = True

            if not changed:
                continue
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
