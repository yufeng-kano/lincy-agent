"""build_agent reports operator-fixable assembly problems as BuildError."""

import dataclasses
from pathlib import Path

import pytest

from lincy.agent import build as build_module
from lincy.agent.build import BuildError, BuildInputs, build_agent
from lincy.core.schema import AppConfig


class _DummyWorkspace:
    def __init__(self, agent_os_dir: Path):
        self.kernel_dir = agent_os_dir / "kernel"
        self.memory_dir = agent_os_dir / "memory"

    def get_system_prompt(self, _agent: str) -> str:
        return "prompt"

    def get_agent_prompt(self, *args, **kwargs) -> str:
        raise FileNotFoundError("optional prompt")


class _MissingPromptWorkspace(_DummyWorkspace):
    def get_system_prompt(self, agent: str) -> str:
        raise FileNotFoundError(f"{agent}/system.md")


def _inputs(tmp_path: Path, agents: dict) -> BuildInputs:
    config = AppConfig.model_validate({
        "app": {"agent_os_dir": str(tmp_path)},
        "agents": agents,
    })
    return BuildInputs(
        config=config,
        agent_os_dir=tmp_path,
        user_id="yufeng",
        display_name="Yufeng",
        resume_id=None,
        upgrade_message="",
    )


_BRAIN = {"enabled": True, "llm": {"provider": "openrouter", "model": "dummy"}}


def test_missing_memory_editor_raises_build_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(build_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(build_module, "create_agent_client", lambda *a, **kw: object())

    with pytest.raises(BuildError, match="agents.memory_editor"):
        build_agent(_inputs(tmp_path, {"brain": _BRAIN}))


def test_missing_system_prompt_raises_build_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(build_module, "WorkspaceManager", _MissingPromptWorkspace)

    with pytest.raises(BuildError, match="Failed to load system prompt"):
        build_agent(_inputs(tmp_path, {"brain": _BRAIN}))


def test_unknown_resume_id_raises_build_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(build_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(build_module, "create_agent_client", lambda *a, **kw: object())
    agents = {
        "brain": _BRAIN,
        "memory_editor": {
            "enabled": True,
            "llm": {"provider": "openrouter", "model": "dummy"},
            "post_parse_retries": 0,
        },
    }
    inputs = dataclasses.replace(_inputs(tmp_path, agents), resume_id="missing")

    with pytest.raises(BuildError, match="Session not found: missing"):
        build_agent(inputs)
