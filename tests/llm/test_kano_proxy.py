"""Tests for the Kano Proxy Anthropic-compatible gateway adapter."""

import pytest

from lincy.core import config as config_module
from lincy.core.schema import KanoProxyConfig
from lincy.llm.providers.kano_proxy import KanoProxyClient
from lincy.llm.schema import Message


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _FakeHttpxClient:
    def __init__(self, payload: dict, calls: list[dict]):
        self.payload = payload
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, headers: dict, json: dict) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(self.payload)


def _patch_httpx_client(monkeypatch, payload: dict, calls: list[dict]) -> None:
    monkeypatch.setattr(
        "lincy.llm.providers.anthropic.httpx.Client",
        lambda timeout: _FakeHttpxClient(payload, calls),
    )


def _make_client(**kwargs) -> KanoProxyClient:
    return KanoProxyClient(
        KanoProxyConfig(
            provider="kano_proxy",
            model="brain-agent",
            api_key="test-key",
            **kwargs,
        )
    )


def test_all_kano_proxy_profiles_load_through_real_loader(monkeypatch):
    monkeypatch.setattr(config_module, "_dotenv_values", {})
    monkeypatch.setenv("KANO_PROXY_API_KEY", "test-key")
    profile_paths = sorted(
        path.relative_to(config_module.CFGS_DIR).as_posix()
        for path in (config_module.CFGS_DIR / "llm" / "kano-proxy").rglob("*.yaml")
    )

    assert profile_paths
    for profile_path in profile_paths:
        config = config_module.resolve_llm_config(profile_path)
        assert config.provider == "kano_proxy"
        assert config.api_key == "test-key"


def test_kano_proxy_url_has_no_double_slash(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)

    result = _make_client().chat([Message(role="user", content="hi")])

    assert result == "ok"
    assert calls[0]["url"] == "https://kano-proxy.yuufeng.com/g/lincy/anthropic/v1/messages"
    assert "//v1/messages" not in calls[0]["url"]


@pytest.mark.parametrize(
    ("thinking", "expected"),
    [
        ({"type": "adaptive"}, {"type": "adaptive"}),
        (
            {"type": "enabled", "budget_tokens": 2048},
            {"type": "enabled", "budget_tokens": 2048},
        ),
        ({"type": "disabled"}, {"type": "disabled"}),
    ],
)
def test_kano_proxy_thinking_payload_variants(monkeypatch, thinking, expected):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)

    _make_client(thinking=thinking, temperature=0.2).chat(
        [Message(role="user", content="hi")]
    )

    assert calls[0]["json"]["thinking"] == expected
    if expected["type"] == "disabled":
        assert calls[0]["json"]["temperature"] == 0.2
    else:
        assert "temperature" not in calls[0]["json"]


def test_kano_proxy_effort_and_beta_header(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)

    _make_client(
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
    ).chat([Message(role="user", content="hi")])

    assert calls[0]["json"]["output_config"] == {"effort": "xhigh"}
    assert calls[0]["headers"]["anthropic-beta"] == "effort-2025-11-24"


def test_kano_proxy_default_api_key_env_is_dedicated():
    config = KanoProxyConfig(provider="kano_proxy", model="brain-agent")
    assert config.api_key_env == "KANO_PROXY_API_KEY"


def test_session_metadata_reaches_http_and_survives_retries(monkeypatch):
    import httpx

    from lincy.llm.retry import with_llm_retry
    from lincy.llm.session import llm_session

    calls = []
    original_post = _FakeHttpxClient.post

    def flaky_post(self, url, headers, json):
        response = original_post(self, url, headers, json)
        if len(calls) == 1:
            raise httpx.ReadTimeout("test timeout")
        return response

    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)
    monkeypatch.setattr(_FakeHttpxClient, "post", flaky_post)
    monkeypatch.setattr("lincy.llm.retry.time.sleep", lambda _: None)
    client = with_llm_retry(_make_client(), 1)
    messages = [Message(role="user", content="hi")]

    with llm_session("brain", "saved-session"):
        client.chat_with_tools(messages, [])
        client.chat(messages)
    with llm_session("brain", "saved-session"):
        client.chat(messages)
    with llm_session("brain", "new-session"):
        client.chat(messages)
    client.chat(messages)

    keys = [call["json"]["metadata"]["user_id"] for call in calls[:5]]
    assert len(set(keys[:4])) == 1  # Retry, tool loop, and resume.
    assert keys[4] != keys[0]
    assert "metadata" not in calls[5]["json"]
