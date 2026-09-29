"""Tests for generic agent-level LLM failover."""

import httpx
import pytest

from lincy.core.schema import AgentConfig, AnthropicConfig, OpenRouterConfig
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
        self.seen_messages: list[list[Message]] = []

    def chat(self, messages, response_schema=None, temperature=None):
        self.chat_calls += 1
        effect = self.chat_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect

    def chat_with_tools(self, messages, tools, temperature=None):
        self.tool_calls_count += 1
        self.seen_messages.append(messages)
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
    claude_opus = AnthropicConfig(
        provider="anthropic",
        model="claude-opus-4-6",
        api_key="test-key",
    )
    claude_sonnet = AnthropicConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="test-key",
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
        llm=AnthropicConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="test-key",
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
                label="kano_proxy:brain-agent",
                client=primary,
                provider="kano_proxy",
                model="brain-agent",
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
    assert served.model == "brain-agent"
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
                label="kano_proxy:brain-agent",
                client=_StubClient(chat_effects=["ok"]),
                provider="kano_proxy",
                model="brain-agent",
            )
        ],
        cooldown_seconds=1800,
        label="brain",
    )

    with observe_served_candidate() as probe:
        assert single.chat([Message(role="user", content="hi")]) == "ok"
        assert probe.get() is None


def _signed_assistant(origin: str | None, signature: str) -> Message:
    return Message(
        role="assistant",
        content="thinking done",
        reasoning_details=[{"type": "thinking", "thinking": "plan", "signature": signature}],
        reasoning_origin=origin,
    )


def _make_signature_400():
    return _make_status(400, '{"error":{"type":"invalid_request_error","message":"Corrupted thought signature."}}')


def _chain(primary, fallback):
    return with_llm_failover(
        [
            FailoverCandidate(key="claude-primary", label="brain-primary", client=primary, model="brain-agent"),
            FailoverCandidate(key="openrouter-fallback", label="brain-fallback", client=fallback, model="opus"),
        ],
        cooldown_seconds=1800,
        label="brain",
    )


def test_response_is_stamped_with_the_candidate_that_served_it():
    primary = _StubClient(tool_effects=[_make_429()])
    fallback = _StubClient(tool_effects=[LLMResponse(content="ok", tool_calls=[])])

    result = _chain(primary, fallback).chat_with_tools([Message(role="user", content="hi")], [])

    assert result.served_by == "openrouter-fallback#opus"


def test_fallback_candidate_does_not_receive_another_candidates_signatures():
    primary = _StubClient(tool_effects=[_make_429()])
    fallback = _StubClient(tool_effects=[LLMResponse(content="ok", tool_calls=[])])
    messages = [
        Message(role="user", content="hi"),
        _signed_assistant("claude-primary#brain-agent", "gemini-blob"),
        _signed_assistant("openrouter-fallback#opus", "claude-blob"),
        _signed_assistant(None, "legacy-blob"),
        Message(role="user", content="again"),
    ]

    _chain(primary, fallback).chat_with_tools(messages, [])

    seen = fallback.seen_messages[0]
    assert seen[1].reasoning_details is None
    assert seen[2].reasoning_details[0]["signature"] == "claude-blob"
    assert seen[3].reasoning_details[0]["signature"] == "legacy-blob"
    # The caller's history is untouched: only the outgoing copy is trimmed.
    assert messages[1].reasoning_details[0]["signature"] == "gemini-blob"


def test_signature_400_retries_once_without_replayed_thinking():
    primary = _StubClient(tool_effects=[_make_signature_400(), LLMResponse(content="ok", tool_calls=[])])
    fallback = _StubClient(tool_effects=[LLMResponse(content="never", tool_calls=[])])
    messages = [
        Message(role="user", content="hi"),
        _signed_assistant("claude-primary#brain-agent", "stale-blob"),
        Message(role="user", content="again"),
    ]

    result = _chain(primary, fallback).chat_with_tools(messages, [])

    assert result.content == "ok"
    assert primary.tool_calls_count == 2
    assert fallback.tool_calls_count == 0
    assert primary.seen_messages[0][1].reasoning_details is not None
    assert primary.seen_messages[1][1].reasoning_details is None


def test_signature_400_without_replayed_thinking_is_raised_as_is():
    primary = _StubClient(tool_effects=[_make_signature_400()])
    fallback = _StubClient(tool_effects=[LLMResponse(content="never", tool_calls=[])])

    with pytest.raises(httpx.HTTPStatusError):
        _chain(primary, fallback).chat_with_tools([Message(role="user", content="hi")], [])

    assert primary.tool_calls_count == 1
    assert fallback.tool_calls_count == 0


def test_served_by_includes_the_upstream_the_gateway_named():
    primary = _StubClient(
        tool_effects=[LLMResponse(content="ok", tool_calls=[], served_upstream="claude-code/claude-opus-5-5")]
    )

    result = _chain(primary, _StubClient()).chat_with_tools([Message(role="user", content="hi")], [])

    assert result.served_by == "claude-primary#brain-agent via claude-code/claude-opus-5-5"


def test_signatures_from_another_upstream_behind_the_same_candidate_are_not_replayed():
    claude = "claude-primary#brain-agent via claude-code/claude-opus-5-5"
    gemini = "claude-primary#brain-agent via antigravity/gemini-3.8-flash-high"
    primary = _StubClient(
        tool_effects=[
            LLMResponse(content="ok", tool_calls=[], served_upstream="claude-code/claude-opus-5-5"),
            LLMResponse(content="ok", tool_calls=[]),
        ]
    )
    chain = _chain(primary, _StubClient())
    messages = [
        Message(role="user", content="hi"),
        _signed_assistant(claude, "claude-blob"),
        _signed_assistant(gemini, "gemini-blob"),
        _signed_assistant("claude-primary#brain-agent", "pre-header-blob"),
        Message(role="user", content="again"),
    ]

    # Nothing served yet in this process: everything this candidate minted gets one try.
    chain.chat_with_tools(messages, [])
    first = primary.seen_messages[0]
    assert [m.reasoning_details is not None for m in first[1:4]] == [True, True, True]

    # Claude Code answered, so only Claude Code's signatures go back on the next turn.
    chain.chat_with_tools(messages, [])
    second = primary.seen_messages[1]
    assert second[1].reasoning_details[0]["signature"] == "claude-blob"
    assert second[2].reasoning_details is None
    assert second[3].reasoning_details is None
