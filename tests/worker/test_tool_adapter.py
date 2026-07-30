"""Tests for worker/tool_adapter.py: async dispatch worker tool."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from lincy.worker.runner import WorkerResult
from lincy.worker.tool_adapter import (
    WORKER_TOOL_DEFINITION,
    WorkerCounter,
    create_worker_tool,
    format_worker_result,
)


def _ok_result(**kwargs) -> WorkerResult:
    defaults = dict(
        success=True,
        text="All done.",
        turns_used=2,
        tokens_used=100,
        duration_ms=5,
        truncated=False,
    )
    defaults.update(kwargs)
    return WorkerResult(**defaults)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeRunner:
    """Runner that records the call and returns a fixed result."""

    def __init__(self, result: WorkerResult):
        self._result = result
        self.last_prompt: str | None = None
        self.last_kwargs: dict | None = None

    def run(self, prompt: str, **kwargs) -> WorkerResult:
        self.last_prompt = prompt
        self.last_kwargs = kwargs
        return self._result


class FakeBlockingRunner:
    """Runner that blocks until released."""

    def __init__(self, result: WorkerResult):
        self._result = result
        self.release = threading.Event()

    def run(self, prompt: str, **kwargs) -> WorkerResult:
        self.release.wait(timeout=5)
        return self._result


class FakeErrorRunner:
    """Runner that raises an exception."""

    def run(self, prompt: str, **kwargs) -> WorkerResult:
        raise RuntimeError("LLM unavailable")


class TestWorkerToolDefinition:
    def test_name_and_params(self):
        assert WORKER_TOOL_DEFINITION.name == "worker"
        assert "prompt" in WORKER_TOOL_DEFINITION.parameters
        assert "description" in WORKER_TOOL_DEFINITION.parameters
        assert "context_files" in WORKER_TOOL_DEFINITION.parameters
        assert "max_turns" in WORKER_TOOL_DEFINITION.parameters
        assert WORKER_TOOL_DEFINITION.required == ["prompt", "description"]

    def test_description_documents_async_protocol(self):
        assert "[WORKER DISPATCHED]" in WORKER_TOOL_DEFINITION.description
        assert "[worker, from system]" in WORKER_TOOL_DEFINITION.description


class TestFormatWorkerResult:
    def test_success(self):
        text = format_worker_result(_ok_result(), "demo task")
        assert "[WORKER SUCCESS]" in text
        assert "demo task" in text
        assert "All done." in text

    def test_failed_with_error(self):
        text = format_worker_result(
            _ok_result(success=False, error="boom"), "demo task"
        )
        assert "[WORKER FAILED]" in text
        assert "Error: boom" in text

    def test_truncated(self):
        text = format_worker_result(_ok_result(truncated=True), "demo task")
        assert "[WORKER TRUNCATED]" in text


class TestAsyncDispatch:
    def _make_tool(self, runner, queue, max_concurrent: int = 2):
        return create_worker_tool(
            runner,
            Path("/tmp/agent-os"),
            WorkerCounter(),
            queue=queue,
            max_concurrent=max_concurrent,
        )

    def test_dispatch_returns_immediately(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue)
        output = fn(prompt="Do the thing", description="demo task")
        assert "[WORKER DISPATCHED]" in output
        assert "worker-1" in output
        assert "demo task" in output
        assert _wait_until(lambda: mock_queue.put.called)

    def test_result_injected_into_queue(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue)
        fn(prompt="Do the thing", description="demo task")
        assert _wait_until(lambda: mock_queue.put.called)
        msg = mock_queue.put.call_args[0][0]
        assert msg.channel == "worker"
        assert msg.sender == "system"
        assert "[Worker Task Result]" in msg.content
        assert "[WORKER SUCCESS]" in msg.content
        assert "demo task" in msg.content
        assert msg.metadata["worker_label"] == "worker-1"
        assert msg.metadata["worker_description"] == "demo task"

    def test_forwards_task_parameters(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue)
        fn(
            prompt="Do the thing",
            description="demo task",
            context_files=["kernel/a.md"],
            max_turns=7,
        )
        assert _wait_until(lambda: mock_queue.put.called)
        assert runner.last_prompt == "Do the thing"
        assert runner.last_kwargs["context_files"] == ["kernel/a.md"]
        assert runner.last_kwargs["max_turns_override"] == 7
        assert runner.last_kwargs["agent_os_dir"] == Path("/tmp/agent-os")
        assert runner.last_kwargs["worker_label"] == "worker-1"

    def test_busy_when_slots_exhausted(self):
        runner = FakeBlockingRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue, max_concurrent=1)
        first = fn(prompt="Long task", description="slow")
        assert "[WORKER DISPATCHED]" in first
        second = fn(prompt="Another task", description="rejected")
        assert "[WORKER BUSY]" in second
        runner.release.set()
        assert _wait_until(lambda: mock_queue.put.called)

    def test_slot_released_after_completion(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue, max_concurrent=1)
        fn(prompt="First", description="one")
        assert _wait_until(lambda: mock_queue.put.called)
        output = fn(prompt="Second", description="two")
        assert "[WORKER DISPATCHED]" in output
        assert _wait_until(lambda: mock_queue.put.call_count == 2)

    def test_error_result_injected_and_slot_released(self):
        runner = FakeErrorRunner()
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue, max_concurrent=1)
        fn(prompt="Fail task", description="doomed")
        assert _wait_until(lambda: mock_queue.put.called)
        msg = mock_queue.put.call_args[0][0]
        assert msg.channel == "worker"
        assert "[WORKER ERROR]" in msg.content
        assert "LLM unavailable" in msg.content
        output = fn(prompt="Next", description="after error")
        assert "[WORKER DISPATCHED]" in output

    def test_empty_prompt_errors_without_dispatch(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue)
        output = fn(prompt="", description="demo")
        assert "Error" in output
        mock_queue.put.assert_not_called()

    def test_labels_increment_per_dispatch(self):
        runner = FakeRunner(_ok_result())
        mock_queue = MagicMock()
        fn = self._make_tool(runner, mock_queue)
        first = fn(prompt="A", description="one")
        second = fn(prompt="B", description="two")
        assert "worker-1" in first
        assert "worker-2" in second


class TestSyncFallback:
    def test_runs_synchronously_without_queue(self):
        runner = FakeRunner(_ok_result())
        fn = create_worker_tool(
            runner, Path("/tmp/agent-os"), WorkerCounter(), queue=None
        )
        output = fn(prompt="Do the thing", description="demo task")
        assert "[WORKER SUCCESS]" in output
        assert "[WORKER DISPATCHED]" not in output

    def test_default_description(self):
        runner = FakeRunner(_ok_result())
        fn = create_worker_tool(
            runner, Path("/tmp/agent-os"), WorkerCounter(), queue=None
        )
        output = fn(prompt="Do the thing")
        assert "worker task" in output
