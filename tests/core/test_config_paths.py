from pathlib import Path

import yaml

from lincy.core import config as config_module


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def test_resolve_llm_config_accepts_cfgs_prefix(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "openai" / "profile.yaml",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("cfgs/llm/openai/profile.yaml")
    assert config.model == "gpt-4o"


def test_load_config_accepts_cfgs_prefixed_llm_path(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "openai" / "profile.yaml",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
        },
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "cfgs/llm/openai/profile.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.model == "gpt-4o"


def test_load_config_resolves_llm_fallback_paths(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "primary.yaml",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "primary-key",
        },
    )
    _write_yaml(
        tmp_path / "llm" / "fallback.yaml",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "fallback-key",
            "site_url": "https://chat-agent.local",
        },
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "agents": {
                "brain": {
                    "llm": "llm/primary.yaml",
                    "llm_fallbacks": ["cfgs/llm/fallback.yaml"],
                }
            }
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")

    assert config.agents["brain"].llm.model == "gpt-4o"
    assert len(config.agents["brain"].llm_fallbacks) == 1
    assert config.agents["brain"].llm_fallbacks[0].provider == "openrouter"
    assert config.agents["brain"].llm_fallbacks[0].site_url == (
        "https://chat-agent.local/brain"
    )


def test_resolve_llm_config_reads_ollama_api_key_from_env(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "ollama" / "cloud.yaml",
        {
            "provider": "ollama",
            "model": "gpt-oss:20b-cloud",
            "base_url": "https://ollama.com",
            "api_key_env": "OLLAMA_API_KEY",
            "thinking": {"mode": "effort", "effort": "medium"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)
    monkeypatch.setattr(config_module, "_dotenv_values", {})
    monkeypatch.setenv("OLLAMA_API_KEY", "env-ollama-key")

    config = config_module.resolve_llm_config("llm/ollama/cloud.yaml")
    assert config.api_key == "env-ollama-key"


def test_repo_agent_config_enables_shell_handoff_rules():
    config = config_module.load_config("agent.yaml")

    handoff = config.tools.shell.handoff
    assert handoff.enabled is True
    assert [rule.id for rule in handoff.rules] == [
        "auth_browser_url",
        "auth_code_prompt",
        "press_enter_to_continue",
        "interactive_menu_prompt",
    ]


def test_repo_agent_config_brain_uses_claude_code_with_expected_fallbacks():
    config = config_module.load_config("agent.yaml")

    brain_llm = config.agents["brain"].llm
    assert brain_llm.provider == "claude_code"
    assert brain_llm.model == "claude-opus-5"
    assert brain_llm.thinking is not None
    assert brain_llm.thinking.type == "adaptive"
    assert brain_llm.output_config is not None
    assert brain_llm.output_config.effort == "xhigh"
    assert brain_llm.temperature == 1.0

    fallbacks = config.agents["brain"].llm_fallbacks
    assert [cfg.provider for cfg in fallbacks] == ["heyroute"]
    assert [cfg.model for cfg in fallbacks] == ["claude-opus-5"]
    assert fallbacks[0].thinking.type == "adaptive"


def test_repo_agent_config_worker_uses_claude_code_with_expected_fallbacks():
    config = config_module.load_config("agent.yaml")

    worker_llm = config.agents["worker"].llm
    assert worker_llm.provider == "claude_code"
    assert worker_llm.model == "claude-opus-5"
    assert worker_llm.thinking is not None
    assert worker_llm.thinking.type == "adaptive"

    fallbacks = config.agents["worker"].llm_fallbacks
    assert [cfg.provider for cfg in fallbacks] == ["heyroute", "deepseek"]
    assert [cfg.model for cfg in fallbacks] == ["claude-opus-5", "deepseek-v4-pro"]
    assert fallbacks[0].thinking.type == "adaptive"
    assert fallbacks[1].thinking.enabled is True


def test_repo_agent_config_memory_editor_uses_deepseek_v4_flash_no_thinking():
    config = config_module.load_config("agent.yaml")

    memory_editor_llm = config.agents["memory_editor"].llm
    assert memory_editor_llm.provider == "deepseek"
    assert memory_editor_llm.model == "deepseek-v4-flash"
    assert memory_editor_llm.thinking is not None
    assert memory_editor_llm.thinking.enabled is False

    fallbacks = config.agents["memory_editor"].llm_fallbacks
    assert [cfg.provider for cfg in fallbacks] == ["deepseek", "codex"]
    assert [cfg.model for cfg in fallbacks] == ["deepseek-v4-pro", "gpt-5.5"]
    assert fallbacks[0].thinking.enabled is False
    assert fallbacks[1].reasoning.enabled is True
    assert fallbacks[1].reasoning.effort == "low"


def test_repo_kimi_k26_cloud_profile_loads():
    config = config_module.resolve_llm_config(
        "cfgs/llm/ollama/kimi-k2.6-cloud/thinking.yaml"
    )

    assert config.provider == "ollama"
    assert config.model == "kimi-k2.6:cloud"
    assert config.vision is True
    assert config.thinking.mode == "toggle"
    assert config.thinking.enabled is True


def test_repo_deepseek_v4_flash_cloud_profile_loads():
    config = config_module.resolve_llm_config(
        "cfgs/llm/ollama/deepseek-v4-flash-cloud/thinking.yaml"
    )

    assert config.provider == "ollama"
    assert config.model == "deepseek-v4-flash:cloud"
    assert config.vision is False
    assert config.thinking.mode == "effort"
    assert config.thinking.effort == "max"


def test_repo_claude_code_opus_47_and_48_profiles_load():
    thinking = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-4.7/thinking.yaml"
    )
    no_thinking = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-4.7/no-thinking.yaml"
    )

    assert thinking.provider == "claude_code"
    assert thinking.model == "claude-opus-4-7"
    assert thinking.thinking is not None
    assert thinking.thinking.type == "adaptive"
    assert thinking.output_config is not None
    assert thinking.output_config.effort == "high"

    assert no_thinking.provider == "claude_code"
    assert no_thinking.model == "claude-opus-4-7"
    assert no_thinking.thinking is not None
    assert no_thinking.thinking.type == "disabled"
    assert no_thinking.output_config is not None
    assert no_thinking.output_config.effort == "low"

    thinking_48 = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-4.8/thinking.yaml"
    )
    no_thinking_48 = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-4.8/no-thinking.yaml"
    )

    assert thinking_48.provider == "claude_code"
    assert thinking_48.model == "claude-opus-4-8"
    assert thinking_48.thinking is not None
    assert thinking_48.thinking.type == "adaptive"
    assert thinking_48.output_config is not None
    assert thinking_48.output_config.effort == "high"

    assert no_thinking_48.provider == "claude_code"
    assert no_thinking_48.model == "claude-opus-4-8"
    assert no_thinking_48.thinking is not None
    assert no_thinking_48.thinking.type == "disabled"
    assert no_thinking_48.output_config is not None
    assert no_thinking_48.output_config.effort == "low"


def test_repo_claude_code_opus_5_profiles_load():
    thinking = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-5/thinking.yaml"
    )
    no_thinking = config_module.resolve_llm_config(
        "cfgs/llm/claude_code/claude-opus-5/no-thinking.yaml"
    )

    assert thinking.provider == "claude_code"
    assert thinking.model == "claude-opus-5"
    assert thinking.vision is True
    assert thinking.thinking is not None
    assert thinking.thinking.type == "adaptive"
    assert thinking.output_config is not None
    assert thinking.output_config.effort == "xhigh"

    assert no_thinking.provider == "claude_code"
    assert no_thinking.model == "claude-opus-5"
    assert no_thinking.vision is True
    assert no_thinking.thinking is not None
    assert no_thinking.thinking.type == "disabled"
    # Upstream rejects disabled thinking above effort high.
    assert no_thinking.output_config is not None
    assert no_thinking.output_config.effort == "low"


def test_load_app_timezone_reads_only_timezone(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "agent.yaml",
        {
            "app": {"timezone": "Asia/Taipei"},
            "agents": {"brain": {"llm": "missing-llm.yaml"}},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    assert config_module.load_app_timezone("agent.yaml") == "Asia/Taipei"
