"""Tests for recall tool wiring in setup_tools."""

from pathlib import Path
from unittest.mock import MagicMock

from lincy.agent.tool_setup import setup_tools
from lincy.core.schema import ToolsConfig
from lincy.llm.schema import ToolCall
from lincy.tools.builtin.recall import RecallAgent


def test_no_recall_agent_no_tool(tmp_path: Path):
    registry, _, _ = setup_tools(ToolsConfig(allowed_paths=[]), tmp_path)
    assert not registry.has_tool("recall")


def test_recall_agent_registers_tool(tmp_path: Path):
    fake_agent = MagicMock(spec=RecallAgent)
    fake_agent.recall.return_value = "- [2026-11-05 14:44][session/x] took the medicine"
    registry, _, _ = setup_tools(
        ToolsConfig(allowed_paths=[]), tmp_path, recall_agent=fake_agent,
    )
    assert registry.has_tool("recall")

    result = registry.execute(
        ToolCall(id="call_1", name="recall", arguments={"question": "when did he take it"})
    )
    assert "took the medicine" in result.content
    assert result.is_error is False
    assert fake_agent.recall.call_args.args == ("when did he take it",)

    fake_agent.recall.return_value = None
    result = registry.execute(
        ToolCall(id="call_2", name="recall", arguments={"question": "anything"})
    )
    assert result.content == "No relevant memory found."
