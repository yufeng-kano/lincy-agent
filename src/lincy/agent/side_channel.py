"""Small parameterized tool loop for isolated side-channel calls."""

from __future__ import annotations

from collections.abc import Callable

from ..llm.base import LLMClient
from ..llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, make_tool_result_message
from .ui_event_console import AgentUiPort


def run_side_channel_tool_loop(
    *,
    client: LLMClient,
    messages: list[Message],
    tools: list[ToolDefinition],
    execute_fn: Callable[[ToolCall], object | None],
    console: AgentUiPort,
    max_iterations: int,
    spinner_label: str | None = None,
    raise_if_cancel_requested: Callable[[], None] | None = None,
    on_response: Callable[[LLMResponse], bool] | None = None,
) -> LLMResponse:
    """Run a local tool loop without mutating the main conversation."""
    response = LLMResponse(content=None, tool_calls=[])
    for _ in range(max(1, max_iterations)):
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        with console.spinner(spinner_label):
            response = client.chat_with_tools(messages, tools)
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        if on_response is not None and on_response(response):
            break
        if not response.has_tool_calls():
            break
        messages.append(
            Message(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                reasoning_details=response.reasoning_details,
                tool_calls=response.tool_calls,
            )
        )
        for tool_call in response.tool_calls:
            console.print_tool_call(tool_call)
            result = execute_fn(tool_call)
            if result is None:
                continue
            console.print_tool_result(tool_call, result.content)
            messages.append(
                make_tool_result_message(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=result.content,
                )
            )
    return response
