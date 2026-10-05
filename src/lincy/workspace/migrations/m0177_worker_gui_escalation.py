"""Move gui_task from brain to worker: prompts plus excluded_tools lists."""

import shutil
from pathlib import Path

import yaml

from .base import Migration

_FILES = [
    "agents/brain/prompts/system.md",
    "agents/worker/prompts/system.md",
]


class M0177WorkerGuiEscalation(Migration):
    """Brain delegates browser/GUI work to worker, which escalates to gui_task."""

    version = "0.78.0"
    summary = (
        "gui_task 不再是 brain 的工具：瀏覽器、登入、視覺確認的工作一律派 worker，"
        "worker 會自行先走 HTTP/指令，被擋才升級到 GUI 子代理。"
        "agent-browser 已停用，不可再指示 worker 使用。"
        "請修正記憶中 2026-08-19 那條「瀏覽器一律 gui_task、不可交給 worker」的規則，"
        "並修正 tra-query、chart-gen、mos-survey、read-later-notes 四個 skill 裡的 agent-browser 段落。"
    )

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        for rel in _FILES:
            src = templates_dir / rel
            dst = kernel_dir / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

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
            worker = agents.get("worker")
            if isinstance(worker, dict):
                excluded = worker.get("excluded_tools")
                if isinstance(excluded, list) and "gui_task" in excluded:
                    worker["excluded_tools"] = [n for n in excluded if n != "gui_task"]
                    changed = True
            # Only touch an existing brain block: a bare one without llm
            # would fail config validation.
            brain = agents.get("brain")
            if isinstance(brain, dict):
                excluded = brain.get("excluded_tools")
                if excluded is None:
                    brain["excluded_tools"] = ["gui_task"]
                    changed = True
                elif isinstance(excluded, list) and "gui_task" not in excluded:
                    excluded.append("gui_task")
                    changed = True

            if not changed:
                continue
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
