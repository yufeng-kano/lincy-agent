"""macOS Reminders tool backed by EventKit, with ReminderKit for the fields EventKit lacks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

import EventKit
from Foundation import NSURL
import objc
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ....llm.schema import ToolDefinition, ToolParameter
from ...security import resolve_allowed_path
from .eventkit import (
    components_to_iso,
    components_to_nsdate,
    date_to_components,
    datetime_to_components,
    is_date_only,
    open_store,
    save_error,
    to_nsdate,
)
from .reminderkit import image_size, parse_early_reminder, write_private_fields
from .reminders_db import ReminderExtras, read_extras, read_flags
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
            description=(
                "Local ISO datetime, e.g. '2026-04-20T09:00', or a date like '2026-04-20' "
                "for an all-day reminder without a time."
            ),
        ),
        "early_reminder": ToolParameter(
            type="string",
            description=(
                "Extra notification before the due date: a positive amount and a unit "
                "(m, h, d, w, mo), e.g. '30m', '2h', '1d', '1w'. Needs a due date. "
                "The reminder stays on its due date; do not move 'due' earlier instead."
            ),
        ),
        "recurrence": ToolParameter(
            type="string",
            description="Repeat rule. Needs a due date.",
            enum=["daily", "weekly", "monthly", "yearly"],
        ),
        "recurrence_interval": ToolParameter(
            type="integer",
            description="Repeat every N units of 'recurrence'. Default 1.",
        ),
        "urgent": ToolParameter(
            type="boolean",
            description=(
                "When true, the reminder rings as an alarm on the user's iPhone at the due time "
                "until dismissed. Needs a due date with a time."
            ),
        ),
        "tags": ToolParameter(
            type="array",
            description="Tags without '#' or spaces. Replaces the reminder's existing tags.",
            items={"type": "string"},
        ),
        "url": ToolParameter(
            type="string",
            description="URL attached to the reminder, including the scheme.",
        ),
        "location": ToolParameter(
            type="object",
            description="Place that triggers the reminder when the user arrives or leaves.",
            json_schema={
                "properties": {
                    "title": {"type": "string"},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "radius_m": {"type": "number", "description": "Default 100."},
                    "trigger": {"type": "string", "enum": ["arrive", "leave"], "description": "Default arrive."},
                },
                "required": ["title", "latitude", "longitude"],
            },
        ),
        "subtasks": ToolParameter(
            type="array",
            description="Titles of subtasks to add under the reminder.",
            items={"type": "string"},
        ),
        "images": ToolParameter(
            type="array",
            description="Paths of image files to attach. Must be within allowed paths.",
            items={"type": "string"},
        ),
        "clear": ToolParameter(
            type="array",
            description="Update only: fields to remove from the reminder.",
            items={"type": "string", "enum": ["early_reminder", "recurrence", "location", "url", "tags"]},
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
_CLEARABLE_FIELDS = ("early_reminder", "recurrence", "location", "url", "tags")
_RECURRENCE_FREQUENCIES = {
    "daily": EventKit.EKRecurrenceFrequencyDaily,
    "weekly": EventKit.EKRecurrenceFrequencyWeekly,
    "monthly": EventKit.EKRecurrenceFrequencyMonthly,
    "yearly": EventKit.EKRecurrenceFrequencyYearly,
}
_RECURRENCE_NAMES = {value: name for name, value in _RECURRENCE_FREQUENCIES.items()}

# pyobjc-framework-CoreLocation is not a dependency; a location alarm only
# needs the CLLocation class, which the runtime provides once the framework loads.
objc.loadBundle("CoreLocation", {}, bundle_path="/System/Library/Frameworks/CoreLocation.framework")


class ReminderLocation(BaseModel):
    """A place that triggers a reminder when the user arrives or leaves."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=100, gt=0)
    trigger: Literal["arrive", "leave"] = "arrive"


def _list_path(calendar: Any) -> str:
    return f"{calendar.source().title()}/{calendar.title()}"


def _location_row(reminder: Any) -> dict[str, Any] | None:
    for alarm in reminder.alarms() or []:
        place = alarm.structuredLocation()
        if place is None:
            continue
        geo = place.geoLocation()
        # Without the CoreLocation bindings pyobjc returns the coordinate
        # struct as a (latitude, longitude) tuple.
        latitude, longitude = geo.coordinate() if geo is not None else (None, None)
        return {
            "title": place.title(),
            "latitude": latitude,
            "longitude": longitude,
            "radius_m": place.radius(),
            "trigger": "leave" if alarm.proximity() == EventKit.EKAlarmProximityLeave else "arrive",
        }
    return None


def _reminder_row(reminder: Any, flagged: bool | None, extras: ReminderExtras | None) -> dict[str, Any]:
    calendar = reminder.calendar()
    due = reminder.dueDateComponents()
    row = {
        "id": reminder.calendarItemIdentifier(),
        "title": reminder.title(),
        "notes": reminder.notes(),
        "completed": bool(reminder.isCompleted()),
        "due": components_to_iso(due),
        "priority": int(reminder.priority()),
        # EventKit has no API for the flag; callers read it from the Reminders database.
        "flagged": flagged,
        "list_id": calendar.calendarIdentifier(),
        "list_name": calendar.title(),
        "list_path": _list_path(calendar),
    }
    # The remaining fields are rare, so a row only carries the ones that are
    # set; search returns many rows and every key costs prompt tokens.
    extras = extras or ReminderExtras()
    rules = reminder.recurrenceRules() or []
    optional = {
        "all_day": due is not None and is_date_only(due),
        "recurrence": (
            {"frequency": _RECURRENCE_NAMES[rules[0].frequency()], "interval": int(rules[0].interval())} if rules else None
        ),
        "location": _location_row(reminder),
        "early_reminders": extras.early_reminders,
        "urgent": extras.urgent,
        "tags": extras.tags,
        "url": extras.url,
        "image_count": extras.image_count,
        "parent_id": extras.parent_id,
        "subtask_ids": extras.subtask_ids,
    }
    row.update({key: value for key, value in optional.items() if value})
    return row


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


def _set_due(reminder: Any, due: datetime | date) -> None:
    old_due = reminder.dueDateComponents()
    old_date = components_to_nsdate(old_due) if old_due is not None else None
    timed = isinstance(due, datetime)
    components = datetime_to_components(due) if timed else date_to_components(due)
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
    # An all-day reminder has no due time to alarm at; Reminders.app notifies
    # for it at the user's all-day notification time.
    if timed:
        # Derive the alarm from the stored components so the next update finds
        # it by exact date even when due carried sub-second precision.
        alarms.append(EventKit.EKAlarm.alarmWithAbsoluteDate_(components_to_nsdate(components)))
    reminder.setAlarms_(alarms)


def _set_recurrence(reminder: Any, recurrence: str | None, interval: int) -> None:
    """Replace the recurrence rules; None removes them."""
    rules = []
    if recurrence is not None:
        rules.append(
            EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
                _RECURRENCE_FREQUENCIES[recurrence], interval, None
            )
        )
    reminder.setRecurrenceRules_(rules)


def _set_location(reminder: Any, location: ReminderLocation | None) -> None:
    """Replace the location alarm; None removes it. Time alarms stay."""
    alarms = [alarm for alarm in reminder.alarms() or [] if alarm.structuredLocation() is None]
    if location is not None:
        place = EventKit.EKStructuredLocation.locationWithTitle_(location.title)
        place.setGeoLocation_(
            objc.lookUpClass("CLLocation").alloc().initWithLatitude_longitude_(location.latitude, location.longitude)
        )
        place.setRadius_(location.radius_m)
        alarm = EventKit.EKAlarm.alarmWithRelativeOffset_(0)
        alarm.setStructuredLocation_(place)
        alarm.setProximity_(
            EventKit.EKAlarmProximityLeave if location.trigger == "leave" else EventKit.EKAlarmProximityEnter
        )
        alarms.append(alarm)
    reminder.setAlarms_(alarms)


def _readback_problems(private: dict[str, Any], before: ReminderExtras, extras: ReminderExtras | None) -> list[str]:
    """Compare the fields just written through ReminderKit with the database.

    before is the reminder's state ahead of the write, for the appended fields.
    """
    if extras is None:
        return ["the reminder could not be read back from the Reminders database"]
    problems = []
    added_subtasks = len(extras.subtask_ids) - len(before.subtask_ids)
    if added_subtasks != len(private["subtasks"] or []):
        problems.append(f"{added_subtasks} of {len(private['subtasks'] or [])} subtasks read back")
    added_images = extras.image_count - before.image_count
    if added_images != len(private["images"] or []):
        problems.append(f"{added_images} of {len(private['images'] or [])} images read back")
    if private["urgent"] is not None and extras.urgent != private["urgent"]:
        problems.append(f"urgent reads back as {extras.urgent}")
    if private["tags"] is not None and sorted(map(str.lower, extras.tags)) != sorted(map(str.lower, private["tags"])):
        problems.append(f"tags read back as {extras.tags}")
    # NSURL percent-encodes and punycodes what it stores, so compare in that form.
    expected_url = str(NSURL.URLWithString_(private["url"]).absoluteString()) if private["url"] else ""
    if private["url"] is not None and (extras.url or "") != expected_url:
        problems.append(f"url reads back as {extras.url!r}")
    if private["early_reminder"] is not None:
        expected = [] if private["early_reminder"] == "none" else [private["early_reminder"]]
        if extras.early_reminders != expected:
            problems.append(f"early_reminder reads back as {extras.early_reminders}")
    return problems


def _field_problem(
    *,
    creating: bool,
    due: datetime | date | None,
    early_reminder: str | None,
    recurrence: str | None,
    recurrence_interval: int | None,
    urgent: bool | None,
    tags: list[str] | None,
    url: str | None,
    location: ReminderLocation | None,
    subtasks: list[str] | None,
    images: list[str] | None,
    clear: list[str] | None,
) -> str | None:
    """Return why the field values cannot be written, before anything is saved."""
    if early_reminder is not None:
        try:
            parse_early_reminder(early_reminder)
        except ValueError as exc:
            return str(exc)
    if recurrence is not None and recurrence not in _RECURRENCE_FREQUENCIES:
        return f"invalid recurrence {recurrence!r}: use one of {', '.join(_RECURRENCE_FREQUENCIES)}"
    if recurrence_interval is not None and (recurrence is None or recurrence_interval < 1):
        return "'recurrence_interval' must be 1 or more and needs 'recurrence'"
    if tags is not None and (not tags or any(not tag or tag.startswith("#") or tag != "".join(tag.split()) for tag in tags)):
        return "'tags' needs at least one tag, each without '#' or spaces; use clear=['tags'] to remove tags"
    # NSURL is what ReminderKit stores, and it refuses some strings urlsplit accepts.
    if url is not None and (not urlsplit(url).scheme or NSURL.URLWithString_(url) is None):
        return f"invalid url {url!r}: include the scheme, e.g. https://; use clear=['url'] to remove it"
    if any(not title.strip() for title in subtasks or []):
        return "'subtasks' titles must not be empty"
    if any(not image.strip() for image in images or []):
        return "'images' paths must not be empty"
    given = {"early_reminder": early_reminder, "recurrence": recurrence, "location": location, "url": url, "tags": tags}
    for name in clear or []:
        if name not in _CLEARABLE_FIELDS:
            return f"cannot clear {name!r}: use one of {', '.join(_CLEARABLE_FIELDS)}"
        if given[name] is not None:
            return f"{name!r} is both set and cleared"
    if creating:
        if clear:
            return "'clear' only applies to update"
        if early_reminder is not None and due is None:
            return "'early_reminder' needs 'due'"
        if recurrence is not None and due is None:
            return "'recurrence' needs 'due'"
        if urgent and not isinstance(due, datetime):
            return "'urgent' needs 'due' with a time"
    return None


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

    def _reminder_image_paths(self, images: list[str] | None) -> list[Path] | None:
        """Resolve image attachments before anything is saved."""
        paths = []
        for image in images or []:
            path = resolve_allowed_path(image, self._allowed_paths, self._base_dir)
            if path is None or not path.is_file():
                raise ValueError(f"image is not a readable file inside allowed paths: {image}")
            image_size(path)
            paths.append(path)
        return paths or None

    def _finish_reminder_write(
        self,
        reminder: Any,
        *,
        created: bool,
        flagged: bool | None,
        private: dict[str, Any],
    ) -> dict[str, Any]:
        """Write the flag and the ReminderKit fields of a reminder EventKit has saved.

        The save is committed, so a failure here is a warning on a successful
        result: an error would make the agent retry a create and duplicate the
        reminder. private holds the write_private_fields arguments.
        """
        identifier = reminder.calendarItemIdentifier()
        warnings = []
        row_flagged = False if created else read_flags().get(identifier)
        if flagged is not None:
            try:
                self._set_reminder_flagged(identifier, flagged)
                # Report the value just written rather than re-reading the
                # database, which can trail the Reminders.app write.
                row_flagged = flagged
            except RuntimeError as exc:
                # _run_jxa_json has already logged the failure.
                warnings.append(f"flagged={flagged} was not applied: {exc}")
                row_flagged = read_flags().get(identifier)
        extras = None if created else read_extras().get(identifier)
        if any(value is not None for value in private.values()):
            before = extras or ReminderExtras()
            try:
                write_private_fields(identifier, early_reminder_ids=before.early_reminder_ids, **private)
                extras = read_extras().get(identifier)
                warnings.extend(_readback_problems(private, before, extras))
            # Broad on purpose: besides RuntimeError from the write, a changed
            # private API surfaces as objc.error or AttributeError and the
            # readback can fail in sqlite; none of them may turn a committed
            # save into a tool error.
            except Exception as exc:
                names = ", ".join(name for name, value in private.items() if value is not None)
                warnings.append(f"{names} may not be applied: {exc}")
        result = {"ok": True, "reminder": _reminder_row(reminder, row_flagged, extras)}
        if warnings:
            result["warning"] = "reminder saved, but " + "; ".join(warnings)
        return result

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
            extras = read_extras()
            needle = (query or "").lower()
            results = []
            for reminder in fetched:
                identifier = reminder.calendarItemIdentifier()
                row = _reminder_row(reminder, flags.get(identifier), extras.get(identifier))
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
            identifier = reminder.calendarItemIdentifier()
            return {"ok": True, "reminder": _reminder_row(reminder, read_flags().get(identifier), read_extras().get(identifier))}

    def reminders_create(
        self,
        *,
        list_id: str | None,
        list_name: str | None,
        list_path: str | None,
        title: str,
        notes: str | None,
        due: datetime | date | None,
        priority: int | None,
        flagged: bool | None,
        early_reminder: str | None = None,
        recurrence: str | None = None,
        recurrence_interval: int | None = None,
        urgent: bool | None = None,
        tags: list[str] | None = None,
        url: str | None = None,
        location: ReminderLocation | None = None,
        subtasks: list[str] | None = None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a reminder."""
        image_paths = self._reminder_image_paths(images)
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
            if recurrence is not None:
                _set_recurrence(reminder, recurrence, recurrence_interval or 1)
            if location is not None:
                _set_location(reminder, location)
            saved, error = store.saveReminder_commit_error_(reminder, True, None)
            if not saved:
                return save_error(error)
            return self._finish_reminder_write(
                reminder,
                created=True,
                flagged=True if flagged else None,
                private={
                    "urgent": True if urgent else None,
                    "tags": tags,
                    "url": url,
                    "early_reminder": early_reminder,
                    "subtasks": subtasks,
                    "images": image_paths,
                },
            )

    def reminders_update(
        self,
        *,
        reminder_id: str,
        title: str | None,
        notes: str | None,
        due: datetime | date | None,
        priority: int | None,
        flagged: bool | None,
        completed: bool | None,
        early_reminder: str | None = None,
        recurrence: str | None = None,
        recurrence_interval: int | None = None,
        urgent: bool | None = None,
        tags: list[str] | None = None,
        url: str | None = None,
        location: ReminderLocation | None = None,
        subtasks: list[str] | None = None,
        images: list[str] | None = None,
        clear: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update a reminder."""
        image_paths = self._reminder_image_paths(images)
        clear = clear or []
        with open_store(EventKit.EKEntityTypeReminder) as store:
            reminder = _find_reminder(store, reminder_id)
            if reminder is None:
                return {"ok": False, "error": f"reminder not found: {reminder_id}"}
            # Checked against the due date the reminder will have after this
            # update, before any field is changed.
            current_due = reminder.dueDateComponents()
            has_due = due is not None or current_due is not None
            if due is not None:
                has_due_time = isinstance(due, datetime)
            else:
                has_due_time = current_due is not None and not is_date_only(current_due)
            if early_reminder is not None and not has_due:
                return {"ok": False, "error": "'early_reminder' needs a due date; pass 'due' as well"}
            if recurrence is not None and not has_due:
                return {"ok": False, "error": "'recurrence' needs a due date; pass 'due' as well"}
            if urgent and not has_due_time:
                return {"ok": False, "error": "'urgent' needs a due date with a time; pass 'due' as well"}
            if due is not None and not has_due_time and urgent is None:
                if read_extras().get(reminder.calendarItemIdentifier(), ReminderExtras()).urgent:
                    return {
                        "ok": False,
                        "error": "an urgent reminder needs a due date with a time; pass urgent=false to make it all-day",
                    }
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
            if recurrence is not None or "recurrence" in clear:
                _set_recurrence(reminder, recurrence, recurrence_interval or 1)
            if location is not None or "location" in clear:
                _set_location(reminder, location)
            saved, error = store.saveReminder_commit_error_(reminder, True, None)
            if not saved:
                return save_error(error)
            return self._finish_reminder_write(
                reminder,
                created=False,
                flagged=flagged,
                private={
                    "urgent": urgent,
                    "tags": [] if "tags" in clear else tags,
                    "url": "" if "url" in clear else url,
                    "early_reminder": "none" if "early_reminder" in clear else early_reminder,
                    "subtasks": subtasks,
                    "images": image_paths,
                },
            )


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
        early_reminder: str | None = None,
        recurrence: str | None = None,
        recurrence_interval: int | None = None,
        urgent: bool | None = None,
        tags: list[str] | None = None,
        url: str | None = None,
        location: dict[str, Any] | None = None,
        subtasks: list[str] | None = None,
        images: list[str] | None = None,
        clear: list[str] | None = None,
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
            if action not in {"create", "update", "complete"}:
                return _error(f"unknown action '{action}'")
            if action == "create" and not title:
                return _error("'title' is required for create")
            if action != "create" and not reminder_id:
                return _error("'reminder_id' is required for update/complete")
            # A date without a time makes an all-day reminder.
            due_value = None
            if due:
                try:
                    due_value = date.fromisoformat(due)
                except ValueError:
                    due_value = _parse_local_datetime(due, field_name="due")
            try:
                place = ReminderLocation.model_validate(location) if location is not None else None
            except ValidationError as exc:
                details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                return _error(f"invalid location: {details}")
            problem = _field_problem(
                creating=action == "create",
                due=due_value,
                early_reminder=early_reminder,
                recurrence=recurrence,
                recurrence_interval=recurrence_interval,
                urgent=urgent,
                tags=tags,
                url=url,
                location=place,
                subtasks=subtasks,
                images=images,
                clear=clear,
            )
            if problem:
                return _error(problem)
            fields = {
                "early_reminder": early_reminder,
                "recurrence": recurrence,
                "recurrence_interval": recurrence_interval,
                "urgent": urgent,
                "tags": tags,
                "url": url,
                "location": place,
                "subtasks": subtasks,
                "images": images,
            }
            if action == "create":
                return _json_output(
                    bridge.reminders_create(
                        list_id=list_id,
                        list_name=list_name,
                        list_path=list_path,
                        title=title,
                        notes=notes,
                        due=due_value,
                        priority=priority,
                        flagged=flagged,
                        **fields,
                    )
                )
            if action == "complete" and completed is None:
                completed = True
            if (
                title is None
                and notes is None
                and due_value is None
                and priority is None
                and flagged is None
                and completed is None
                and not clear
                and all(value is None for value in fields.values())
            ):
                return _error("update requires at least one field to change")
            return _json_output(
                bridge.reminders_update(
                    reminder_id=reminder_id,
                    title=title,
                    notes=notes,
                    due=due_value,
                    priority=priority,
                    flagged=flagged,
                    completed=completed,
                    clear=clear,
                    **fields,
                )
            )


    return reminders_tool
