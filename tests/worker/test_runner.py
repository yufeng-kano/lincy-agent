from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, ToolParameter
from lincy.tools.registry import ToolRegistry
from lincy.worker.runner import WorkerRunner


class _FakeWorkerClient:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    def chat(self, messages, response_schema=None, temperature=None):
        raise NotImplementedError

    def chat_with_tools(self, messages, tools, temperature=None):
        self.calls.append([message.model_copy(deep=True) for message in messages])
        return self._responses.pop(0)


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "echo",
        lambda text: text,
        ToolDefinition(
            name="echo",
            description="Echo text",
            parameters={"text": ToolParameter(type="string", description="text")},
            required=["text"],
        ),
    )
    return registry


def test_worker_runner_trims_large_initial_prompt():
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset(),
        "system prompt",
        max_context_tokens=80,
    )

    result = runner.run("A" * 2000, worker_label="worker-trim")

    assert result.success is True
    assert len(client.calls) == 1
    user_message = client.calls[0][1]
    assert isinstance(user_message.content, str)
    assert user_message.content.startswith("[Earlier context trimmed]")
    assert len(user_message.content) < 2000


def test_worker_runner_drops_old_tool_turns_when_context_budget_is_small():
    first_tool_result = "A" * 320
    second_tool_result = "B" * 320
    client = _FakeWorkerClient(
        [
            LLMResponse(
                content="step1",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": first_tool_result})],
                total_tokens=1,
            ),
            LLMResponse(
                content="step2",
                tool_calls=[ToolCall(id="call-2", name="echo", arguments={"text": second_tool_result})],
                total_tokens=1,
            ),
            LLMResponse(content="done", total_tokens=1),
        ]
    )
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset(),
        "system prompt",
        max_context_tokens=220,
    )

    result = runner.run("short prompt", worker_label="worker-compact")

    assert result.success is True
    assert len(client.calls) == 3
    third_call_messages = client.calls[2]
    tool_outputs = [
        message.content
        for message in third_call_messages
        if message.role == "tool" and isinstance(message.content, str)
    ]
    assert first_tool_result not in tool_outputs
    assert second_tool_result in tool_outputs


class _RecordingConsole:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.results: list[tuple[str, str]] = []

    def print_subagent_tool_call(self, label, tool_call):
        self.calls.append((label, tool_call.name))

    def print_subagent_tool_result(self, label, tool_call, content):
        self.results.append((label, tool_call.name))


class _ExplodingConsole:
    def print_subagent_tool_call(self, label, tool_call):
        raise RuntimeError("ui down")

    def print_subagent_tool_result(self, label, tool_call, content):
        raise RuntimeError("ui down")


def _tool_call_response() -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
        total_tokens=1,
    )


def test_worker_runner_surfaces_tool_activity_to_console():
    client = _FakeWorkerClient([
        _tool_call_response(),
        LLMResponse(content="done", total_tokens=1),
    ])
    console = _RecordingConsole()
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset(),
        "system prompt",
        ui_console=console,
    )

    result = runner.run("Say hi", worker_label="worker-9")

    assert result.success is True
    assert console.calls == [("worker-9", "echo")]
    assert console.results == [("worker-9", "echo")]


def test_worker_runner_survives_console_failure():
    client = _FakeWorkerClient([
        _tool_call_response(),
        LLMResponse(content="done", total_tokens=1),
    ])
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset(),
        "system prompt",
        ui_console=_ExplodingConsole(),
    )

    result = runner.run("Say hi", worker_label="worker-9")

    assert result.success is True
    assert result.text == "done"
