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


class TestWorkerToolOverrides:
    def test_override_replaces_source_implementation(self):
        source = _source_registry()
        override_defn = ToolDefinition(
            name="read_file", description="unguarded", parameters={},
        )
        runner = WorkerRunner(
            client=None,
            source_registry=source,
            excluded_tools=frozenset(),
            system_prompt="worker",
            tool_overrides={"read_file": (lambda: "override ran", override_defn)},
        )

        worker_registry = runner._build_filtered_registry()
        result = worker_registry.execute(
            ToolCall(id="c1", name="read_file", arguments={})
        )
        assert result.content == "override ran"
        # Source registry keeps the original implementation.
        original = source.execute(ToolCall(id="c2", name="read_file", arguments={}))
        assert original.content == "ran read_file"

    def test_override_ignored_for_excluded_tool(self):
        source = _source_registry()
        runner = WorkerRunner(
            client=None,
            source_registry=source,
            excluded_tools=frozenset({"read_file"}),
            system_prompt="worker",
            tool_overrides={
                "read_file": (
                    lambda: "override ran",
                    ToolDefinition(name="read_file", description="x", parameters={}),
                )
            },
        )

        worker_registry = runner._build_filtered_registry()
        assert worker_registry.has_tool("read_file") is False


class TestBuildWorkerFileTools:
    def test_worker_file_tools_can_write_memory(self, tmp_path):
        from lincy.agent.tool_setup import build_worker_file_tools

        agent_os_dir = tmp_path / "agent"
        (agent_os_dir / "memory" / "agent").mkdir(parents=True)
        tools = build_worker_file_tools([str(tmp_path)], agent_os_dir)

        write_file, write_defn = tools["write_file"]
        edit_file, edit_defn = tools["edit_file"]
        assert write_defn.name == "write_file"
        assert edit_defn.name == "edit_file"

        target = agent_os_dir / "memory" / "agent" / "long-term.md"
        write_result = write_file(str(target), "# Long term\n- entry\n")
        assert "Error" not in write_result
        assert target.read_text() == "# Long term\n- entry\n"

        edit_result = edit_file(str(target), "- entry", "- entry v2", False)
        assert "Error" not in edit_result
        assert "- entry v2" in target.read_text()

    def test_worker_file_tools_respect_allowed_paths(self, tmp_path):
        from lincy.agent.tool_setup import build_worker_file_tools

        agent_os_dir = tmp_path / "agent"
        agent_os_dir.mkdir()
        tools = build_worker_file_tools([str(agent_os_dir)], agent_os_dir)

        write_file, _ = tools["write_file"]
        result = write_file("/etc/lincy-test-forbidden.txt", "nope")
        assert "not allowed" in result
