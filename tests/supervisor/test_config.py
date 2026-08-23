"""Tests for supervisor config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from chat_supervisor.config import load_supervisor_config


def test_load_supervisor_config_resolves_auto_processes(monkeypatch, tmp_path: Path):
    cfgs_dir = tmp_path / "cfgs"
    cfgs_dir.mkdir()
    (cfgs_dir / "supervisor.yaml").write_text(
        """
processes:
  ollama-helper:
    enabled: auto
    auto_enable_when_any_agent_uses_provider: ollama_native
    command: ["uv", "run", "ollama-helper", "serve"]
  gemini-helper:
    enabled: auto
    auto_enable_when_any_agent_uses_provider: gemini
    command: ["uv", "run", "gemini-helper", "serve"]
  anthropic-helper:
    enabled: auto
    auto_enable_when_any_agent_uses_provider: anthropic
    command: ["uv", "run", "anthropic-helper", "serve"]
  chat-cli:
    enabled: true
    command: ["uv", "run", "chat-cli"]
    depends_on: ["ollama-helper", "gemini-helper", "anthropic-helper"]
"""
    )
    monkeypatch.setattr("chat_supervisor.config.CFGS_DIR", cfgs_dir)
    monkeypatch.setattr(
        "chat_supervisor.config._used_agent_llm_providers",
        lambda _config_path="agent.yaml": {"anthropic"},
    )

    config = load_supervisor_config("supervisor.yaml")

    assert config.processes["ollama-helper"].enabled is False
    assert config.processes["gemini-helper"].enabled is False
    assert config.processes["anthropic-helper"].enabled is True
    assert config.processes["chat-cli"].enabled is True


def test_load_supervisor_config_rejects_auto_without_provider(monkeypatch, tmp_path: Path):
    cfgs_dir = tmp_path / "cfgs"
    cfgs_dir.mkdir()
    (cfgs_dir / "supervisor.yaml").write_text(
        """
processes:
  anthropic-helper:
    enabled: auto
    command: ["uv", "run", "anthropic-helper", "serve"]
"""
    )
    monkeypatch.setattr("chat_supervisor.config.CFGS_DIR", cfgs_dir)

    with pytest.raises(
        ValueError,
        match="auto_enable_when_any_agent_uses_provider",
    ):
        load_supervisor_config("supervisor.yaml")


def test_used_agent_llm_providers_includes_fallbacks(monkeypatch, tmp_path: Path):
    cfgs_dir = tmp_path / "cfgs"
    cfgs_dir.mkdir()
    (cfgs_dir / "primary.yaml").write_text(
        """
provider: openai
model: gpt-4o
api_key: test-key
"""
    )
    (cfgs_dir / "fallback.yaml").write_text(
        """
provider: openrouter
model: anthropic/claude-sonnet-4.6
api_key: test-key
"""
    )
    (cfgs_dir / "agent.yaml").write_text(
        """
agents:
  brain:
    llm: primary.yaml
    llm_fallbacks:
      - fallback.yaml
"""
    )
    monkeypatch.setattr("chat_supervisor.config.CFGS_DIR", cfgs_dir)
    monkeypatch.setattr("lincy.core.config.CFGS_DIR", cfgs_dir)

    from chat_supervisor.config import _used_agent_llm_providers

    assert _used_agent_llm_providers() == {"openai", "openrouter"}
