import json
from pathlib import Path

from lincy.llm.schema import Message, ToolCall
from lincy.memory.tool_analysis import (
    find_missing_artifact_registry_paths,
    summarize_memory_edit_failure,
)
from lincy.session.schema import SessionEntry


def test_summarize_memory_edit_failure_includes_code_status_and_counts() -> None:
    payload = {
        "status": "failed",
        "turn_id": "t1",
        "applied": [
            {"request_id": "r1", "status": "applied", "path": "memory/people/yu-feng/basic-info.md"},
        ],
        "errors": [
            {
                "request_id": "r2",
                "code": "planner_exception",
                "detail": "Server error '503 Service Unavailable' for url 'http://localhost:4141/chat'",
            }
        ],
        "warnings": [],
    }

    summary = summarize_memory_edit_failure(json.dumps(payload, ensure_ascii=False))

    assert summary is not None
    assert "planner_exception" in summary
    assert "503" in summary
    assert "errors=1" in summary
    assert "applied=1" in summary


def test_find_missing_artifact_registry_paths_detects_relative_artifact_write() -> None:
    turn_messages = [
        SessionEntry(
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="w1",
                        name="write_file",
                        arguments={
                            "path": "artifacts/files/report.pdf",
                            "content": "stub",
                        },
                    )
                ],
            )
        ),
        SessionEntry(
            message=Message(
                role="tool",
                tool_call_id="w1",
                name="write_file",
                content="Successfully wrote 4 bytes to /tmp/runtime/artifacts/files/report.pdf",
            )
        ),
    ]

    missing = find_missing_artifact_registry_paths(
        turn_messages,
        agent_os_dir=Path("/tmp/runtime"),
    )

    assert missing == ["artifacts/files/report.pdf"]


def test_find_missing_artifact_registry_paths_skips_when_registry_was_updated() -> None:
    turn_messages = [
        SessionEntry(
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="e1",
                        name="edit_file",
                        arguments={
                            "path": "/tmp/runtime/artifacts/creations/story.md",
                            "old_string": "draft",
                            "new_string": "final",
                        },
                    ),
                    ToolCall(
                        id="m1",
                        name="memory_edit",
                        arguments={
                            "requests": [
                                {
                                    "target_path": "memory/agent/artifacts.md",
                                    "instruction": "append artifact entry",
                                }
                            ]
                        },
                    ),
                ],
            )
        ),
        SessionEntry(
            message=Message(
                role="tool",
                tool_call_id="e1",
                name="edit_file",
                content="Successfully edited /tmp/runtime/artifacts/creations/story.md",
            )
        ),
        SessionEntry(
            message=Message(
                role="tool",
                tool_call_id="m1",
                name="memory_edit",
                content=json.dumps(
                    {
                        "status": "ok",
                        "applied": [
                            {
                                "request_id": "r1",
                                "status": "applied",
                                "path": "memory/agent/artifacts.md",
                            }
                        ],
                        "errors": [],
                        "warnings": [],
                    }
                ),
            )
        ),
    ]

    missing = find_missing_artifact_registry_paths(
        turn_messages,
        agent_os_dir=Path("/tmp/runtime"),
    )

    assert missing == []
