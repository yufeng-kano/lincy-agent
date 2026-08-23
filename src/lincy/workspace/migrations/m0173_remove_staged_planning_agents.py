"""Remove staged planning, the skill_checker agent, and the conscience agent."""

from pathlib import Path
import shutil

import yaml

from .base import Migration


class M0173RemoveStagedPlanningAgents(Migration):
    """Drop brain.staged_planning plus the skill_checker/conscience agents."""

    version = "0.76.8"
    summary = (
        "移除 brain 三階段 staged planning、skill_checker 預載子代理與 conscience "
        "事後稽核子代理；brain 一律走單段 responder loop，skill guide 仍由 tool "
        "governance preflight 在執行前注入"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        del templates_dir

        prompt_dir = kernel_dir / "agents" / "skill_checker"
        if prompt_dir.exists():
            shutil.rmtree(prompt_dir)

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
            for name in ("skill_checker", "conscience"):
                if name in agents:
                    del agents[name]
                    changed = True
            brain = agents.get("brain")
            if isinstance(brain, dict) and "staged_planning" in brain:
                del brain["staged_planning"]
                changed = True
            if not changed:
                continue

            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
