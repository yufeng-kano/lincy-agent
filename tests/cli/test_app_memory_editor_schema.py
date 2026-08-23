from lincy.cli import app as app_module
from lincy.core.schema import AgentConfig, DeepSeekConfig, OllamaNativeConfig


def test_agent_supports_response_schema_requires_all_fallback_candidates():
    agent_config = AgentConfig(
        llm=OllamaNativeConfig(
            provider="ollama",
            model="qwen3.5:9b",
            thinking={"mode": "toggle", "enabled": False},
        ),
        llm_fallbacks=[
            DeepSeekConfig(
                provider="deepseek",
                model="deepseek-v4-flash",
                thinking={"enabled": True, "effort": "max"},
            )
        ],
    )

    assert app_module._agent_supports_response_schema(agent_config) is False
