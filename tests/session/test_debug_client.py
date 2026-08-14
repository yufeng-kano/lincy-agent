import httpx
import pytest

from lincy.llm.failover import (
    FailoverCandidate,
    reset_failover_cooldowns,
    with_llm_failover,
)
from lincy.llm.schema import LLMResponse, Message
from lincy.session.debug_client import wrap_llm_client_with_session_debug


class _Sink:
    def begin_llm_request(self, **kwargs):
        return None

    def complete_llm_response(self, pending, *, response, latency_ms):
        return None

    def complete_llm_text_response(self, pending, *, response_text, latency_ms):
        return None

    def fail_llm_request(self, pending, *, error, latency_ms):
        return None


class _Client:
    def chat(self, messages, response_schema=None, temperature=None):
        return "ok"

    def chat_with_tools(self, messages, tools, temperature=None):
        raise NotImplementedError

    def compact_messages(self, messages, tools=None):
        return [Message(role="assistant", content="compact-ok")]


def test_debug_wrapper_preserves_compact_messages():
    wrapped = wrap_llm_client_with_session_debug(
        _Client(),
        sink=_Sink(),
        client_label="brain",
        provider="codex",
        model="gpt-5.4",
    )

    assert hasattr(wrapped, "compact_messages")
    result = wrapped.compact_messages([Message(role="user", content="hi")])
    assert result == [Message(role="assistant", content="compact-ok")]


class _RecordingSink:
    """Capture the kwargs the debug wrapper hands to the session store."""

    def __init__(self):
        self.completed = []
        self.failed = []

    def begin_llm_request(self, **kwargs):
        return "pending"

    def complete_llm_response(self, pending, *, response, latency_ms, served=None):
        self.completed.append(served)

    def complete_llm_text_response(self, pending, *, response_text, latency_ms, served=None):
        self.completed.append(served)

    def fail_llm_request(self, pending, *, error, latency_ms, served=None):
        self.failed.append(served)


def _failover_client(primary, fallback):
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


def _rate_limited():
    request = httpx.Request("POST", "http://localhost:4142/v1/messages")
    return httpx.HTTPStatusError(
        "Rate limited",
        request=request,
        response=httpx.Response(429, request=request),
    )


class _ChainClient:
    def __init__(self, effect):
        self._effect = effect

    def chat(self, messages, response_schema=None, temperature=None):
        if isinstance(self._effect, Exception):
            raise self._effect
        return self._effect

    def chat_with_tools(self, messages, tools, temperature=None):
        if isinstance(self._effect, Exception):
            raise self._effect
        return self._effect


def test_debug_wrapper_records_the_fallback_that_served():
    reset_failover_cooldowns()
    sink = _RecordingSink()
    wrapped = wrap_llm_client_with_session_debug(
        _failover_client(
            _ChainClient(_rate_limited()),
            _ChainClient(LLMResponse(content="ok", tool_calls=[])),
        ),
        sink=sink,
        client_label="brain",
        provider="kano_proxy",
        model="lincy-brain-agent",
    )

    wrapped.chat_with_tools([Message(role="user", content="hi")], [])

    served = sink.completed[0]
    assert served is not None
    assert (served.provider, served.model, served.index) == (
        "heyroute",
        "deepseek-v3",
        1,
    )
    reset_failover_cooldowns()


def test_debug_wrapper_records_served_candidate_on_failure():
    reset_failover_cooldowns()
    sink = _RecordingSink()
    wrapped = wrap_llm_client_with_session_debug(
        _failover_client(
            _ChainClient(_rate_limited()),
            _ChainClient(_rate_limited()),
        ),
        sink=sink,
        client_label="brain",
        provider="kano_proxy",
        model="lincy-brain-agent",
    )

    with pytest.raises(httpx.HTTPStatusError):
        wrapped.chat([Message(role="user", content="hi")])

    served = sink.failed[0]
    assert served is not None
    assert served.provider == "heyroute"
    reset_failover_cooldowns()


def test_debug_wrapper_leaves_served_unknown_without_failover():
    sink = _RecordingSink()
    wrapped = wrap_llm_client_with_session_debug(
        _Client(),
        sink=sink,
        client_label="brain",
        provider="codex",
        model="gpt-5.4",
    )

    assert wrapped.chat([Message(role="user", content="hi")]) == "ok"
    assert sink.completed == [None]
