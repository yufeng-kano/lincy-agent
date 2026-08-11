"""Tests for config schema defaults and fields."""

import pytest
from pydantic import ValidationError

from lincy.core.schema import AgentConfig, AppConfig


def _ollama_llm() -> dict[str, object]:
    return {
        "provider": "ollama",
        "model": "test-model",
        "thinking": {"mode": "toggle", "enabled": False},
    }


def test_app_config_warn_on_failure_default_true():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    assert config.app.warn_on_failure is True
    assert config.app.timezone == "UTC+8"
    assert config.app.turn_failure_requeue_limit == 1
    assert config.app.turn_failure_requeue_delay_seconds == 60
    assert config.app.requeue_non_retryable_turn_failures is False


def test_app_config_warn_on_failure_override_false():
    config = AppConfig.model_validate(
        {
            "app": {
                "warn_on_failure": False,
                "turn_failure_requeue_limit": 2,
                "turn_failure_requeue_delay_seconds": 90,
                "requeue_non_retryable_turn_failures": True,
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.app.warn_on_failure is False
    assert config.app.turn_failure_requeue_limit == 2
    assert config.app.turn_failure_requeue_delay_seconds == 90
    assert config.app.requeue_non_retryable_turn_failures is True


def test_agent_config_enabled_default_true():
    config = AgentConfig.model_validate({"llm": _ollama_llm()})
    assert config.enabled is True


def test_agent_config_rejects_visible_text_review_mode():
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            {
                "llm": _ollama_llm(),
                "visible_text_review_mode": "all",
            }
        )


def test_discord_channel_config_defaults():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    discord_cfg = config.channels.discord
    assert discord_cfg.enabled is False
    assert discord_cfg.listen_dms is True
    assert discord_cfg.guild_review_interval_seconds == 60
    assert discord_cfg.auto_read_images is True
    assert discord_cfg.auto_download_attachment_max_mb == 25
    assert discord_cfg.dm_debounce_seconds == 12
    assert discord_cfg.dm_max_wait_seconds == 180
    assert discord_cfg.dm_typing_quiet_seconds == 15
    assert discord_cfg.presence_mode == "auto"
    assert discord_cfg.presence_refresh_seconds == 90
    assert discord_cfg.presence_idle_after_seconds == 300


def test_web_channel_config_defaults_and_override():
    config = AppConfig.model_validate(
        {
            "channels": {
                "web": {
                    "enabled": True,
                    "history_limit": 50,
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )

    assert config.channels.web.enabled is True
    assert config.channels.web.history_limit == 50


def test_web_channel_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "channels": {
                    "web": {
                        "enabled": True,
                        "unknown": True,
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


def test_context_config_boot_files_include_builtin_skills_index():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    assert config.context.boot_files == [
        "memory/agent/persona.md",
        "memory/agent/long-term.md",
        "kernel/builtin-skills/index.md",
        "personal-skills/index.md",
    ]


def test_context_config_boot_files_as_tool_use_live_memory_only():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    assert config.context.boot_files_as_tool == [
        "memory/agent/index.md",
        "memory/agent/temp-memory.md",
    ]


def test_memory_edit_warning_defaults_match_live_structure():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    warnings = config.tools.memory_edit.warnings
    assert warnings.max_chars == 10000
    assert warnings.budgets == []
    assert warnings.ignore == [
        "temp-memory.md",
        "index.md",
        "archive/",
    ]


def test_maintenance_curation_defaults_and_strict_validation():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    curate = config.maintenance.curate
    assert curate.enabled is True
    assert curate.digest_retain_days == 14
    assert curate.digest_max_chars == 1200

    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "maintenance": {"curate": {"unknown": True}},
                "agents": {"brain": {"llm": _ollama_llm()}},
            }
        )


def test_memory_edit_warning_budget_overrides_are_strict():
    config = AppConfig.model_validate(
        {
            "tools": {
                "memory_edit": {
                    "warnings": {
                        "max_chars": 12000,
                        "budgets": [
                            {
                                "pattern": "people/",
                                "max_chars": 2000,
                            }
                        ],
                    }
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    warnings = config.tools.memory_edit.warnings
    assert warnings.max_chars == 12000
    assert warnings.budgets[0].pattern == "people/"
    assert warnings.budgets[0].max_chars == 2000

    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "tools": {
                    "memory_edit": {
                        "warnings": {
                            "max_lines": 150,
                        }
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


def test_tools_config_defaults():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    assert config.features.send_message_batch_guidance.enabled is False
    assert config.tools.shell.task_max_concurrency == 2
    assert config.tools.shell.handoff.enabled is False
    assert config.tools.shell.handoff.tail_lines == 8
    assert config.tools.shell.handoff.grace_seconds == 1.5
    assert config.tools.shell.handoff.rules == []
    assert config.tools.web_fetch.enabled is False
    assert config.tools.web_fetch.default_max_chars == 100_000
    assert config.tools.web_fetch.max_response_chars == 100_000
    assert config.tools.web_search.enabled is False
    assert config.tools.web_search.api_key_env == "TAVILY_API_KEY"
    assert config.tools.web_search.default_max_results == 5
    assert config.tools.apple_apps.context_sync.enabled is False


def test_shell_config_task_max_concurrency_override():
    config = AppConfig.model_validate(
        {
            "tools": {
                "shell": {
                    "task_max_concurrency": 4,
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.tools.shell.task_max_concurrency == 4


def test_shell_handoff_config_override():
    config = AppConfig.model_validate(
        {
            "tools": {
                "shell": {
                    "handoff": {
                        "enabled": True,
                        "tail_lines": 12,
                        "grace_seconds": 2.0,
                        "rules": [
                            {
                                "id": "auth-url",
                                "outcome": "waiting_external_action",
                                "require_url": True,
                                "any_text": ["(?i)login", "(?i)verify"],
                                "process_alive": True,
                            },
                            {
                                "id": "auth-code-prompt",
                                "outcome": "waiting_user_input",
                                "any_text": ["(?i)authorization code"],
                                "prompt_suffix": [":"],
                                "idle_seconds_ge": 1.0,
                            },
                        ],
                    }
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    handoff = config.tools.shell.handoff
    assert handoff.enabled is True
    assert handoff.tail_lines == 12
    assert handoff.grace_seconds == 2.0
    assert len(handoff.rules) == 2
    assert handoff.rules[0].require_url is True
    assert handoff.rules[1].prompt_suffix == [":"]


def test_shell_handoff_rule_requires_matcher():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "tools": {
                    "shell": {
                        "handoff": {
                            "enabled": True,
                            "rules": [
                                {
                                    "id": "empty",
                                    "outcome": "waiting_user_input",
                                }
                            ],
                        }
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


def test_shell_handoff_rule_rejects_invalid_regex():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "tools": {
                    "shell": {
                        "handoff": {
                            "enabled": True,
                            "rules": [
                                {
                                    "id": "broken",
                                    "outcome": "waiting_external_action",
                                    "any_text": ["("],
                                }
                            ],
                        }
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


def test_web_search_config_override():
    config = AppConfig.model_validate(
        {
            "tools": {
                "web_search": {
                    "enabled": True,
                    "timeout": 12,
                    "default_max_results": 4,
                    "max_results_limit": 8,
                    "include_raw_content": True,
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.tools.web_search.enabled is True
    assert config.tools.web_search.timeout == 12
    assert config.tools.web_search.default_max_results == 4
    assert config.tools.web_search.max_results_limit == 8
    assert config.tools.web_search.include_raw_content is True


def test_web_fetch_config_override():
    config = AppConfig.model_validate(
        {
            "tools": {
                "web_fetch": {
                    "enabled": True,
                    "timeout": 12,
                    "default_max_chars": 2500,
                    "max_response_chars": 5000,
                    "max_response_bytes": 123456,
                    "user_agent": "custom-agent",
                    "allow_private_hosts": True,
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.tools.web_fetch.enabled is True
    assert config.tools.web_fetch.timeout == 12
    assert config.tools.web_fetch.default_max_chars == 2500
    assert config.tools.web_fetch.max_response_chars == 5000
    assert config.tools.web_fetch.max_response_bytes == 123456
    assert config.tools.web_fetch.user_agent == "custom-agent"
    assert config.tools.web_fetch.allow_private_hosts is True


def test_agent_note_tool_config_defaults():
    config = AppConfig.model_validate(
        {
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            }
        }
    )
    assert config.tools.agent_note.max_value_chars == 80
    assert config.tools.agent_note.max_notes == 12


def test_agent_note_tool_config_override():
    config = AppConfig.model_validate(
        {
            "tools": {
                "agent_note": {
                    "max_value_chars": 40,
                    "max_notes": 5,
                }
            },
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.tools.agent_note.max_value_chars == 40
    assert config.tools.agent_note.max_notes == 5


@pytest.mark.parametrize("field", ["max_value_chars", "max_notes"])
def test_agent_note_tool_config_rejects_non_positive_values(field: str):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "tools": {
                    "agent_note": {
                        field: 0,
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


def test_discord_channel_config_validates_ranges():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "channels": {
                    "discord": {
                        "send_delay_min": 5,
                        "send_delay_max": 1,
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "channels": {
                    "discord": {
                        "dm_debounce_seconds": 200,
                        "dm_max_wait_seconds": 100,
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "channels": {
                    "discord": {
                        "debounce_seconds": 20,
                        "max_wait_seconds": 10,
                    }
                },
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )


@pytest.mark.parametrize("value", ["UTC+8", "UTC+08:00", "Asia/Taipei"])
def test_app_config_accepts_timezone_formats(value: str):
    config = AppConfig.model_validate(
        {
            "app": {"timezone": value},
            "agents": {
                "brain": {
                    "llm": _ollama_llm(),
                }
            },
        }
    )
    assert config.app.timezone == value


@pytest.mark.parametrize("value", ["UTC+25", "Taipei", "UTC+8:99"])
def test_app_config_rejects_invalid_timezone(value: str):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "app": {"timezone": value},
                "agents": {
                    "brain": {
                        "llm": _ollama_llm(),
                    }
                },
            }
        )
