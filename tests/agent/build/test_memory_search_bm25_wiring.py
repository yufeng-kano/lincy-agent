"""Tests for BM25 memory_search wiring in build_agent."""

from pathlib import Path

import pytest

from lincy.core.schema import AppConfig, BM25SearchConfig


def _make_app_config(agent_os_dir: Path) -> AppConfig:
    return AppConfig.model_validate({
        "app": {
            "agent_os_dir": str(agent_os_dir),
            "warn_on_failure": False,
        },
        "tools": {
            "allowed_paths": [],
            "shell": {"blacklist": [], "timeout": 30},
            "memory_search": {
                "bm25": {
                    "top_k": 3,
                    "snippet_lines": 2,
                    "max_snippets_per_file": 1,
                    "max_response_chars": 4321,
                    "date_normalization": False,
                    "exclude": ["memory/agent/temp-memory.md"],
                },
            },
        },
        "agents": {
            "brain": {
                "enabled": True,
                "llm": {"provider": "openrouter", "model": "dummy"},
            },
            "memory_editor": {
                "enabled": True,
                "llm": {"provider": "openrouter", "model": "dummy"},
                "post_parse_retries": 0,
            },
        },
    })


def test_build_wires_bm25_memory_search(monkeypatch, tmp_path: Path):
    from lincy.agent import build as build_module

    captured: dict[str, object] = {}
    sentinel = RuntimeError("stop after bm25 setup")

    class _DummyBM25MemorySearch:
        def __init__(self, memory_dir: Path, config: BM25SearchConfig):
            captured["memory_dir"] = memory_dir
            captured["config"] = config

    class _DummyWorkspace:
        def __init__(self, agent_os_dir: Path):
            self.agent_os_dir = agent_os_dir
            self.kernel_dir = agent_os_dir / "kernel"
            self.memory_dir = agent_os_dir / "memory"

        def get_system_prompt(self, _agent: str) -> str:
            return "prompt"

        def get_agent_prompt(self, *args, **kwargs) -> str:
            return "parse-retry"

    monkeypatch.setattr(build_module, "WorkspaceManager", _DummyWorkspace)
    monkeypatch.setattr(
        build_module,
        "create_agent_client",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(build_module, "BM25MemorySearch", _DummyBM25MemorySearch)
    # setup_tools runs right after BM25 construction; stop there.
    monkeypatch.setattr(
        build_module,
        "setup_tools",
        lambda *a, **kw: (_ for _ in ()).throw(sentinel),
    )

    inputs = build_module.BuildInputs(
        config=_make_app_config(tmp_path),
        agent_os_dir=tmp_path,
        user_id="yufeng",
        display_name="Yufeng",
        resume_id=None,
        ax_binary=None,
        upgrade_message="",
    )
    with pytest.raises(RuntimeError, match="stop after bm25 setup"):
        build_module.build_agent(inputs)

    assert captured["memory_dir"] == tmp_path / "memory"
    assert captured["config"].model_dump() == {
        "top_k": 3,
        "snippet_lines": 2,
        "max_snippets_per_file": 1,
        "max_response_chars": 4321,
        "date_normalization": False,
        "exclude": ["memory/agent/temp-memory.md"],
    }
