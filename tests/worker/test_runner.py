from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, ToolParameter
from lincy.tools.registry import ToolRegistry
from lincy.worker.runner import WorkerRunner


class _FakeWorkerClient:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[Message]] = []
        self.tools: list[list[ToolDefinition]] = []

    def chat(self, messages, response_schema=None, temperature=None):
        raise NotImplementedError

    def chat_with_tools(self, messages, tools, temperature=None):
        self.calls.append([message.model_copy(deep=True) for message in messages])
        self.tools.append(list(tools))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def test_worker_session_covers_tool_loop_and_is_new_for_each_task():
    from lincy.llm.session import current_llm_session_key, llm_session

    client = _FakeWorkerClient([
        LLMResponse(tool_calls=[ToolCall(id="call", name="echo", arguments={"text": "hi"})]),
        LLMResponse(content="done"),
        LLMResponse(content="second task"),
    ])
    keys = []
    original = client.chat_with_tools

    def record(messages, tools, temperature=None):
        keys.append(current_llm_session_key())
        return original(messages, tools, temperature)

    client.chat_with_tools = record
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt")
    with llm_session("brain", "parent"):
        parent = current_llm_session_key()
        assert runner.run("first").success
        assert current_llm_session_key() == parent
        assert runner.run("second").success
    assert keys[0] is not None
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]
    assert parent not in keys


def _echo_call(call_id: str, text: str) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name="echo", arguments={"text": text})],
        total_tokens=1,
    )


def test_worker_runner_forces_final_report_at_turn_limit():
    client = _FakeWorkerClient([
        _echo_call("c1", "first value"),
        _echo_call("c2", "pending value"),
        LLMResponse(content="Report: echoed first value; c2 not run.", total_tokens=5),
    ])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt")

    result = runner.run("Echo twice", max_turns_override=1, worker_label="worker-cap")

    assert result.truncated is True
    assert result.success is False
    assert result.turns_used == 1
    assert result.tokens_used == 7
    assert result.text == "Report: echoed first value; c2 not run."
    assert len(client.calls) == 3
    assert client.tools[0] and client.tools[1]
    assert client.tools[2] == client.tools[1]
    assistant, tool_result, user = client.calls[2][-3:]
    assert assistant.role == "assistant"
    assert [tc.id for tc in assistant.tool_calls] == ["c2"]
    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == "c2"
    assert tool_result.name == "echo"
    assert tool_result.content == "Not executed: worker turn limit reached."
    assert user.role == "user"
    assert "Do not call tools" in user.content
    assert '1. echo({"text": "first value"}) -> first value' in result.action_log
    assert "2. echo(" in result.action_log
    assert "Not executed" in result.action_log


def test_worker_runner_falls_back_when_forced_report_fails():
    client = _FakeWorkerClient([
        _echo_call("c1", "first value"),
        LLMResponse(
            content="halfway",
            tool_calls=[ToolCall(id="c2", name="echo", arguments={"text": "x"})],
            total_tokens=1,
        ),
        RuntimeError("provider rejected"),
    ])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt")

    result = runner.run("Echo twice", max_turns_override=1)

    assert result.truncated is True
    assert result.success is False
    assert result.error is None
    assert result.text == "halfway"
    assert "first value" in result.action_log


def test_worker_runner_keeps_partial_action_log_on_exception():
    client = _FakeWorkerClient([
        _echo_call("c1", "first value"),
        RuntimeError("connection reset"),
    ])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt")

    result = runner.run("Echo")

    assert result.success is False
    assert result.truncated is False
    assert result.error == "connection reset"
    assert result.action_log == '1. echo({"text": "first value"}) -> first value'


def test_worker_action_log_keeps_latest_entries_within_budget():
    responses = [_echo_call(f"c{i}", f"value-{i} " + "z" * 400) for i in range(20)]
    responses.append(LLMResponse(content="done", total_tokens=1))
    client = _FakeWorkerClient(responses)
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt")

    result = runner.run("Echo many")

    assert result.success is True
    lines = result.action_log.splitlines()
    assert len(result.action_log) <= 4100
    assert lines[0].endswith("earlier entries omitted)")
    assert lines[-1].startswith("20. echo(")
    assert "value-0 " not in result.action_log


def _note_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="worker_note",
        description="note",
        parameters={"text": ToolParameter(type="string", description="text")},
        required=["text"],
    )


def test_worker_runner_injects_notes_before_context_files(tmp_path):
    from lincy.worker.notes import WorkerNotes

    notes = WorkerNotes(tmp_path / "notes.md", 1000, 500)
    notes.append("site X blocks curl")
    context_file = tmp_path / "ctx.md"
    context_file.write_text("context body", encoding="utf-8")
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt", notes=notes)

    runner.run("Do it", context_files=[str(context_file)])

    content = client.calls[0][1].content
    assert content.startswith("[Worker notes]\n- [")
    assert "site X blocks curl\n[/Worker notes]" in content
    assert content.index("[/Worker notes]") < content.index("[Context:")
    assert content.endswith("Do it")


def test_worker_runner_skips_empty_notes(tmp_path):
    from lincy.worker.notes import WorkerNotes

    notes = WorkerNotes(tmp_path / "notes.md", 1000, 500)
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt", notes=notes)

    runner.run("Do it")

    assert client.calls[0][1].content == "Do it"


def test_worker_runner_registers_extra_tools():
    calls = []
    client = _FakeWorkerClient([
        LLMResponse(
            tool_calls=[ToolCall(id="n1", name="worker_note", arguments={"text": "lesson"})],
            total_tokens=1,
        ),
        LLMResponse(content="done", total_tokens=1),
    ])
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset(),
        "system prompt",
        extra_tools={
            "worker_note": (lambda text: calls.append(text) or "ok", _note_tool_definition()),
        },
    )

    result = runner.run("Do it")

    assert result.success is True
    assert {tool.name for tool in client.tools[0]} == {"echo", "worker_note"}
    assert calls == ["lesson"]


def test_worker_runner_extra_tools_respect_exclusions():
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(
        client,
        _build_registry(),
        frozenset({"worker_note"}),
        "system prompt",
        extra_tools={"worker_note": (lambda text: "ok", _note_tool_definition())},
    )

    runner.run("Do it")

    assert [tool.name for tool in client.tools[0]] == ["echo"]


class _RecordingNotes:
    def __init__(self, compress_error: Exception | None = None):
        self.compressed_with = []
        self.compress_error = compress_error

    def read(self) -> str:
        return ""

    def compress(self, summarize):
        self.compressed_with.append(summarize)
        if self.compress_error is not None:
            raise self.compress_error
        return True


def test_worker_runner_compresses_notes_after_run():
    summarizer = lambda text: text  # noqa: E731
    notes = _RecordingNotes()
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(
        client, _build_registry(), frozenset(), "system prompt",
        notes=notes, notes_summarizer=summarizer,
    )

    result = runner.run("Do it")

    assert result.text == "done"
    assert notes.compressed_with == [summarizer]


def test_worker_runner_compresses_notes_after_failure_and_ignores_errors():
    notes = _RecordingNotes(compress_error=RuntimeError("disk full"))
    client = _FakeWorkerClient([RuntimeError("connection reset")])
    runner = WorkerRunner(
        client, _build_registry(), frozenset(), "system prompt",
        notes=notes, notes_summarizer=lambda text: text,
    )

    result = runner.run("Do it")

    assert result.success is False
    assert result.error == "connection reset"
    assert len(notes.compressed_with) == 1


def test_worker_runner_skips_compression_without_summarizer():
    notes = _RecordingNotes()
    client = _FakeWorkerClient([LLMResponse(content="done", total_tokens=1)])
    runner = WorkerRunner(client, _build_registry(), frozenset(), "system prompt", notes=notes)

    runner.run("Do it")

    assert notes.compressed_with == []
