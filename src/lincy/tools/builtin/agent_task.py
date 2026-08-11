"""agent_task tool: structured task management with recurrence.

The agent uses this to create, complete, list, update, and remove tasks.
Tasks with a ``due`` time automatically get a queue wake-up message so
the agent is woken at the right time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ...llm.schema import ToolDefinition, ToolParameter
from .batch_helpers import format_source_result, validate_source_fields
from ...timezone_utils import get_tz, now as tz_now

if TYPE_CHECKING:
    from ...agent.queue import PersistentPriorityQueue
    from ...agent.task_store import TaskStore

logger = logging.getLogger(__name__)

_TASK_DUE_PRIORITY = 3

_TASK_DUE_TEMPLATE = (
    "[TASK DUE]\n"
    "Task: [{task_id}] {title}\n"
    "Recurrence: {recurrence}\n\n"
    'Process this task. When done, call agent_task(action="complete", task_id="{task_id}").'
)

AGENT_TASK_DEFINITION = ToolDefinition(
    name="agent_task",
    description=(
        "Manage structured tasks (todo + calendar). "
        "'create' adds a task with optional due time and recurrence, "
        "'complete' marks it done (recurring tasks auto-schedule next), "
        "'list' shows all tasks, "
        "'update' modifies a task, "
        "'remove' deletes a task."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["create", "complete", "list", "update", "remove"],
        ),
        "task_id": ToolParameter(
            type="string",
            description="Task ID (required for complete/update/remove).",
        ),
        "title": ToolParameter(
            type="string",
            description="Task title — high-level intent, not specific instructions (required for create).",
        ),
        "description": ToolParameter(
            type="string",
            description="Optional longer description.",
        ),
        "due": ToolParameter(
            type="string",
            description=(
                "Due time as ISO datetime in local time, e.g. '2026-03-30T06:00'. "
                "Optional for create/update."
            ),
        ),
        "recurrence": ToolParameter(
            type="string",
            description=(
                "Recurrence spec. Formats: "
                "'daily@HH:MM', 'weekdays@HH:MM', 'weekly:1,3,5@HH:MM' "
                "(ISO weekday 1=Mon..7=Sun), 'monthly:D@HH:MM', "
                "'every:Nh', 'every:Nm'. Optional for create/update."
            ),
        ),
        "source_app": ToolParameter(
            type="string",
            description="Optional external source namespace, e.g. 'calendar' or 'reminders'.",
        ),
        "source_id": ToolParameter(
            type="string",
            description="Optional external source item id, such as an event uid or reminder id.",
        ),
        "source_label": ToolParameter(
            type="string",
            description="Optional source label for derived tasks, e.g. 'prep' or 'follow_up'.",
        ),
    },
    required=["action"],
)


def create_agent_task(
    task_store: TaskStore,
    queue: PersistentPriorityQueue,
) -> Callable[..., str]:
    """Create an agent_task function bound to a task store and queue."""
    from ...agent.schema import InboundMessage
    from ...agent.task_store import validate_recurrence

    tz = get_tz()

    def agent_task(
        action: str,
        task_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        due: str | None = None,
        recurrence: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
    ) -> str:
        source_error = validate_source_fields(
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
        )
        if source_error:
            return source_error
        if action == "create":
            return _handle_create(
                title,
                description,
                due,
                recurrence,
                source_app,
                source_id,
                source_label,
            )
        if action == "complete":
            return _handle_complete(task_id)
        if action == "list":
            return _handle_list()
        if action == "update":
            return _handle_update(
                task_id,
                title,
                description,
                due,
                recurrence,
                source_app,
                source_id,
                source_label,
            )
        if action == "remove":
            return _handle_remove(task_id)
        return f"Error: unknown action '{action}'"

    def _parse_due(due_str: str) -> datetime | str:
        """Parse due string to datetime. Returns error string on failure."""
        try:
            dt = datetime.fromisoformat(due_str)
        except ValueError:
            return f"Error: invalid datetime format: {due_str!r}"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)

    def _enqueue_wakeup(task_id: str, title: str, due_dt: datetime, recurrence_str: str | None) -> None:
        """Enqueue a [TASK DUE] wake-up message for a task."""
        content = _TASK_DUE_TEMPLATE.format(
            task_id=task_id,
            title=title,
            recurrence=recurrence_str or "one-time",
        )
        msg = InboundMessage(
            channel="system",
            content=content,
            priority=_TASK_DUE_PRIORITY,
            sender="system",
            metadata={"task_id": task_id, "task_due": True},
            timestamp=due_dt,
            not_before=due_dt,
        )
        queue.put(msg)

    def _remove_wakeup(task_id: str) -> None:
        """Remove any pending wake-up message for a task."""
        for filepath, msg in queue.scan_pending(channel="system"):
            if msg.metadata.get("task_id") == task_id:
                queue.remove_pending(filepath)
                break

    def _handle_create(
        title: str | None,
        description: str | None,
        due_str: str | None,
        recurrence_str: str | None,
        source_app: str | None,
        source_id: str | None,
        source_label: str | None,
    ) -> str:
        if not title:
            return "Error: 'title' is required for create"

        # Validate recurrence
        if recurrence_str:
            err = validate_recurrence(recurrence_str)
            if err:
                return f"Error: invalid recurrence: {err}"

        # Parse due
        due_dt: datetime | None = None
        if due_str:
            result = _parse_due(due_str)
            if isinstance(result, str):
                return result
            due_dt = result
        elif recurrence_str:
            # Auto-calculate first due from recurrence
            from ...agent.task_store import calculate_next_due
            due_dt = calculate_next_due(recurrence_str, tz_now())

        task = task_store.create(
            title=title,
            description=description,
            due=due_dt,
            recurrence=recurrence_str,
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
        )

        if due_dt is not None:
            _enqueue_wakeup(task.id, task.title, due_dt, recurrence_str)

        parts = [f"OK: created [{task.id}] {task.title}"]
        source = format_source_result(task.source_app, task.source_label, task.source_id)
        if source:
            parts.append(f"source: {source}")
        if due_dt:
            parts.append(f"due: {due_dt.strftime('%Y-%m-%d %H:%M')}")
        if recurrence_str:
            parts.append(f"recurrence: {recurrence_str}")
        return " | ".join(parts)

    def _handle_complete(task_id: str | None) -> str:
        if not task_id:
            return "Error: 'task_id' is required for complete"

        try:
            task, next_due = task_store.complete(task_id)
        except KeyError as e:
            return f"Error: {e}"

        _remove_wakeup(task_id)

        if next_due is not None:
            _enqueue_wakeup(task.id, task.title, next_due, task.recurrence)
            return (
                f"OK: completed [{task.id}]; "
                f"next due: {next_due.strftime('%Y-%m-%d %H:%M')}"
            )
        return f"OK: completed [{task.id}] (one-time, done)"

    def _handle_list() -> str:
        tasks = task_store.list_all()
        return task_store.format_task_list(tasks)

    def _handle_update(
        task_id: str | None,
        title: str | None,
        description: str | None,
        due_str: str | None,
        recurrence_str: str | None,
        source_app: str | None,
        source_id: str | None,
        source_label: str | None,
    ) -> str:
        if not task_id:
            return "Error: 'task_id' is required for update"

        old_task = task_store.get(task_id)
        if old_task is None:
            return f"Error: task not found: {task_id}"

        # Validate recurrence if changing
        if recurrence_str:
            err = validate_recurrence(recurrence_str)
            if err:
                return f"Error: invalid recurrence: {err}"

        kwargs: dict = {}
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if recurrence_str is not None:
            kwargs["recurrence"] = recurrence_str
        if source_app is not None:
            kwargs["source_app"] = source_app
        if source_id is not None:
            kwargs["source_id"] = source_id
        if source_label is not None:
            kwargs["source_label"] = source_label

        # Parse due
        due_dt: datetime | None = None
        due_changed = False
        if due_str is not None:
            result = _parse_due(due_str)
            if isinstance(result, str):
                return result
            due_dt = result
            kwargs["due"] = due_dt
            due_changed = True

        task = task_store.update(task_id, **kwargs)
        if task is None:
            return f"Error: task not found: {task_id}"

        # Re-schedule wake-up if due changed
        if due_changed:
            _remove_wakeup(task_id)
            if due_dt is not None:
                _enqueue_wakeup(task.id, task.title, due_dt, task.recurrence)

        source = format_source_result(task.source_app, task.source_label, task.source_id)
        if source:
            return f"OK: updated [{task.id}] | source: {source}"
        return f"OK: updated [{task.id}]"

    def _handle_remove(task_id: str | None) -> str:
        if not task_id:
            return "Error: 'task_id' is required for remove"

        if not task_store.remove(task_id):
            return f"Error: task not found: {task_id}"

        _remove_wakeup(task_id)
        return f"OK: removed [{task_id}]"

    return agent_task
