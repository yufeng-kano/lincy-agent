"""Tests for per-agent tool exclusion (FilteredToolRegistry + startup check)."""

import pytest

from lincy.agent.tool_setup import validate_excluded_tools
from lincy.core.schema import AgentConfig, ClaudeCodeConfig
from lincy.llm.schema import ToolCall, ToolDefinition
from lincy.tools.registry import FilteredToolRegistry, ToolRegistry
from lincy.worker.runner import WorkerRunner


def _register(registry: ToolRegistry, name: str) -> None:
    registry.register(
        name,
        lambda: f"ran {name}",
        ToolDefinition(name=name, description=f"{name} tool", parameters={}),
    )


def _source_registry() -> ToolRegistry:
    registry = ToolRegistry()
    _register(registry, "execute_shell")
    _register(registry, "read_file")
    return registry


class TestFilteredToolRegistry:
    def test_excluded_tool_is_hidden_from_definitions(self):
        view = FilteredToolRegistry(_source_registry(), frozenset({"execute_shell"}))

        names = [defn.name for defn in view.get_definitions()]
        assert names == ["read_file"]

    def test_execute_excluded_tool_returns_unknown_tool_error(self):
        view = FilteredToolRegistry(_source_registry(), frozenset({"execute_shell"}))

        result = view.execute(ToolCall(id="c1", name="execute_shell", arguments={}))
        assert "Unknown tool" in result.content
        assert result.is_error is True
        assert view.has_tool("execute_shell") is False

    def test_non_excluded_tools_pass_through(self):
        source = _source_registry()
        source.set_side_effect_tools(frozenset({"read_file"}))
        view = FilteredToolRegistry(source, frozenset({"execute_shell"}))

        result = view.execute(ToolCall(id="c1", name="read_file", arguments={}))
        assert result.content == "ran read_file"
        assert result.is_error is False
        assert view.has_tool("read_file") is True
        # Unrelated accessors delegate to the source registry.
        assert view.is_side_effect("read_file") is True

    def test_tools_registered_after_construction_are_visible(self):
        source = _source_registry()
        view = FilteredToolRegistry(source, frozenset({"execute_shell"}))

        _register(source, "worker")

        assert view.has_tool("worker") is True
        assert "worker" in [defn.name for defn in view.get_definitions()]

    def test_worker_keeps_shell_while_brain_view_excludes_it(self):
        source = _source_registry()
        brain_view = FilteredToolRegistry(source, frozenset({"execute_shell"}))
        runner = WorkerRunner(
            client=None,
            source_registry=source,
            excluded_tools=frozenset({"shell_task"}),
            system_prompt="worker",
        )

        worker_registry = runner._build_filtered_registry()
        assert worker_registry.has_tool("execute_shell") is True
        assert brain_view.has_tool("execute_shell") is False


def _agent_config(excluded_tools: list[str]) -> AgentConfig:
    return AgentConfig(
        llm=ClaudeCodeConfig(
            provider="claude_code",
            model="claude-sonnet-5",
            base_url="http://localhost:4142",
        ),
        excluded_tools=excluded_tools,
    )


class TestValidateExcludedTools:
    def test_unknown_tool_name_aborts_startup(self):
        agents = {"brain": _agent_config(["execute_shell", "typo_tool"])}

        with pytest.raises(SystemExit, match="agents.brain.excluded_tools"):
            validate_excluded_tools(_source_registry(), agents)

    def test_registered_names_pass(self):
        agents = {
            "brain": _agent_config(["execute_shell"]),
            "worker": _agent_config([]),
        }

        validate_excluded_tools(_source_registry(), agents)
