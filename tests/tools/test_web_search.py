"""Tests for Tavily-backed web_search tool."""

from pathlib import Path

import httpx

from lincy.agent.tool_setup import setup_tools
from lincy.agent.staged_planning import build_stage1_tools
from lincy.core.schema import ToolsConfig
from lincy.tools.builtin.web_search import (
    WEB_SEARCH_DEFINITION,
    create_web_search,
)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://api.tavily.com/search")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, *, response: _FakeResponse | Exception, calls: list[dict], timeout: float) -> None:
        self._response = response
        self._calls = calls
        self._timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self._calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": self._timeout,
            }
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestWebSearchDefinition:
    def test_name_and_params(self):
        assert WEB_SEARCH_DEFINITION.name == "web_search"
        assert WEB_SEARCH_DEFINITION.required == ["query"]
        assert "include_domains" in WEB_SEARCH_DEFINITION.parameters
        assert "time_range" in WEB_SEARCH_DEFINITION.parameters


class TestCreateWebSearch:
    def test_formats_results_and_maps_payload(self, monkeypatch):
        calls: list[dict] = []
        response = _FakeResponse(
            {
                "results": [
                    {
                        "title": "OpenAI Pricing",
                        "url": "https://openai.com/pricing",
                        "content": "Latest API pricing details and usage notes.",
                    },
                    {
                        "title": "Status",
                        "url": "https://status.openai.com",
                        "content": "Operational status for API services.",
                    },
                ]
            }
        )

        monkeypatch.setattr(
            "lincy.tools.builtin.web_search.httpx.Client",
            lambda timeout: _FakeClient(response=response, calls=calls, timeout=timeout),
        )

        tool = create_web_search(
            api_key="test-key",
            timeout=7.5,
            default_max_results=5,
            max_results_limit=3,
            include_raw_content=False,
        )

        output = tool(
            query="openai pricing",
            max_results=9,
            include_domains=["openai.com", " status.openai.com "],
            time_range="month",
        )

        assert output.startswith("Search results for: openai pricing (2 results)")
        assert "[1] OpenAI Pricing" in output
        assert "https://openai.com/pricing" in output
        assert "Latest API pricing details" in output
        assert calls == [
            {
                "url": "https://api.tavily.com/search",
                "json": {
                    "query": "openai pricing",
                    "topic": "general",
                    "max_results": 3,
                    "include_raw_content": False,
                    "include_domains": ["openai.com", "status.openai.com"],
                    "time_range": "month",
                },
                "headers": {"Authorization": "Bearer test-key"},
                "timeout": 7.5,
            }
        ]

    def test_returns_validation_errors(self):
        tool = create_web_search(api_key="test-key")

        assert tool(query="") == "Error: query is required."
        assert tool(query="test", max_results=0) == "Error: max_results must be a positive integer."
        assert tool(query="test", time_range="hour") == (
            "Error: time_range must be one of: day, week, month, year."
        )
        assert tool(query="test", include_domains="openai.com") == (
            "Error: include_domains must be a list of strings."
        )
        assert tool(query="test", include_domains=[""]) == (
            "Error: include_domains must contain non-empty strings."
        )

    def test_handles_http_errors(self, monkeypatch):
        calls: list[dict] = []
        timeout_exc = httpx.TimeoutException("timed out")

        monkeypatch.setattr(
            "lincy.tools.builtin.web_search.httpx.Client",
            lambda timeout: _FakeClient(response=timeout_exc, calls=calls, timeout=timeout),
        )
        tool = create_web_search(api_key="test-key")
        assert tool(query="latest") == "Error: Search timed out."

        unauthorized = _FakeResponse({}, status_code=401)
        monkeypatch.setattr(
            "lincy.tools.builtin.web_search.httpx.Client",
            lambda timeout: _FakeClient(response=unauthorized, calls=calls, timeout=timeout),
        )
        assert tool(query="latest") == "Error: Invalid TAVILY_API_KEY."


class TestWebSearchWiring:
    def test_setup_tools_skips_web_search_when_disabled(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("lincy.agent.tool_setup.dotenv_values", lambda: {})
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        config = ToolsConfig.model_validate({"allowed_paths": []})

        registry, _, _ = setup_tools(config, tmp_path)

        assert not registry.has_tool("web_search")

    def test_setup_tools_registers_web_search_when_enabled_and_key_present(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        monkeypatch.setattr("lincy.agent.tool_setup.dotenv_values", lambda: {})
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        config = ToolsConfig.model_validate({"allowed_paths": [], "web_search": {"enabled": True}})

        registry, _, _ = setup_tools(config, tmp_path)

        assert registry.has_tool("web_search")

    def test_setup_tools_skips_web_search_without_key(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("lincy.agent.tool_setup.dotenv_values", lambda: {})
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        config = ToolsConfig.model_validate({"allowed_paths": [], "web_search": {"enabled": True}})

        registry, _, _ = setup_tools(config, tmp_path)

        assert not registry.has_tool("web_search")

    def test_stage1_whitelist_includes_web_search(self):
        tools = build_stage1_tools([WEB_SEARCH_DEFINITION])

        assert [tool.name for tool in tools] == ["web_search"]
