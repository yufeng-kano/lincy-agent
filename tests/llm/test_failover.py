"""Tests for generic agent-level LLM failover."""

import httpx
import pytest

from lincy.core.schema import AgentConfig, ClaudeCodeConfig, OpenRouterConfig
from lincy.llm.agent_factory import create_agent_client
from lincy.llm.failover import (
    FailoverCandidate,
    llm_failover_key,
    observe_served_candidate,
    preferred_candidate_supports_vision,
    reset_failover_cooldowns,
    with_llm_failover,
)
from lincy.llm.schema import ContentPart, LLMResponse, Message


def _make_429(*, headers=None):
    request = httpx.Request("POST", "http://localhost:4142/v1/messages")
    return httpx.HTTPStatusError(
        "Rate limited",
        request=request,
        response=httpx.Response(429, request=request, headers=headers or {}),
    )


def _make_status(code: int, text: str = ""):
    request = httpx.Request("POST", "http://localhost:4142/v1/messages")
    return httpx.HTTPStatusError(
        f"HTTP {code}",
        request=request,
        response=httpx.Response(code, request=request, text=text),
    )


class _StubClient:
    def __init__(self, *, chat_effects=None, tool_effects=None):
        self.chat_effects = list(chat_effects or [])
        self.tool_effects = list(tool_effects or [])
        self.chat_calls = 0
        self.tool_calls_count = 0

    def chat(self, messages, response_schema=None, temperature=None):
        self.chat_calls += 1
        effect = self.chat_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def chat_with_tools(self, messages, tools, temperature=None):
        self.tool_calls_count += 1
        effect = self.tool_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


@pytest.fixture(autouse=True)
def _reset_global_failover_state():
    reset_failover_cooldowns()
    yield
    reset_failover_cooldowns()


def test_failover_uses_secondary_after_429():
    primary = _StubClient(
        tool_effects=[_make_429()],
    )
    fallback = _StubClient(
        tool_effects=[LLMResponse(content="ok", tool_calls=[])],
    )
    client = with_llm_failover(
        [
            FailoverCandidate(
                key="claude-primary",
                label="brain-primary",
                client=primary,
            ),
            FailoverCandidate(
                key="openrouter-fallback",
                label="brain-fallback",
                client=fallback,
            ),
        ],
        cooldown_seconds=1800,
        label="brain",
    )

    result = client.chat_with_tools([Message(role="user", content="hi")], [])

    assert result.content == "ok"
    assert primary.tool_calls_count == 1
    assert fallback.tool_calls_count == 1


def test_failover_uses_secondary_after_529():
    primary = _StubClient(
        tool_effects=[_make_status(529, '{"error":{"type":"overloaded_error","message":"Overloaded"}}')],
    )
    fallback = _StubClient(
        tool_effects=[LLMResponse(content="ok", tool_calls=[])],
    )
    client = with_llm_failover(
        [
            FailoverCandidate(
                key="claude-primary",
                label="brain-primary",
                client=primary,
            ),
            FailoverCandidate(
                key="openrouter-fallback",
                label="brain-fallback",
                client=fallback,
            ),
        ],
        cooldown_seconds=1800,
        label="brain",
    )

    result = client.chat_with_tools([Message(role="user", content="hi")], [])

    assert result.content == "ok"
    assert primary.tool_calls_count == 1
    assert fallback.tool_calls_count == 1


def test_failover_uses_secondary_after_subscription_entitlement_error():
    primary = _StubClient(
        chat_effects=[
            _make_status(
                403,
                '{"error":"this model requires a subscription, upgrade for access"}',
            )
        ],
    )
    fallback = _StubClient(chat_effects=["ok"])
    client = with_llm_failover(
        [
            FailoverCandidate("ollama-primary", "primary", primary),
            FailoverCandidate("openrouter-fallback", "fallback", fallback),
        ],
        cooldown_seconds=1800,
        label="vision",
    )

    result = client.chat([Message(role="user", content="hi")])

    assert result == "ok"
    assert primary.chat_calls == 1
    assert fallback.chat_calls == 1


def test_failover_skips_cooled_primary_when_alternative_exists():
    primary_one = _StubClient(
        tool_effects=[_make_429()],
    )
    fallback_one = _StubClient(
        tool_effects=[LLMResponse(content="first", tool_calls=[])],
    )
    client_one = with_llm_failover(
        [
            FailoverCandidate("shared-claude", "primary-one", primary_one),
            FailoverCandidate("shared-openrouter", "fallback-one", fallback_one),
        ],
        cooldown_seconds=1800,
        label="brain",
    )
    assert client_one.chat_with_tools([Message(role="user", content="hi")], []).content == "first"

    primary_two = _StubClient(
        tool_effects=[LLMResponse(content="should-not-run", tool_calls=[])],
    )
    fallback_two = _StubClient(
        tool_effects=[LLMResponse(content="second", tool_calls=[])],
    )
    client_two = with_llm_failover(
        [
            FailoverCandidate("shared-claude", "primary-two", primary_two),
            FailoverCandidate("shared-openrouter", "fallback-two", fallback_two),
        ],
        cooldown_seconds=1800,
        label="memory_editor",
    )

    result = client_two.chat_with_tools([Message(role="user", content="hi")], [])

    assert result.content == "second"
    assert primary_two.tool_calls_count == 0
    assert fallback_two.tool_calls_count == 1


def test_failover_does_not_switch_on_request_format_error():
    primary = _StubClient(
        tool_effects=[
            _make_status(
                400,
                '{"error":"Function call is missing a thought_signature in functionCall parts."}',
            )
        ],
    )
    fallback = _StubClient(
        tool_effects=[LLMResponse(content="should-not-run", tool_calls=[])],
    )
    client = with_llm_failover(
        [
            FailoverCandidate("claude-primary", "primary", primary),
            FailoverCandidate("openrouter-fallback", "fallback", fallback),
        ],
        cooldown_seconds=1800,
        label="memory_editor",
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.chat_with_tools([Message(role="user", content="hi")], [])

    assert primary.tool_calls_count == 1
    assert fallback.tool_calls_count == 0


def test_failover_does_not_switch_on_auth_error():
    primary = _StubClient(
        chat_effects=[
            _make_status(403, '{"error":"invalid api key"}')
        ],
    )
    fallback = _StubClient(chat_effects=["should-not-run"])
    client = with_llm_failover(
        [
            FailoverCandidate("primary", "primary", primary),
            FailoverCandidate("fallback", "fallback", fallback),
        ],
        cooldown_seconds=1800,
        label="vision",
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.chat([Message(role="user", content="hi")])

    assert primary.chat_calls == 1
    assert fallback.chat_calls == 0


def test_failover_skips_non_vision_candidate_when_messages_have_images():
    primary = _StubClient(tool_effects=[_make_429()])
    no_vision = _StubClient(
        tool_effects=[LLMResponse(content="should-not-run", tool_calls=[])],
    )
    vision_fallback = _StubClient(
        tool_effects=[LLMResponse(content="ok", tool_calls=[])],
    )
    client = with_llm_failover(
        [
            FailoverCandidate(
                "primary", "primary", primary, supports_vision=True
            ),
            FailoverCandidate(
                "no-vision", "no-vision", no_vision, supports_vision=False
            ),
            FailoverCandidate(
                "vision-fallback",
                "vision-fallback",
                vision_fallback,
                supports_vision=True,
            ),
        ],
        cooldown_seconds=1800,
        label="brain",
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentPart(type="text", text="see this"),
                ContentPart(
                    type="image",
                    media_type="image/png",
                    data="abc",
                ),
            ],
        )
    ]

    result = client.chat_with_tools(messages, [])

    assert result.content == "ok"
    assert primary.tool_calls_count == 1
    assert no_vision.tool_calls_count == 0
    assert vision_fallback.tool_calls_count == 1


def test_failover_errors_when_only_non_vision_left_for_image_prompt():
    primary = _StubClient(chat_effects=[_make_429()])
    no_vision = _StubClient(chat_effects=["should-not-run"])
    client = with_llm_failover(
        [
            FailoverCandidate(
                "primary", "primary", primary, supports_vision=True
            ),
            FailoverCandidate(
                "no-vision", "no-vision", no_vision, supports_vision=False
            ),
        ],
        cooldown_seconds=1800,
        label="brain",
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentPart(
                    type="image",
                    media_type="image/png",
                    data="abc",
                ),
            ],
        )
    ]

    with pytest.raises(httpx.HTTPStatusError):
        client.chat(messages)

    assert primary.chat_calls == 1
    assert no_vision.chat_calls == 0


def test_preferred_candidate_supports_vision_respects_cooldown():
    from lincy.llm import failover as failover_module

    chain = [("vision-key", True), ("text-key", False)]
    assert preferred_candidate_supports_vision(chain) is True
    failover_module._COOLDOWNS.mark("vision-key", 1800)
    assert preferred_candidate_supports_vision(chain) is False


def test_failover_key_shares_quota_bucket_across_models():
    claude_opus = ClaudeCodeConfig(
        provider="claude_code",
        model="claude-opus-4-6",
        base_url="http://localhost:4142",
    )
    claude_sonnet = ClaudeCodeConfig(
        provider="claude_code",
        model="claude-sonnet-4-6",
        base_url="http://localhost:4142",
    )
    openrouter_sonnet = OpenRouterConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
    )
    openrouter_haiku = OpenRouterConfig(
        provider="openrouter",
        model="anthropic/claude-haiku-4.5",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
    )

    assert llm_failover_key(claude_opus) == llm_failover_key(claude_sonnet)
    assert llm_failover_key(openrouter_sonnet) == llm_failover_key(openrouter_haiku)


def test_agent_factory_skips_429_retries_before_fallback(monkeypatch):
    observed: list[dict[str, object]] = []

    def _fake_create_client(
        config,
        transient_retries=0,
        request_timeout=None,
        rate_limit_retries=0,
        retry_label=None,
        **provider_kwargs,
    ):
        observed.append({
            "model": config.model,
            "rate_limit_retries": rate_limit_retries,
            "retry_label": retry_label,
        })
        return _StubClient(tool_effects=[LLMResponse(content="ok", tool_calls=[])])

    monkeypatch.setattr("lincy.llm.agent_factory.create_client", _fake_create_client)

    agent_config = AgentConfig(
        llm=ClaudeCodeConfig(
            provider="claude_code",
            model="claude-sonnet-4-6",
            base_url="http://localhost:4142",
        ),
        llm_fallbacks=[
            OpenRouterConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4.6",
                base_url="https://openrouter.ai/api/v1",
                api_key="test-key",
            )
        ],
        llm_rate_limit_retries=5,
    )

    create_agent_client(agent_config, retry_label="brain")

    assert observed == [
        {
            "model": "claude-sonnet-4-6",
            "rate_limit_retries": 0,
            "retry_label": "brain",
        },
        {
            "model": "anthropic/claude-sonnet-4.6",
            "rate_limit_retries": 5,
            "retry_label": "brain.fallback1",
        },
    ]


def _served_chain(primary: _StubClient, fallback: _StubClient):
    return with_llm_failover(
        [
            FailoverCandidate(
                key="kano-primary",
                label="kano_proxy:lincy-brain-agent",
                client=primary,
                provider="kano_proxy",
                model="lincy-brain-agent",
            ),
            FailoverCandidate(
                key="heyroute-fallback",
                label="heyroute:deepseek-v3",
                client=fallback,
                provider="heyroute",
                model="deepseek-v3",
            ),
        ],
        cooldown_seconds=1800,
        label="brain",
    )


def test_served_candidate_reports_primary_when_it_answers():
    client = _served_chain(
        _StubClient(tool_effects=[LLMResponse(content="ok", tool_calls=[])]),
        _StubClient(),
    )

    with observe_served_candidate() as probe:
        client.chat_with_tools([Message(role="user", content="hi")], [])
        served = probe.get()

    assert served is not None
    assert served.provider == "kano_proxy"
    assert served.model == "lincy-brain-agent"
    assert served.index == 0
    assert served.is_fallback is False


def test_served_candidate_reports_fallback_that_answered():
    client = _served_chain(
        _StubClient(tool_effects=[_make_429()]),
        _StubClient(tool_effects=[LLMResponse(content="ok", tool_calls=[])]),
    )

    with observe_served_candidate() as probe:
        client.chat_with_tools([Message(role="user", content="hi")], [])
        served = probe.get()

    assert served is not None
    assert served.provider == "heyroute"
    assert served.model == "deepseek-v3"
    assert served.index == 1
    assert served.is_fallback is True


def test_served_candidate_keeps_configured_index_when_primary_is_cooling_down():
    from lincy.llm import failover as failover_module

    # A cooled-down primary is attempted last, but the fallback that answers
    # must still be reported with its configured position, not its attempt order.
    failover_module._COOLDOWNS.mark("kano-primary", 1800)
    client = _served_chain(
        _StubClient(),
        _StubClient(chat_effects=["ok"]),
    )

    with observe_served_candidate() as probe:
        assert client.chat([Message(role="user", content="hi")]) == "ok"
        served = probe.get()

    assert served is not None
    assert served.index == 1
    assert served.provider == "heyroute"


def test_served_candidate_reports_the_candidate_that_raised():
    client = _served_chain(
        _StubClient(tool_effects=[_make_429()]),
        _StubClient(tool_effects=[_make_429()]),
    )

    with observe_served_candidate() as probe:
        with pytest.raises(httpx.HTTPStatusError):
            client.chat_with_tools([Message(role="user", content="hi")], [])
        served = probe.get()

    assert served is not None
    assert served.provider == "heyroute"
    assert served.index == 1


def test_served_candidate_is_unknown_without_a_failover_chain():
    single = with_llm_failover(
        [
            FailoverCandidate(
                key="only",
                label="kano_proxy:lincy-brain-agent",
                client=_StubClient(chat_effects=["ok"]),
                provider="kano_proxy",
                model="lincy-brain-agent",
            )
        ],
        cooldown_seconds=1800,
        label="brain",
    )

    with observe_served_candidate() as probe:
        assert single.chat([Message(role="user", content="hi")]) == "ok"
        assert probe.get() is None
