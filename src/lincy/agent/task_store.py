"""Persistent task store: structured todo + calendar with recurrence.

Tasks are stored in ``state/tasks.json`` and provide the agent with a
structured agenda during heartbeats and dedicated [TASK DUE] turns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from ..json_store import format_source_tag, load_json, parse_datetime, save_json
from ..timezone_utils import get_tz, now as tz_now

logger = logging.getLogger(__name__)

_FILENAME = "tasks.json"

# Recurrence pattern matchers
_RE_DAILY = re.compile(r"^daily@(\d{2}:\d{2})$")
_RE_WEEKDAYS = re.compile(r"^weekdays@(\d{2}:\d{2})$")
_RE_WEEKLY = re.compile(r"^weekly:([\d,]+)@(\d{2}:\d{2})$")
_RE_MONTHLY = re.compile(r"^monthly:(\d{1,2})@(\d{2}:\d{2})$")
_RE_INTERVAL = re.compile(r"^every:(\d+)([hm])$")


@dataclass
class Task:
    id: str
    title: str
    description: str | None
    status: str  # "pending" | "completed" | "cancelled"
    due: datetime | None
    recurrence: str | None
    source_app: str | None
    source_id: str | None
    source_label: str | None
    created_at: datetime
    completed_at: datetime | None


def _parse_time(s: str) -> time:
    """Parse ``HH:MM`` into a :class:`time` object."""
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]))


def _next_daily(t: time, after: datetime) -> datetime:
    """Return the next occurrence of *t* strictly after *after*."""
    candidate = after.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _next_weekdays(t: time, after: datetime) -> datetime:
    """Return the next Mon-Fri occurrence of *t* after *after*."""
    candidate = _next_daily(t, after)
    # 0=Mon..4=Fri, 5=Sat, 6=Sun
    while candidate.weekday() > 4:
        candidate += timedelta(days=1)
    return candidate


def _next_weekly(days: list[int], t: time, after: datetime) -> datetime:
    """Return the next occurrence on one of *days* (ISO 1=Mon..7=Sun)."""
    for d in days:
        if d < 1 or d > 7:
            raise ValueError(f"Invalid ISO weekday: {d} (must be 1-7)")
    # Convert ISO weekday to Python weekday (Mon=0..Sun=6)
    py_days = {(d - 1) % 7 for d in days}
    candidate = _next_daily(t, after)
    for _ in range(8):
        if candidate.weekday() in py_days:
            return candidate
        candidate += timedelta(days=1)
    # Fallback (should not happen with valid input)
    return candidate


def _next_monthly(day_of_month: int, t: time, after: datetime) -> datetime:
    """Return the next occurrence on *day_of_month*."""
    import calendar

    year, month = after.year, after.month
    for _ in range(13):
        max_day = calendar.monthrange(year, month)[1]
        actual_day = min(day_of_month, max_day)
        candidate = after.replace(
            year=year, month=month, day=actual_day,
            hour=t.hour, minute=t.minute, second=0, microsecond=0,
        )
        if candidate > after:
            return candidate
        # Advance to next month
        month += 1
        if month > 12:
            month = 1
            year += 1
    return after + timedelta(days=31)  # fallback


def calculate_next_due(recurrence: str, from_time: datetime) -> datetime:
    """Calculate the next due time from *from_time* given a recurrence spec.

    Raises ``ValueError`` for unrecognised recurrence formats.
    """
    tz = get_tz()
    # Ensure from_time is in local tz
    if from_time.tzinfo is None:
        from_time = from_time.replace(tzinfo=tz)
    else:
        from_time = from_time.astimezone(tz)

    m = _RE_DAILY.match(recurrence)
    if m:
        return _next_daily(_parse_time(m.group(1)), from_time)

    m = _RE_WEEKDAYS.match(recurrence)
    if m:
        return _next_weekdays(_parse_time(m.group(1)), from_time)

    m = _RE_WEEKLY.match(recurrence)
    if m:
        days = [int(d) for d in m.group(1).split(",")]
        return _next_weekly(days, _parse_time(m.group(2)), from_time)

    m = _RE_MONTHLY.match(recurrence)
    if m:
        return _next_monthly(int(m.group(1)), _parse_time(m.group(2)), from_time)

    m = _RE_INTERVAL.match(recurrence)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=val) if unit == "h" else timedelta(minutes=val)
        return from_time + delta

    raise ValueError(f"Unknown recurrence format: {recurrence!r}")


def validate_recurrence(recurrence: str) -> str | None:
    """Return an error message if *recurrence* is invalid, else ``None``."""
    try:
        calculate_next_due(recurrence, tz_now())
        return None
    except ValueError as e:
        return str(e)


def _format_due_relative(due: datetime) -> str:
    """Human-readable relative time for a due datetime."""
    now = tz_now()
    diff = due - now
    total_sec = diff.total_seconds()

    if abs(total_sec) < 60:
        return "due now"

    abs_sec = abs(total_sec)
    if abs_sec < 3600:
        mins = int(abs_sec / 60)
        label = f"{mins}m"
    elif abs_sec < 86400:
        hours = abs_sec / 3600
        label = f"{hours:.0f}h" if hours >= 2 else f"{int(abs_sec / 60)}m"
    else:
        days = abs_sec / 86400
        label = f"{days:.0f}d"

    return f"overdue {label}" if total_sec < 0 else f"due in {label}"


class TaskStore:
    """Persistent store for agent tasks."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / _FILENAME
        self._tasks: dict[str, Task] = {}
        self._next_id: int = 1
        self._load()

    # -- CRUD ---------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str | None = None,
        due: datetime | None = None,
        recurrence: str | None = None,
        source_app: str | None = None,
        source_id: str | None = None,
        source_label: str | None = None,
    ) -> Task:
        task_id = f"t_{self._next_id:04d}"
        self._next_id += 1
        task = Task(
            id=task_id,
            title=title,
            description=description,
            status="pending",
            due=due,
            recurrence=recurrence,
            source_app=source_app,
            source_id=source_id,
            source_label=source_label,
            created_at=tz_now(),
            completed_at=None,
        )
        self._tasks[task_id] = task
        self._save()
        logger.info("Created task %s: %s", task_id, title)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_pending(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.status == "pending"]

    def list_all(self) -> list[Task]:
        return list(self._tasks.values())

    def find_pending_by_source(
        self,
        *,
        source_app: str,
        source_id: str,
    ) -> Task | None:
        """Return the first pending task linked to the given external item."""
        for task in self._tasks.values():
            if task.status != "pending":
                continue
            if task.source_app == source_app and task.source_id == source_id:
                return task
        return None

    def complete(self, task_id: str) -> tuple[Task, datetime | None]:
        """Mark a task complete.

        Returns ``(task, next_due)`` where *next_due* is non-None for
        recurring tasks that have been reset to pending.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")

        now = tz_now()
        task.completed_at = now
        next_due: datetime | None = None

        if task.recurrence:
            # Recurring: calculate next due and reset
            next_due = calculate_next_due(task.recurrence, now)
            task.due = next_due
            task.status = "pending"
            logger.info("Recurring task %s completed; next due %s", task_id, next_due)
        else:
            task.status = "completed"
            logger.info("Task %s completed", task_id)

        self._save()
        return task, next_due

    def update(self, task_id: str, **kwargs: Any) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        for key, value in kwargs.items():
            if hasattr(task, key) and key not in ("id", "created_at"):
                setattr(task, key, value)
        self._save()
        return task

    def remove(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        self._save()
        logger.info("Removed task %s", task_id)
        return True

    # -- Display ------------------------------------------------------------

    def format_task_list(self, tasks: list[Task]) -> str:
        """Format tasks for display in heartbeat or tool output."""
        if not tasks:
            return "No tasks."
        lines: list[str] = []
        for t in sorted(tasks, key=lambda x: (x.due or datetime.max.replace(tzinfo=get_tz()))):
            parts = [f"[{t.id}] {t.title}"]
            if t.status != "pending":
                parts.append(f"[{t.status}]")
            source = _format_task_source(t)
            if source:
                parts.append(f"[src {source}]")
            if t.recurrence:
                parts.append(f"({t.recurrence}")
                if t.due and t.status == "pending":
                    parts[-1] += f", {_format_due_relative(t.due)}"
                parts[-1] += ")"
            elif t.due and t.status == "pending":
                parts.append(f"({_format_due_relative(t.due)})")
            lines.append("- " + " ".join(parts))
        return "\n".join(lines)

    # -- Persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = load_json(self._path, default={})
            self._next_id = raw.get("next_id", 1)
            tz = get_tz()
            for item in raw.get("tasks", []):
                task = Task(
                    id=item["id"],
                    title=item["title"],
                    description=item.get("description"),
                    status=item.get("status", "pending"),
                    due=parse_datetime(item.get("due"), tz),
                    recurrence=item.get("recurrence"),
                    source_app=item.get("source_app"),
                    source_id=item.get("source_id"),
                    source_label=item.get("source_label"),
                    created_at=parse_datetime(item.get("created_at"), tz) or tz_now(),
                    completed_at=parse_datetime(item.get("completed_at"), tz),
                )
                self._tasks[task.id] = task
            logger.info("Loaded %d tasks from %s", len(self._tasks), self._path)
        except Exception:
            logger.warning("Failed to load tasks from %s", self._path, exc_info=True)

    def _save(self) -> None:
        data = {
            "tasks": [_task_to_dict(t) for t in self._tasks.values()],
            "next_id": self._next_id,
        }
        save_json(self._path, data)


# -- Helpers ----------------------------------------------------------------


def _task_to_dict(task: Task) -> dict[str, Any]:
    d = asdict(task)
    for key in ("due", "created_at", "completed_at"):
        if d[key] is not None:
            d[key] = d[key].isoformat()
    return d


def _format_task_source(task: Task) -> str | None:
    return format_source_tag(task.source_app, task.source_label)
