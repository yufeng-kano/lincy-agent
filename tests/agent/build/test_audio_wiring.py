"""Tests for audio tool wiring in setup_tools."""

from pathlib import Path
from unittest.mock import MagicMock

from lincy.agent.tool_setup import setup_tools
from lincy.core.schema import ToolsConfig
from lincy.llm.schema import ToolCall
from lincy.tools.builtin.audio import AudioAgent


def test_no_audio_agent_no_tool(tmp_path: Path):
    registry, _, _ = setup_tools(ToolsConfig(allowed_paths=[]), tmp_path)
    assert not registry.has_tool("read_audio_by_subagent")


def test_audio_agent_registers_subagent_tool(tmp_path: Path):
    fake_agent = MagicMock(spec=AudioAgent)
    fake_agent.describe.return_value = "Speaker 1: hello"
    registry, _, _ = setup_tools(
        ToolsConfig(allowed_paths=[]), tmp_path, audio_agent=fake_agent,
    )
    assert registry.has_tool("read_audio_by_subagent")

    clip = tmp_path / "clip.ogg"
    clip.write_bytes(b"OggS")
    result = registry.execute(
        ToolCall(
            id="call_1",
            name="read_audio_by_subagent",
            arguments={"path": str(clip), "context": "a friend saying hi"},
        )
    )
    assert "Speaker 1: hello" in result.content
    parts = fake_agent.describe.call_args.args[0]
    assert parts[1].type == "audio"
