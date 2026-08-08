"""Tests for the Heyroute Anthropic-compatible gateway adapter."""

import pytest

from lincy.core import config as config_module
from lincy.core.schema import HeyrouteConfig
from lincy.llm.providers.heyroute import HeyrouteClient
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


def _make_client(**kwargs) -> HeyrouteClient:
    return HeyrouteClient(
        HeyrouteConfig(
            provider="heyroute",
            model="claude-sonnet-test",
            api_key="test-key",
            **kwargs,
        )
    )


def test_all_heyroute_profiles_load_through_real_loader(monkeypatch):
    monkeypatch.setenv("HEYROUTE_API_KEY", "test-key")
    profile_paths = sorted(
        path.relative_to(config_module.CFGS_DIR).as_posix()
        for path in (config_module.CFGS_DIR / "llm" / "heyroute").rglob("*.yaml")
    )

    assert profile_paths
    for profile_path in profile_paths:
        config = config_module.resolve_llm_config(profile_path)
        assert config.provider == "heyroute"
        assert config.api_key == "test-key"


def test_heyroute_url_has_no_double_slash(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)

    result = _make_client().chat([Message(role="user", content="hi")])

    assert result == "ok"
    assert calls[0]["url"] == "https://heyroute.ai/v1/messages"
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
def test_heyroute_thinking_payload_variants(monkeypatch, thinking, expected):
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


def test_heyroute_effort_and_beta_header(monkeypatch):
    calls: list[dict] = []
    _patch_httpx_client(monkeypatch, {"content": [{"type": "text", "text": "ok"}]}, calls)

    _make_client(
        thinking={"type": "adaptive"},
        output_config={"effort": "xhigh"},
    ).chat([Message(role="user", content="hi")])

    assert calls[0]["json"]["output_config"] == {"effort": "xhigh"}
    assert calls[0]["headers"]["anthropic-beta"] == "effort-2025-11-24"
