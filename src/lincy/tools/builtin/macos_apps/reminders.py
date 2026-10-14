"""macOS Reminders tool backed by EventKit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import threading
from typing import Any

import EventKit

from ....llm.schema import ToolDefinition, ToolParameter
from .eventkit import (
    components_to_iso,
    components_to_nsdate,
    datetime_to_components,
    open_store,
    save_error,
    to_nsdate,
)
from .reminders_db import read_flags
from .runtime import (
    _datetime_in_app_tz,
    _error,
    _json_output,
    _parse_local_datetime,
)

REMINDERS_TOOL_DEFINITION = ToolDefinition(
    name="reminders_tool",
    description=(
        "Access the user's real macOS Reminders data. "
        "'catalog' lists reminder lists, "
        "'search' searches reminders, "
        "'get' fetches one reminder by id, "
        "'create' creates a reminder, "
        "'update' updates a reminder by id, "
        "'complete' marks a reminder complete/incomplete."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["catalog", "search", "get", "create", "update", "complete"],
        ),
        "list_id": ToolParameter(
            type="string",
            description="Reminder list id. Preferred over list_name when known.",
        ),
        "list_name": ToolParameter(
            type="string",
            description="Exact reminder list name.",
        ),
        "list_path": ToolParameter(
            type="string",
            description="Reminder list path from catalog, e.g. 'iCloud/Work'.",
        ),
        "reminder_id": ToolParameter(
            type="string",
            description="Reminder id. Required for update/complete.",
        ),
        "query": ToolParameter(
            type="string",
            description="Case-insensitive text query matched against title and notes.",
        ),
        "title": ToolParameter(
            type="string",
            description="Reminder title. Required for create.",
        ),
        "notes": ToolParameter(
            type="string",
            description="Reminder notes/body.",
        ),
        "due": ToolParameter(
            type="string",
            description="Local ISO datetime, e.g. '2026-04-20T09:00'.",
        ),
        "due_start": ToolParameter(
            type="string",
            description="Lower bound for reminder due date when searching.",
        ),
        "due_end": ToolParameter(
            type="string",
            description="Upper bound for reminder due date when searching.",
        ),
        "priority": ToolParameter(
            type="integer",
            description="Reminder priority: 0 none, 1-4 high, 5 medium, 6-9 low.",
        ),
        "priority_min": ToolParameter(
            type="integer",
            description="Optional minimum priority filter for search.",
        ),
        "priority_max": ToolParameter(
            type="integer",
            description="Optional maximum priority filter for search.",
        ),
        "flagged": ToolParameter(
            type="boolean",
            description="Whether the reminder is flagged. Search filter or write value.",
        ),
        "completed": ToolParameter(
            type="boolean",
            description="Whether the reminder is completed. Search filter or write value.",
        ),
        "sort_by": ToolParameter(
            type="string",
            description="Sort order for search results.",
            enum=["due_asc", "due_desc", "title_asc"],
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum number of search results to return.",
        ),
    },
    required=["action"],
)

_REMINDER_URL_PREFIX = "x-apple-reminder://"


def _list_path(calendar: Any) -> str:
    return f"{calendar.source().title()}/{calendar.title()}"


def _reminder_row(reminder: Any, flagged: bool | None) -> dict[str, Any]:
    calendar = reminder.calendar()
    return {
        "id": reminder.calendarItemIdentifier(),
        "title": reminder.title(),
        "notes": reminder.notes(),
        "completed": bool(reminder.isCompleted()),
        "due": components_to_iso(reminder.dueDateComponents()),
        "priority": int(reminder.priority()),
        # EventKit has no API for the flag; callers read it from the Reminders database.
        "flagged": flagged,
        "list_id": calendar.calendarIdentifier(),
        "list_name": calendar.title(),
        "list_path": _list_path(calendar),
    }


def _find_list(
    store: Any,
    *,
    list_id: str | None,
    list_name: str | None,
    list_path: str | None,
) -> Any:
    """Return the reminder list named by id, path, or name (in that priority)."""
    for calendar in store.calendarsForEntityType_(EventKit.EKEntityTypeReminder):
        if list_id:
            if calendar.calendarIdentifier() == list_id:
                return calendar
        elif list_path:
            if _list_path(calendar) == list_path:
                return calendar
        elif calendar.title() == list_name:
            return calendar
    raise ValueError(f"reminders list not found: {list_id or list_path or list_name}")


def _find_reminder(store: Any, reminder_id: str) -> Any:
    # Older tool output and the agent's memory carry the JXA form of the id.
    item = store.calendarItemWithIdentifier_(reminder_id.removeprefix(_REMINDER_URL_PREFIX))
    return item if isinstance(item, EventKit.EKReminder) else None


def _set_due(reminder: Any, due: datetime) -> None:
    old_due = reminder.dueDateComponents()
    old_date = components_to_nsdate(old_due) if old_due is not None else None
    components = datetime_to_components(due)
    reminder.setDueDateComponents_(components)
    # A JXA dueDate write also produced one absolute alarm at the due time; that
    # alarm is what makes Reminders notify, so move it along with the due date.
    # Other alarms (relative "1 day early", location, custom times) are the
    # user's and stay.
    alarms = [
        alarm
        for alarm in reminder.alarms() or []
        if old_date is None or alarm.absoluteDate() is None or not alarm.absoluteDate().isEqualToDate_(old_date)
    ]
    # Derive the alarm from the stored components so the next update finds it
    # by exact date even when due carried sub-second precision.
    alarms.append(EventKit.EKAlarm.alarmWithAbsoluteDate_(components_to_nsdate(components)))
    reminder.setAlarms_(alarms)


_SET_FLAGGED_JXA = """
const app = Application("Reminders");
const payload = readPayload();
app.reminders.byId(`x-apple-reminder://${payload.reminder_id}`).flagged.set(payload.flagged);
return { ok: true };
"""


class MacOSAppBridge:

    def _set_reminder_flagged(self, reminder_id: str, flagged: bool) -> None:
        """Write the flag through Reminders.app; EventKit cannot.

        A single by-id reference is one Apple Event, unlike the whose() scans
        that made the old JXA backend time out.
        """
        self._run_jxa_json(
            _SET_FLAGGED_JXA,
            payload={"reminder_id": reminder_id, "flagged": flagged},
            operation="reminders set flagged",
        )

    def _flag_saved_reminder(self, reminder: Any, flagged: bool) -> dict[str, Any]:
        """Write the flag of a reminder already saved through EventKit.

        The save is committed, so a failed flag write must not report the call
        as failed: the agent would retry a create and duplicate the reminder.
        """
        identifier = reminder.calendarItemIdentifier()
        try:
            self._set_reminder_flagged(identifier, flagged)
        except RuntimeError as exc:
            # _run_jxa_json has already logged the failure.
            return {
                "ok": True,
                "reminder": _reminder_row(reminder, read_flags().get(identifier)),
                "warning": f"reminder saved, but flagged={flagged} was not applied: {exc}",
            }
        # Report the value just written rather than re-reading the database,
        # which can trail the Reminders.app write.
        return {"ok": True, "reminder": _reminder_row(reminder, flagged)}

    def reminders_catalog(self) -> dict[str, Any]:
        """List reminder accounts and lists."""
        accounts: dict[str, dict[str, Any]] = {}
        with open_store(EventKit.EKEntityTypeReminder) as store:
            calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
            for calendar in calendars:
                source = calendar.source()
                account = accounts.setdefault(
                    source.sourceIdentifier(),
                    {"id": source.sourceIdentifier(), "name": source.title(), "lists": []},
                )
                account["lists"].append(
                    {
                        "id": calendar.calendarIdentifier(),
                        "name": calendar.title(),
                        "account": source.title(),
                        "path": _list_path(calendar),
                    }
                )
        return {"ok": True, "accounts": list(accounts.values()), "count": len(calendars)}

    def reminders_search(
        self,
        *,
        list_id: str | None,
        list_name: str | None,
        list_path: str | None,
        query: str | None,
        due_start: str | None,
        due_end: str | None,
        completed: bool | None,
        flagged: bool | None,
        priority_min: int | None,
        priority_max: int | None,
        sort_by: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        """Search reminders."""
        start = _datetime_in_app_tz(_parse_local_datetime(due_start, field_name="due_start")) if due_start else None
        end = _datetime_in_app_tz(_parse_local_datetime(due_end, field_name="due_end")) if due_end else None
        with open_store(EventKit.EKEntityTypeReminder) as store:
            calendars = None
            if list_id or list_name or list_path:
                calendars = [_find_list(store, list_id=list_id, list_name=list_name, list_path=list_path)]
            if completed is False:
                # Bounds are widened by a day because EventKit does not document how it
                # places floating (date-only) due dates; exact bounds are applied below.
                predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                    to_nsdate(start - timedelta(days=1)) if start else None,
                    to_nsdate(end + timedelta(days=1)) if end else None,
                    calendars,
                )
            elif completed is True:
                predicate = store.predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(
                    None, None, calendars
                )
            else:
                predicate = store.predicateForRemindersInCalendars_(calendars)

            fetched: list[Any] = []
            done = threading.Event()

            def on_fetched(reminders: Any) -> None:
                fetched.extend(reminders or [])
                done.set()

            store.fetchRemindersMatchingPredicate_completion_(predicate, on_fetched)
            if not done.wait(self._timeout_seconds):
                raise RuntimeError(f"reminders search timed out after {self._timeout_seconds:.1f} seconds")

            flags = read_flags()
            needle = (query or "").lower()
            results = []
            for reminder in fetched:
                row = _reminder_row(reminder, flags.get(reminder.calendarItemIdentifier()))
                due = datetime.fromisoformat(row["due"]) if row["due"] else None
                if flagged is not None and row["flagged"] != flagged:
                    continue
                if priority_min is not None and row["priority"] < priority_min:
                    continue
                if priority_max is not None and row["priority"] > priority_max:
                    continue
                if start and (due is None or due < start):
                    continue
                if end and (due is None or due > end):
                    continue
                if needle and needle not in f"{row['title'] or ''}\n{row['notes'] or ''}".lower():
                    continue
                results.append((due, row))

        if sort_by == "title_asc":
            results.sort(key=lambda item: item[1]["title"] or "")
        else:
            # Reminders without a due date go last in both directions.
            dated = sorted((item for item in results if item[0]), key=lambda item: item[0], reverse=sort_by == "due_desc")
            results = dated + [item for item in results if not item[0]]
        limit = max(1, min(limit or self._max_search_results, self._max_search_results))
        truncated = len(results) > limit
        rows = [row for _, row in results[:limit]]
        return {
            "ok": True,
            "results": rows,
            "count": len(rows),
            "limit": limit,
            "truncated": truncated,
            "warning": f"results hit limit {limit}; narrow the filters or increase limit" if truncated else None,
        }

    def reminders_get(self, *, reminder_id: str) -> dict[str, Any]:
        """Fetch one reminder by id."""
        with open_store(EventKit.EKEntityTypeReminder) as store:
            reminder = _find_reminder(store, reminder_id)
            if reminder is None:
                return {"ok": False, "error": f"reminder not found: {reminder_id}"}
            return {"ok": True, "reminder": _reminder_row(reminder, read_flags().get(reminder.calendarItemIdentifier()))}

    def reminders_create(
        self,
        *,
        list_id: str | None,
        list_name: str | None,
        list_path: str | None,
        title: str,
        notes: str | None,
        due: datetime | None,
        priority: int | None,
        flagged: bool | None,
    ) -> dict[str, Any]:
        """Create a reminder."""
        with open_store(EventKit.EKEntityTypeReminder) as store:
            if list_id or list_name or list_path:
                calendar = _find_list(store, list_id=list_id, list_name=list_name, list_path=list_path)
            else:
                calendar = store.defaultCalendarForNewReminders()
            reminder = EventKit.EKReminder.reminderWithEventStore_(store)
            reminder.setCalendar_(calendar)
            reminder.setTitle_(title)
            if notes:
                reminder.setNotes_(notes)
            if priority is not None:
                reminder.setPriority_(priority)
            if due is not None:
                _set_due(reminder, due)
            saved, error = store.saveReminder_commit_error_(reminder, True, None)
            if not saved:
                return save_error(error)
            if flagged:
                return self._flag_saved_reminder(reminder, True)
            return {"ok": True, "reminder": _reminder_row(reminder, False)}

    def reminders_update(
        self,
        *,
        reminder_id: str,
        title: str | None,
        notes: str | None,
        due: datetime | None,
        priority: int | None,
        flagged: bool | None,
        completed: bool | None,
    ) -> dict[str, Any]:
        """Update a reminder."""
        with open_store(EventKit.EKEntityTypeReminder) as store:
            reminder = _find_reminder(store, reminder_id)
            if reminder is None:
                return {"ok": False, "error": f"reminder not found: {reminder_id}"}
            if title is not None:
                reminder.setTitle_(title)
            if notes is not None:
                reminder.setNotes_(notes)
            if priority is not None:
                reminder.setPriority_(priority)
            if completed is not None:
                reminder.setCompleted_(completed)
            if due is not None:
                _set_due(reminder, due)
            saved, error = store.saveReminder_commit_error_(reminder, True, None)
            if not saved:
                return save_error(error)
            if flagged is None:
                return {"ok": True, "reminder": _reminder_row(reminder, read_flags().get(reminder.calendarItemIdentifier()))}
            return self._flag_saved_reminder(reminder, flagged)


def create_reminders_tool(bridge: MacOSAppBridge) -> Callable[..., str]:
    """Create reminders_tool bound to the bridge."""

    def reminders_tool(
        action: str,
        list_id: str | None = None,
        list_name: str | None = None,
        list_path: str | None = None,
        reminder_id: str | None = None,
        query: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        due_start: str | None = None,
        due_end: str | None = None,
        priority: int | None = None,
        priority_min: int | None = None,
        priority_max: int | None = None,
        flagged: bool | None = None,
        completed: bool | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> str:

            if action == "catalog":
                return _json_output(bridge.reminders_catalog())
            if action == "search":
                return _json_output(
                    bridge.reminders_search(
                        list_id=list_id,
                        list_name=list_name,
                        list_path=list_path,
                        query=query,
                        due_start=due_start,
                        due_end=due_end,
                        completed=completed,
                        flagged=flagged,
                        priority_min=priority_min,
                        priority_max=priority_max,
                        sort_by=sort_by,
                        limit=limit,
                    )
                )
            if action == "get":
                if not reminder_id:
                    return _error("'reminder_id' is required for get")
                return _json_output(bridge.reminders_get(reminder_id=reminder_id))
            if action == "create":
                if not title:
                    return _error("'title' is required for create")
                due_dt = _parse_local_datetime(due, field_name="due") if due else None
                return _json_output(
                    bridge.reminders_create(
                        list_id=list_id,
                        list_name=list_name,
                        list_path=list_path,
                        title=title,
                        notes=notes,
                        due=due_dt,
                        priority=priority,
                        flagged=flagged,
                    )
                )
            if action in {"update", "complete"}:
                if not reminder_id:
                    return _error("'reminder_id' is required for update/complete")
                due_dt = _parse_local_datetime(due, field_name="due") if due else None
                if action == "complete" and completed is None:
                    completed = True
                if (
                    title is None
                    and notes is None
                    and due_dt is None
                    and priority is None
                    and flagged is None
                    and completed is None
                ):
                    return _error("update requires at least one field to change")
                return _json_output(
                    bridge.reminders_update(
                        reminder_id=reminder_id,
                        title=title,
                        notes=notes,
                        due=due_dt,
                        priority=priority,
                        flagged=flagged,
                        completed=completed,
                    )
                )
            return _error(f"unknown action '{action}'")


    return reminders_tool
