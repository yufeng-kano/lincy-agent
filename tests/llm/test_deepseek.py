"""Tests for the DeepSeek provider adapter."""

from pathlib import Path

import pytest

from lincy.core.schema import DeepSeekConfig, DeepSeekThinkingConfig
from lincy.llm.providers.deepseek import DeepSeekClient
from lincy.llm.schema import Message, ToolCall, ToolDefinition, ToolParameter

from .conftest import FakeHttpxClient, make_openai_payload


def _patch_httpx_client(monkeypatch, payload: dict, calls: list[dict]) -> None:
    monkeypatch.setattr(
        "lincy.llm.providers.openai_compat.httpx.Client",
        lambda timeout: FakeHttpxClient([payload], calls),
    )


def _thinking_config() -> DeepSeekConfig:
    return DeepSeekConfig(
        model="deepseek-v4-pro",
        api_key="test-key",
        thinking=DeepSeekThinkingConfig(enabled=True, effort="max"),
    ).validate_reasoning(source_path=Path("test.yaml"))


def test_chat_includes_auth_url_and_thinking_max(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())

    result = client.chat([Message(role="user", content="hello")])

    assert result == "ok"
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"]["thinking"] == {"type": "enabled"}
    assert calls[0]["json"]["reasoning_effort"] == "max"


def test_chat_disabled_thinking_omits_reasoning_effort(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    config = DeepSeekConfig(
        model="deepseek-v4-flash",
        api_key="test-key",
        temperature=0.2,
        thinking=DeepSeekThinkingConfig(enabled=False),
    ).validate_reasoning(source_path=Path("test.yaml"))
    client = DeepSeekClient(config)

    _ = client.chat([Message(role="user", content="hello")])

    assert calls[0]["json"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in calls[0]["json"]
    assert calls[0]["json"]["temperature"] == 0.2


def test_validate_rejects_temperature_when_thinking_enabled():
    config = DeepSeekConfig(
        model="deepseek-v4-pro",
        api_key="test-key",
        temperature=0.2,
        thinking=DeepSeekThinkingConfig(enabled=True, effort="max"),
    )

    with pytest.raises(ValueError, match="temperature is not supported"):
        config.validate_reasoning(source_path=Path("test.yaml"))


def test_chat_rejects_temperature_override_when_thinking_enabled(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())

    with pytest.raises(ValueError, match="temperature is not supported"):
        client.chat([Message(role="user", content="hello")], temperature=0.2)

    assert calls == []


def test_validate_rejects_vision_enabled():
    config = DeepSeekConfig(
        model="deepseek-v4-pro",
        api_key="test-key",
        vision=True,
        thinking=DeepSeekThinkingConfig(enabled=True, effort="max"),
    )

    with pytest.raises(ValueError, match="vision is not supported"):
        config.validate_reasoning(source_path=Path("test.yaml"))


def test_validate_rejects_effort_when_thinking_disabled():
    config = DeepSeekConfig(
        model="deepseek-v4-flash",
        api_key="test-key",
        thinking=DeepSeekThinkingConfig(enabled=False, effort="max"),
    )

    with pytest.raises(ValueError, match="cannot be set when thinking is disabled"):
        config.validate_reasoning(source_path=Path("test.yaml"))


def test_tool_history_roundtrips_reasoning_content(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())
    messages = [
        Message(role="user", content="what date is it?"),
        Message(
            role="assistant",
            content="",
            reasoning_content="I need a date tool.",
            tool_calls=[
                ToolCall(id="call_1", name="get_date", arguments={}),
            ],
        ),
        Message(
            role="tool",
            content="2026-05-12",
            tool_call_id="call_1",
            name="get_date",
        ),
        Message(role="user", content="thanks"),
    ]

    _ = client.chat(messages)

    assistant = calls[0]["json"]["messages"][1]
    assert assistant["reasoning_content"] == "I need a date tool."
    assert "reasoning" not in assistant
    assert "reasoning_details" not in assistant


def test_thinking_tool_history_emits_reasoning_content_field(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())
    messages = [
        Message(role="user", content="load a skill"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="skill_1",
                    name="_load_skill_prerequisite",
                    arguments={"skill_name": "discord-messaging"},
                ),
            ],
        ),
        Message(
            role="tool",
            content="loaded",
            tool_call_id="skill_1",
            name="_load_skill_prerequisite",
        ),
    ]

    _ = client.chat(messages)

    assistant = calls[0]["json"]["messages"][1]
    assert assistant["reasoning_content"] == ""
    assert "reasoning" not in assistant
    assert "reasoning_details" not in assistant


def test_disabled_tool_history_does_not_synthesize_reasoning_content(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    config = DeepSeekConfig(
        model="deepseek-v4-flash",
        api_key="test-key",
        thinking=DeepSeekThinkingConfig(enabled=False),
    ).validate_reasoning(source_path=Path("test.yaml"))
    client = DeepSeekClient(config)
    messages = [
        Message(role="user", content="load a skill"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="skill_1",
                    name="_load_skill_prerequisite",
                    arguments={"skill_name": "discord-messaging"},
                ),
            ],
        ),
        Message(
            role="tool",
            content="loaded",
            tool_call_id="skill_1",
            name="_load_skill_prerequisite",
        ),
    ]

    _ = client.chat(messages)

    assistant = calls[0]["json"]["messages"][1]
    assert "reasoning_content" not in assistant


def test_chat_with_tools_parses_reasoning_and_cache_usage(monkeypatch):
    calls: list[dict] = []
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_content": "Need to call the tool.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{\"path\": \"README.md\"}",
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 42,
            "prompt_cache_hit_tokens": 30,
            "prompt_cache_miss_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 50,
        },
    }
    _patch_httpx_client(monkeypatch, payload, calls)
    client = DeepSeekClient(_thinking_config())
    tools = [
        ToolDefinition(
            name="read_file",
            description="read file",
            parameters={
                "path": ToolParameter(type="string", description="path"),
            },
            required=["path"],
        )
    ]

    result = client.chat_with_tools([Message(role="user", content="hello")], tools)

    assert result.reasoning_content == "Need to call the tool."
    assert result.tool_calls[0].name == "read_file"
    assert result.cache_read_tokens == 30
    assert result.cache_write_tokens == 0
    assert result.prompt_tokens == 42


def test_chat_rejects_response_schema(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())

    with pytest.raises(ValueError, match="does not support response_schema"):
        client.chat(
            [Message(role="user", content="hello")],
            response_schema={"type": "object"},
        )

    assert calls == []


def test_image_content_is_rejected(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, make_openai_payload("ok"), calls)
    client = DeepSeekClient(_thinking_config())

    with pytest.raises(ValueError, match="does not support image content"):
        client.chat([
            Message(
                role="user",
                content=[
                    {
                        "type": "image",
                        "data": "abc",
                        "media_type": "image/png",
                    }
                ],
            )
        ])

    assert calls == []
