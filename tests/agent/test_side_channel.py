"""Tests for isolated side-channel tool calls."""

from contextlib import nullcontext

from lincy.agent.side_channel import run_side_channel_tool_loop
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition
from lincy.tools.registry import ToolResult


class _RetryingClient:
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.responses = [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="other", name="other_tool", arguments={}),
                    ToolCall(id="memory", name="memory_edit", arguments={}),
                ]
            ),
            LLMResponse(content="retry complete"),
        ]

    def chat_with_tools(self, messages, tools):
        del tools
        self.calls.append([message.model_copy(deep=True) for message in messages])
        return self.responses.pop(0)


class _Console:
    def spinner(self, text=None):
        del text
        return nullcontext()

    def print_tool_call(self, tool_call) -> None:
        del tool_call

    def print_tool_result(self, tool_call, result) -> None:
        del tool_call, result


def test_side_channel_records_results_for_rejected_tool_calls():
    client = _RetryingClient()
    messages = [Message(role="user", content="sync memory")]
    tools = [
        ToolDefinition(name="other_tool", description="other", parameters={}),
        ToolDefinition(name="memory_edit", description="edit", parameters={}),
    ]

    def execute(tool_call: ToolCall):
        if tool_call.name == "memory_edit":
            return ToolResult("Error: memory edit failed", is_error=True)
        return None

    run_side_channel_tool_loop(
        client=client,
        messages=messages,
        tools=tools,
        execute_fn=execute,
        console=_Console(),
        max_iterations=1,
    )
    run_side_channel_tool_loop(
        client=client,
        messages=messages,
        tools=tools,
        execute_fn=execute,
        console=_Console(),
        max_iterations=1,
    )

    retry_messages = client.calls[1]
    assistant = next(message for message in retry_messages if message.role == "assistant")
    assert [tool_call.id for tool_call in assistant.tool_calls] == ["other", "memory"]
    results = [message for message in retry_messages if message.role == "tool"]
    assert [(message.tool_call_id, message.name, message.content) for message in results] == [
        (
            "other",
            "other_tool",
            "Error: Tool 'other_tool' is unavailable in this side channel",
        ),
        ("memory", "memory_edit", "Error: memory edit failed"),
    ]
