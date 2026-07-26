"""Tests for per-provider reasoning validation via config.validate_reasoning()."""

from pathlib import Path

import pytest
import yaml

from lincy.core import config as config_module


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def test_resolve_llm_config_rejects_extra_fields(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "ollama",
            "model": "test-model",
            "unknown_key": "boom",
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_ollama_requires_thinking_config(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "ollama",
            "model": "test-model",
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="ollama.thinking"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_ollama_accepts_effort_mode_for_non_gpt_oss(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "ollama",
            "model": "test-model",
            "thinking": {"mode": "effort", "effort": "xhigh"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")

    assert config.thinking.mode == "effort"
    assert config.thinking.effort == "xhigh"


def test_ollama_accepts_effort_mode_for_deepseek_v4(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "ollama",
            "model": "deepseek-v4-flash:cloud",
            "thinking": {"mode": "effort", "effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")

    assert config.model == "deepseek-v4-flash:cloud"
    assert config.thinking.mode == "effort"
    assert config.thinking.effort == "max"


def test_ollama_accepts_max_effort_for_gpt_oss(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "ollama",
            "model": "gpt-oss:20b-cloud",
            "thinking": {"mode": "effort", "effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")

    assert config.thinking.mode == "effort"
    assert config.thinking.effort == "max"


def test_openrouter_rejects_effort_and_max_tokens_together(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "openrouter",
            "model": "provider/model",
            "api_key": "test-key",
            "reasoning": {
                "effort": "high",
                "max_tokens": 2048,
                "supported_efforts": ["low", "medium", "high"],
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="mutually exclusive"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_openrouter_provider_routing_rejects_empty_order(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "test-key",
            "provider_routing": {"order": []},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="provider_routing.order must not be empty"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_openrouter_provider_routing_null_is_allowed(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "test-key",
            "provider_routing": None,
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.provider_routing is None


def test_openrouter_provider_routing_accepts_google_vertex(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "api_key": "test-key",
            "provider_routing": {
                "order": ["google-vertex"],
                "allow_fallbacks": False,
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.provider_routing is not None
    assert config.provider_routing.order == ["google-vertex"]
    assert config.provider_routing.allow_fallbacks is False


def test_openai_validates_reasoning(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "openai" / "profile.yaml",
        {
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
            "reasoning": {"effort": "high"},
            "capabilities": {
                "reasoning": {
                    "supports_toggle": True,
                    "supported_efforts": ["low", "medium", "high"],
                    "supports_max_tokens": False,
                }
            },
        },
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "llm/openai/profile.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.reasoning.effort == "high"


def test_openai_accepts_max_effort_even_when_profile_list_is_old(
    monkeypatch, tmp_path: Path
):
    _write_yaml(
        tmp_path / "llm" / "openai" / "profile.yaml",
        {
            "provider": "openai",
            "model": "gpt-5.1",
            "api_key": "test-key",
            "reasoning": {"effort": "max"},
            "capabilities": {
                "reasoning": {
                    "supports_toggle": True,
                    "supported_efforts": ["low", "medium", "high"],
                    "supports_max_tokens": False,
                }
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/openai/profile.yaml")

    assert config.reasoning.effort == "max"


def test_anthropic_requires_budget_tokens(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "anthropic",
            "model": "claude-test",
            "api_key": "test-key",
            "reasoning": {"enabled": True},
            "capabilities": {
                "reasoning": {
                    "supports_toggle": True,
                    "supported_efforts": [],
                    "supports_max_tokens": True,
                }
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="Anthropic thinking requires"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_copilot_validates_supported_efforts(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "copilot",
            "model": "test-model",
            "reasoning": {"effort": "high", "supported_efforts": ["low"]},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="is not supported"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_copilot_passes_with_valid_effort(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "copilot",
            "model": "test-model",
            "reasoning": {
                "effort": "high",
                "supported_efforts": ["low", "medium", "high"],
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.reasoning.effort == "high"
    assert config.reasoning.enabled is True


def test_copilot_no_reasoning_passes(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {"provider": "copilot", "model": "test-model"},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.reasoning is None


def test_grok_validates_supported_efforts(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "grok",
            "model": "grok-4.5",
            "reasoning": {"effort": "xhigh", "supported_efforts": ["low", "high"]},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="is not supported"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_grok_rejects_effort_when_disabled(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "grok",
            "model": "grok-4.3",
            "reasoning": {"enabled": False, "effort": "low"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="cannot be set when enabled is false"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_grok_passes_with_valid_effort(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "grok",
            "model": "grok-4.5",
            "reasoning": {
                "effort": "high",
                "supported_efforts": ["low", "medium", "high"],
            },
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.provider == "grok"
    assert config.reasoning.effort == "high"
    assert config.reasoning.enabled is True
    assert config.base_url == "http://localhost:4144/v1"


def test_claude_code_accepts_adaptive_thinking(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-sonnet-4-6",
            "thinking": {"type": "adaptive"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.thinking is not None
    assert config.thinking.type == "adaptive"


def test_claude_code_accepts_output_config_effort(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-sonnet-4-6",
            "output_config": {"effort": "medium"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.output_config is not None
    assert config.output_config.effort == "medium"


def test_claude_code_rejects_empty_output_config(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-sonnet-4-6",
            "output_config": {},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="output_config must set at least one field"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_claude_code_accepts_xhigh_effort_on_opus_5(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-opus-5",
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.output_config.effort == "xhigh"


def test_claude_code_accepts_max_effort_on_opus_5(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-opus-5",
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.output_config.effort == "max"


def test_claude_code_rejects_xhigh_effort_on_sonnet_46(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-sonnet-4-6",
            "output_config": {"effort": "xhigh"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="effort=xhigh is not supported upstream"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_claude_code_rejects_any_effort_on_haiku_45(monkeypatch, tmp_path: Path):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-haiku-4-5",
            "output_config": {"effort": "low"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="supported: none"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_claude_code_allows_max_effort_on_unknown_model(monkeypatch, tmp_path: Path):
    """Unlisted model ids pass through so new models are not blocked at load."""
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-opus-9-future",
            "output_config": {"effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.output_config.effort == "max"


def test_claude_code_rejects_disabled_thinking_above_high_on_opus_5(
    monkeypatch, tmp_path: Path
):
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-opus-5",
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(ValueError, match="only works at effort high or below"):
        config_module.resolve_llm_config("llm/x.yaml")


def test_claude_code_allows_disabled_thinking_with_max_effort_on_opus_48(
    monkeypatch, tmp_path: Path
):
    """The disabled-thinking effort cap is an Opus 5 rule, not a family-wide one."""
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "claude_code",
            "model": "claude-opus-4-8",
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "max"},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.resolve_llm_config("llm/x.yaml")
    assert config.output_config.effort == "max"


# --- OpenRouter global merge & site_name fallback ---


def test_load_config_site_name_fallback_to_agent_name(monkeypatch, tmp_path: Path):
    """site_name defaults to agent name when null in YAML."""
    _write_yaml(
        tmp_path / "llm" / "or.yaml",
        {"provider": "openrouter", "model": "test/model"},
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "llm/or.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.site_name == "brain"


def test_load_config_yaml_site_name_preserved(monkeypatch, tmp_path: Path):
    """site_name set in YAML is preserved (no override)."""
    _write_yaml(
        tmp_path / "llm" / "or.yaml",
        {"provider": "openrouter", "model": "test/model", "site_name": "from-yaml"},
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "llm/or.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.site_name == "from-yaml"


def test_load_config_app_openrouter_site_name_global(monkeypatch, tmp_path: Path):
    """app.openrouter_site_name overrides agent_name fallback for all agents."""
    _write_yaml(
        tmp_path / "llm" / "or.yaml",
        {"provider": "openrouter", "model": "test/model"},
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "app": {"openrouter_site_name": "MyApp"},
            "agents": {"brain": {"llm": "llm/or.yaml"}},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.site_name == "MyApp"


def test_load_config_site_url_appends_agent_name(monkeypatch, tmp_path: Path):
    """site_url defaults to per-agent path when set in shared LLM config."""
    _write_yaml(
        tmp_path / "llm" / "or.yaml",
        {
            "provider": "openrouter",
            "model": "test/model",
            "site_url": "https://chat-agent.local",
        },
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "llm/or.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.site_url == "https://chat-agent.local/brain"


def test_load_config_site_url_preserves_existing_path(monkeypatch, tmp_path: Path):
    """site_url keeps existing path and appends agent segment."""
    _write_yaml(
        tmp_path / "llm" / "or.yaml",
        {
            "provider": "openrouter",
            "model": "test/model",
            "site_url": "https://chat-agent.local/base/",
        },
    )
    _write_yaml(
        tmp_path / "basic.yaml",
        {"agents": {"brain": {"llm": "llm/or.yaml"}}},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].llm.site_url == "https://chat-agent.local/base/brain"


def test_openrouter_max_tokens_rejects_zero(monkeypatch, tmp_path: Path):
    """max_tokens=0 should be rejected at config level."""
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {"provider": "openrouter", "model": "test/model", "max_tokens": 0},
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(Exception):
        config_module.resolve_llm_config("llm/x.yaml")


def test_openrouter_reasoning_max_tokens_rejects_below_1024(monkeypatch, tmp_path: Path):
    """reasoning.max_tokens below 1024 should be rejected."""
    _write_yaml(
        tmp_path / "llm" / "x.yaml",
        {
            "provider": "openrouter",
            "model": "test/model",
            "reasoning": {"max_tokens": 512},
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(Exception):
        config_module.resolve_llm_config("llm/x.yaml")


# --- use_own_vision_ability vision coverage ---


def _write_ollama_profile(path: Path, *, model: str, vision: bool) -> None:
    _write_yaml(
        path,
        {
            "provider": "ollama",
            "model": model,
            "vision": vision,
            "thinking": {"mode": "toggle", "enabled": False},
        },
    )


def test_load_config_allows_vision_gap_in_fallback_when_use_own_vision_ability(
    monkeypatch, tmp_path: Path
):
    """Non-vision fallbacks are allowed; failover already misses content cache."""
    _write_ollama_profile(tmp_path / "llm" / "vision-ok.yaml", model="a", vision=True)
    _write_ollama_profile(tmp_path / "llm" / "no-vision.yaml", model="b", vision=False)
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "agents": {
                "brain": {
                    "llm": "llm/vision-ok.yaml",
                    "llm_fallbacks": ["llm/no-vision.yaml"],
                    "use_own_vision_ability": True,
                }
            }
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].use_own_vision_ability is True
    assert config.agents["brain"].llm.get_vision() is True
    assert config.agents["brain"].llm_fallbacks[0].get_vision() is False


def test_load_config_rejects_vision_gap_in_primary_when_use_own_vision_ability(
    monkeypatch, tmp_path: Path
):
    _write_ollama_profile(tmp_path / "llm" / "no-vision.yaml", model="a", vision=False)
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "agents": {
                "brain": {
                    "llm": "llm/no-vision.yaml",
                    "use_own_vision_ability": True,
                }
            }
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    with pytest.raises(SystemExit, match="agents.brain.llm.*does not support vision"):
        config_module.load_config("basic.yaml")


def test_load_config_allows_vision_gap_when_use_own_vision_ability_false(
    monkeypatch, tmp_path: Path
):
    _write_ollama_profile(tmp_path / "llm" / "vision-ok.yaml", model="a", vision=True)
    _write_ollama_profile(tmp_path / "llm" / "no-vision.yaml", model="b", vision=False)
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "agents": {
                "brain": {
                    "llm": "llm/vision-ok.yaml",
                    "llm_fallbacks": ["llm/no-vision.yaml"],
                    "use_own_vision_ability": False,
                }
            }
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].use_own_vision_ability is False


def test_load_config_accepts_full_vision_coverage_when_use_own_vision_ability(
    monkeypatch, tmp_path: Path
):
    _write_ollama_profile(tmp_path / "llm" / "vision-ok.yaml", model="a", vision=True)
    _write_ollama_profile(tmp_path / "llm" / "vision-ok-2.yaml", model="b", vision=True)
    _write_yaml(
        tmp_path / "basic.yaml",
        {
            "agents": {
                "brain": {
                    "llm": "llm/vision-ok.yaml",
                    "llm_fallbacks": ["llm/vision-ok-2.yaml"],
                    "use_own_vision_ability": True,
                }
            }
        },
    )
    monkeypatch.setattr(config_module, "CFGS_DIR", tmp_path)

    config = config_module.load_config("basic.yaml")
    assert config.agents["brain"].use_own_vision_ability is True
    assert config.agents["brain"].llm_fallbacks[0].get_vision() is True
