"""Brain-facing worker tool definition and factory."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..llm.schema import ToolDefinition, ToolParameter
from ..tools.background_dispatch import dispatch_background_task
from .runner import WorkerResult, WorkerRunner

if TYPE_CHECKING:
    from ..agent.queue import PersistentPriorityQueue

logger = logging.getLogger(__name__)

WORKER_TOOL_DEFINITION = ToolDefinition(
    name="worker",
    description=(
        "Delegate a multi-step task to an autonomous worker subagent. "
        "The worker runs with its own independent context window and can use "
        "all available tools except gui_task and worker itself. "
        "Write the prompt as a self-contained task description -- "
        "the worker has NO access to the current conversation context. "
        "Include all necessary details, file paths, and success criteria.\n"
        "\n"
        "Asynchronous: returns immediately with [WORKER DISPATCHED]. "
        "The result is delivered later as a [worker, from system] message; "
        "continue the conversation while waiting. "
        "[WORKER BUSY] means the concurrency cap is reached -- wait for a "
        "running worker's result before dispatching more."
    ),
    parameters={
        "prompt": ToolParameter(
            type="string",
            description="Complete task description for the worker subagent.",
        ),
        "description": ToolParameter(
            type="string",
            description="3-5 word summary shown in logs.",
        ),
        "context_files": ToolParameter(
            type="array",
            description="Optional file paths to read and inject as context.",
            items={"type": "string"},
        ),
        "max_turns": ToolParameter(
            type="integer",
            description="Optional override for max agentic loop iterations.",
        ),
    },
    required=["prompt", "description"],
)


class WorkerCounter:
    """Thread-safe per-session counter for worker numbering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def next(self) -> int:
        with self._lock:
            self._count += 1
            return self._count


def format_worker_result(result: WorkerResult, description: str) -> str:
    """Format a WorkerResult into a human-readable status string."""
    if result.truncated:
        status = "TRUNCATED"
    elif result.success:
        status = "SUCCESS"
    else:
        status = "FAILED"
    header = (
        f"[WORKER {status}] ({description}) "
        f"turns: {result.turns_used}, tokens: {result.tokens_used}, "
        f"time: {result.duration_ms}ms"
    )
    parts = [header]
    if result.text:
        parts.append(result.text)
    if result.error:
        parts.append(f"Error: {result.error}")
    return "\n".join(parts)


def create_worker_tool(
    runner: WorkerRunner,
    agent_os_dir: Path,
    counter: WorkerCounter,
    queue: "PersistentPriorityQueue | None" = None,
    max_concurrent: int = 2,
) -> Callable[..., str]:
    """Create worker tool callable bound to a WorkerRunner.

    When *queue* is provided the task runs in a background thread and the
    result is injected into the queue as an ``InboundMessage``; the tool
    returns immediately with a dispatch confirmation.  When *queue* is
    ``None`` the task runs synchronously (test/direct call compatibility).

    Concurrency is capped by *max_concurrent*; excess dispatches return
    ``[WORKER BUSY]`` instead of queuing.
    """
    slots = threading.BoundedSemaphore(max_concurrent)

    def _run(
        prompt: str,
        file_list: list[str] | None,
        turns_override: int | None,
        worker_label: str,
    ) -> WorkerResult:
        return runner.run(
            prompt,
            context_files=file_list,
            max_turns_override=turns_override,
            agent_os_dir=agent_os_dir,
            worker_label=worker_label,
        )

    def _result_message(worker_label: str, description: str, body: str):
        from ..agent.schema import InboundMessage

        return InboundMessage(
            channel="worker",
            content=(
                f"[Worker Task Result]\n"
                f"Worker: {worker_label}\n"
                f"Task: {description}\n\n"
                f"{body}"
            ),
            priority=0,
            sender="system",
            metadata={
                "worker_label": worker_label,
                "worker_description": description,
            },
        )

    def worker_impl(
        prompt: str = "",
        description: str = "",
        context_files: Any = None,
        max_turns: Any = None,
        **_kwargs: Any,
    ) -> str:
        if not prompt:
            return "Error: prompt is required"
        if not description:
            description = "worker task"

        file_list: list[str] | None = None
        if isinstance(context_files, list):
            file_list = [str(f) for f in context_files]

        turns_override: int | None = None
        if isinstance(max_turns, int) and max_turns > 0:
            turns_override = max_turns

        worker_num = counter.next()
        worker_label = f"worker-{worker_num}"

        # Synchronous fallback (no queue -- tests / direct call)
        if queue is None:
            result = _run(prompt, file_list, turns_override, worker_label)
            return format_worker_result(result, description)

        def format_background(result: WorkerResult | None, error: Exception | None):
            if error is not None:
                logger.error("Background worker task error: %s", error)
                body = f"[WORKER ERROR] {error}"
            else:
                assert result is not None
                body = format_worker_result(result, description)
            return _result_message(worker_label, description, body)

        busy = dispatch_background_task(
            queue,
            slots,
            "WORKER",
            lambda: _run(prompt, file_list, turns_override, worker_label),
            format_background,
        )
        if busy is not None:
            return (
                "[WORKER BUSY] Too many worker tasks are already running. "
                "Wait for a [worker, from system] result message before "
                "dispatching more, or retry later via schedule_action."
            )
        return (
            f"[WORKER DISPATCHED] {worker_label} ({description}) is running "
            "in background. The result will be delivered as a "
            "[worker, from system] message; continue without waiting."
        )

    return worker_impl
