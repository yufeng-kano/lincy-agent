from __future__ import annotations

from pathlib import Path

from lincy.agent.turn_runtime import _TurnMemorySnapshot
from lincy.llm.schema import ToolCall


def _memory_edit_call(path: str) -> ToolCall:
    return ToolCall(
        id="tc1",
        name="memory_edit",
        arguments={
            "as_of": "2026-05-17T20:05:00+08:00",
            "turn_id": "turn-1",
            "requests": [
                {
                    "request_id": "r1",
                    "target_path": path,
                    "instruction": "append",
                }
            ],
        },
    )


def test_turn_memory_snapshot_restores_temp_memory_in_full(tmp_path: Path):
    """The state file is rewritten in place, so rollback must restore its bytes."""
    target = tmp_path / "memory" / "agent" / "temp-memory.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "# \u4eca\u65e5\u73fe\u6cc1\n\n## \u6700\u5f8c\u4e92\u52d5\n\n- existing\n".encode("utf-8")
    target.write_bytes(original)

    snapshot = _TurnMemorySnapshot(agent_os_dir=tmp_path)
    snapshot.capture_from_tool_call(_memory_edit_call("memory/agent/temp-memory.md"))

    target.write_bytes(b"# rewritten\n")

    restored = snapshot.rollback()

    assert restored == 1
    assert target.read_bytes() == original


def test_turn_memory_snapshot_still_restores_regular_memory_file(tmp_path: Path):
    target = tmp_path / "memory" / "agent" / "long-term.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original\n")

    snapshot = _TurnMemorySnapshot(agent_os_dir=tmp_path)
    snapshot.capture_from_tool_call(_memory_edit_call("memory/agent/long-term.md"))

    target.write_bytes(b"changed\n")

    restored = snapshot.rollback()

    assert restored == 1
    assert target.read_bytes() == b"original\n"
