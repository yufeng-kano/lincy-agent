"""macOS Calendar tool backed by EventKit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from typing import Any

import AppKit
import EventKit
from Foundation import NSURL

from ....llm.schema import ToolDefinition, ToolParameter
from ....timezone_utils import now
from .eventkit import nsdate_to_iso, open_store, save_error, to_nsdate
from .runtime import (
    _datetime_in_app_tz,
    _datetime_to_app_iso,
    _error,
    _json_output,
    _parse_local_datetime,
)

# EventKit silently truncates event predicates to the first four years.
_MAX_SEARCH_SPAN = timedelta(days=4 * 365)

CALENDAR_TOOL_DEFINITION = ToolDefinition(
    name="calendar_tool",
    description=(
        "Access the user's real macOS Calendar data. "
        "'catalog' lists calendars, "
        "'search' searches events by calendar/date/query, "
        "'conflicts' finds events that overlap a candidate time range, "
        "'get' fetches a single event by uid, "
        "'create' creates a new event, "
        "'update' updates an existing event by uid."
    ),
    parameters={
        "action": ToolParameter(
            type="string",
            description="Action to perform.",
            enum=["catalog", "search", "conflicts", "get", "create", "update"],
        ),
        "calendar": ToolParameter(
            type="string",
            description="Exact calendar name. Required for create; optional for search/update.",
        ),
        "calendars": ToolParameter(
            type="array",
            description="Optional exact calendar names. Use to search/check conflicts across multiple calendars.",
            items={"type": "string"},
        ),
        "event_uid": ToolParameter(
            type="string",
            description="Calendar event uid. Required for update.",
        ),
        "exclude_event_uid": ToolParameter(
            type="string",
            description="Optional event uid to exclude from conflict checks.",
        ),
        "query": ToolParameter(
            type="string",
            description="Case-insensitive text query matched against title, notes, and location.",
        ),
        "title": ToolParameter(
            type="string",
            description="Event title. Required for create.",
        ),
        "notes": ToolParameter(
            type="string",
            description="Event notes/description.",
        ),
        "location": ToolParameter(
            type="string",
            description="Event location.",
        ),
        "url": ToolParameter(
            type="string",
            description="Optional URL attached to the event.",
        ),
        "start": ToolParameter(
            type="string",
            description="Local ISO datetime, e.g. '2026-04-20T14:00'. Required for create.",
        ),
        "end": ToolParameter(
            type="string",
            description="Local ISO datetime, e.g. '2026-04-20T15:00'. Required for create.",
        ),
        "all_day": ToolParameter(
            type="boolean",
            description="Optional all-day filter for search/conflicts, or the all-day value for create/update.",
        ),
        "sort_by": ToolParameter(
            type="string",
            description="Sort order for search/conflicts results.",
            enum=["start_asc", "start_desc"],
        ),
        "limit": ToolParameter(
            type="integer",
            description="Maximum number of search results to return.",
        ),
    },
    required=["action"],
)

def _calendar_path(calendar: Any) -> str:
    return f"{calendar.source().title()}/{calendar.title()}"


def _color_rgb(calendar: Any) -> list[float] | None:
    """Return the calendar color as [r, g, b] in 0..1, the shape JXA reported."""
    color = calendar.color()
    if color is None:
        return None
    rgb = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    return [rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent()]


def _event_row(event: Any) -> dict[str, Any]:
    calendar = event.calendar()
    url = event.URL()
    return {
        # The local item id is what JXA reported as uid, so stored uids keep working.
        "uid": event.calendarItemIdentifier(),
        "title": event.title(),
        "start": nsdate_to_iso(event.startDate()),
        "end": nsdate_to_iso(event.endDate()),
        "location": event.location(),
        "notes": event.notes(),
        "calendar": calendar.title(),
        "calendar_path": _calendar_path(calendar),
        "all_day": bool(event.isAllDay()),
        "url": url.absoluteString() if url else None,
        "recurring": bool(event.hasRecurrenceRules()),
    }


def _matching_calendars(store: Any, name: str) -> list[Any]:
    """Calendars whose title or 'source/title' path equals name."""
    matches = [
        calendar
        for calendar in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        if name in (calendar.title(), _calendar_path(calendar))
    ]
    if not matches:
        raise ValueError(f"calendar not found: {name}")
    return matches


def _selected_calendars(store: Any, calendar: str | None, calendars: list[str] | None) -> list[Any] | None:
    """Resolve the calendar filter; None means every calendar."""
    names = calendars or ([calendar] if calendar else [])
    if not names:
        return None
    return [match for name in names for match in _matching_calendars(store, name)]


def _find_event(store: Any, event_uid: str, calendar: str | None) -> Any:
    """Return the event with this uid; for a recurring event that is the series."""
    item = store.calendarItemWithIdentifier_(event_uid)
    if not isinstance(item, EventKit.EKEvent):
        return None
    if calendar is not None and calendar not in (item.calendar().title(), _calendar_path(item.calendar())):
        return None
    return item


def _all_day_end(start: datetime, end: datetime) -> datetime:
    """Read an all-day end at midnight as exclusive, as Calendar.app scripting did.

    EventKit treats it as one more whole day, so 'Oct 31 00:00 -> Nov 1 00:00'
    would become a two-day event.
    """
    end = _datetime_in_app_tz(end)
    if end.time() == time(0) and end > _datetime_in_app_tz(start):
        return end - timedelta(seconds=1)
    return end


def _sorted_events(events: Any, *, descending: bool = False) -> list[Any]:
    return sorted(events or [], key=lambda event: event.startDate().timeIntervalSince1970(), reverse=descending)


class MacOSAppBridge:

    def calendar_catalog(self) -> dict[str, Any]:
        """List calendars."""
        store = open_store(EventKit.EKEntityTypeEvent)
        calendars = [
            {
                "id": calendar.calendarIdentifier(),
                "name": calendar.title(),
                "path": _calendar_path(calendar),
                "source": calendar.source().title(),
                "writable": bool(calendar.allowsContentModifications()),
                # EventKit does not expose the calendar description.
                "description": None,
                "color": _color_rgb(calendar),
            }
            for calendar in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
        ]
        return {"ok": True, "calendars": calendars, "count": len(calendars)}

    def calendar_search(
        self,
        *,
        calendar: str | None,
        calendars: list[str] | None,
        query: str | None,
        start: str | None,
        end: str | None,
        all_day: bool | None,
        sort_by: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        """Search calendar events (recurring events appear as occurrences)."""
        start_dt = _datetime_in_app_tz(_parse_local_datetime(start, field_name="start")) if start else None
        end_dt = _datetime_in_app_tz(_parse_local_datetime(end, field_name="end")) if end else None
        # EventKit needs a bounded range; fill a missing side with the widest
        # span it will honor.
        if start_dt is None and end_dt is None:
            start_dt = now() - _MAX_SEARCH_SPAN / 2
            end_dt = start_dt + _MAX_SEARCH_SPAN
        elif start_dt is None:
            start_dt = end_dt - _MAX_SEARCH_SPAN
        elif end_dt is None:
            end_dt = start_dt + _MAX_SEARCH_SPAN
        store = open_store(EventKit.EKEntityTypeEvent)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            to_nsdate(start_dt), to_nsdate(end_dt), _selected_calendars(store, calendar, calendars)
        )
        needle = (query or "").lower()
        results = []
        for event in _sorted_events(store.eventsMatchingPredicate_(predicate), descending=sort_by == "start_desc"):
            if all_day is not None and bool(event.isAllDay()) != all_day:
                continue
            row = _event_row(event)
            if needle and needle not in f"{row['title'] or ''}\n{row['location'] or ''}\n{row['notes'] or ''}".lower():
                continue
            results.append(row)
        limit = max(1, min(limit or self._max_search_results, self._max_search_results))
        truncated = len(results) > limit
        results = results[:limit]
        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "limit": limit,
            "truncated": truncated,
            "warning": f"results hit limit {limit}; narrow the date range or increase limit" if truncated else None,
            "searched_range": {"start": _datetime_to_app_iso(start_dt), "end": _datetime_to_app_iso(end_dt)},
        }

    def calendar_conflicts(
        self,
        *,
        calendar: str | None,
        calendars: list[str] | None,
        start: str,
        end: str,
        exclude_event_uid: str | None,
        all_day: bool | None,
        limit: int | None,
    ) -> dict[str, Any]:
        """Find calendar events overlapping a candidate time range."""
        start_dt = _parse_local_datetime(start, field_name="start")
        end_dt = _parse_local_datetime(end, field_name="end")
        store = open_store(EventKit.EKEntityTypeEvent)
        start_ns = to_nsdate(start_dt)
        end_ns = to_nsdate(end_dt)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            start_ns, end_ns, _selected_calendars(store, calendar, calendars)
        )
        limit = max(1, min(limit or self._max_search_results, self._max_search_results))
        results = []
        for event in _sorted_events(store.eventsMatchingPredicate_(predicate)):
            # Strict overlap: back-to-back events that only touch the range are
            # not conflicts.
            if event.endDate().compare_(start_ns) != 1 or event.startDate().compare_(end_ns) != -1:
                continue
            if exclude_event_uid and event.calendarItemIdentifier() == exclude_event_uid:
                continue
            if all_day is not None and bool(event.isAllDay()) != all_day:
                continue
            results.append(_event_row(event))
            if len(results) >= limit:
                break
        return {
            "ok": True,
            "requested_range": {"start": _datetime_to_app_iso(start_dt), "end": _datetime_to_app_iso(end_dt)},
            "conflicts": results,
            "count": len(results),
        }

    def calendar_get(
        self,
        *,
        event_uid: str,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one calendar event by uid."""
        event = _find_event(open_store(EventKit.EKEntityTypeEvent), event_uid, calendar)
        if event is None:
            return {"ok": False, "error": f"event not found: {event_uid}"}
        return {"ok": True, "event": _event_row(event)}

    def calendar_create(
        self,
        *,
        calendar: str,
        title: str,
        start: datetime,
        end: datetime,
        notes: str | None,
        location: str | None,
        url: str | None,
        all_day: bool | None,
    ) -> dict[str, Any]:
        """Create a calendar event."""
        store = open_store(EventKit.EKEntityTypeEvent)
        matches = _matching_calendars(store, calendar)
        writable = [match for match in matches if match.allowsContentModifications()]
        if len(writable) != 1:
            # Several accounts can share a calendar name (e.g. iCloud and Google
            # both have one); writing into a guessed one is worse than asking.
            paths = ", ".join(_calendar_path(match) for match in matches)
            reason = "is ambiguous" if writable else "is not writable"
            return {"ok": False, "error": f"calendar {calendar!r} {reason}: {paths}; pass one writable path from catalog"}
        event = EventKit.EKEvent.eventWithEventStore_(store)
        event.setCalendar_(writable[0])
        event.setTitle_(title)
        event.setAllDay_(bool(all_day))
        event.setStartDate_(to_nsdate(start))
        event.setEndDate_(to_nsdate(_all_day_end(start, end) if all_day else end))
        if notes:
            event.setNotes_(notes)
        if location:
            event.setLocation_(location)
        if url:
            event.setURL_(NSURL.URLWithString_(url))
        saved, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None)
        if not saved:
            return save_error(error)
        return {"ok": True, "event": _event_row(event)}

    def calendar_update(
        self,
        *,
        event_uid: str,
        calendar: str | None,
        title: str | None,
        start: datetime | None,
        end: datetime | None,
        notes: str | None,
        location: str | None,
        url: str | None,
        all_day: bool | None,
    ) -> dict[str, Any]:
        """Update a calendar event; for a recurring event this edits the whole series."""
        store = open_store(EventKit.EKEntityTypeEvent)
        event = _find_event(store, event_uid, calendar)
        if event is None:
            return {"ok": False, "error": f"event not found: {event_uid}"}
        if event.hasRecurrenceRules() and (start is not None or end is not None or all_day is not None):
            # The uid names the series, not an occurrence; moving the series
            # master would silently shift or drop every occurrence.
            return {
                "ok": False,
                "error": (
                    f"event {event_uid} is recurring; changing its time would move the whole series. "
                    "Only title/notes/location/url can be updated; ask the user to reschedule it in Calendar.app"
                ),
            }
        if title is not None:
            event.setTitle_(title)
        if notes is not None:
            event.setNotes_(notes)
        if location is not None:
            event.setLocation_(location)
        if url is not None:
            event.setURL_(NSURL.URLWithString_(url) if url else None)
        if all_day is not None:
            event.setAllDay_(all_day)
        if start is not None:
            event.setStartDate_(to_nsdate(start))
        if end is not None:
            if event.isAllDay():
                current_start = datetime.fromtimestamp(event.startDate().timeIntervalSince1970(), timezone.utc)
                end = _all_day_end(current_start, end)
            event.setEndDate_(to_nsdate(end))
        # The uid stands for the whole series (as with JXA), so text edits apply
        # to every future occurrence from the series start.
        saved, error = store.saveEvent_span_commit_error_(event, EventKit.EKSpanFutureEvents, True, None)
        if not saved:
            return save_error(error)
        return {"ok": True, "event": _event_row(event)}


def create_calendar_tool(bridge: MacOSAppBridge) -> Callable[..., str]:
    """Create calendar_tool bound to the bridge."""

    def calendar_tool(
        action: str,
        calendar: str | None = None,
        calendars: list[str] | None = None,
        event_uid: str | None = None,
        exclude_event_uid: str | None = None,
        query: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        location: str | None = None,
        url: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        sort_by: str | None = None,
        limit: int | None = None,
    ) -> str:

            if action == "catalog":
                return _json_output(bridge.calendar_catalog())
            if action == "search":
                return _json_output(
                    bridge.calendar_search(
                        calendar=calendar,
                        calendars=calendars,
                        query=query,
                        start=start,
                        end=end,
                        all_day=all_day,
                        sort_by=sort_by,
                        limit=limit,
                    )
                )
            if action == "conflicts":
                if not start or not end:
                    return _error("'start' and 'end' are required for conflicts")
                start_dt = _parse_local_datetime(start, field_name="start")
                end_dt = _parse_local_datetime(end, field_name="end")
                if _datetime_in_app_tz(end_dt) < _datetime_in_app_tz(start_dt):
                    return _error("'end' must be after or equal to 'start'")
                return _json_output(
                    bridge.calendar_conflicts(
                        calendar=calendar,
                        calendars=calendars,
                        start=start,
                        end=end,
                        exclude_event_uid=exclude_event_uid,
                        all_day=all_day,
                        limit=limit,
                    )
                )
            if action == "get":
                if not event_uid:
                    return _error("'event_uid' is required for get")
                return _json_output(
                    bridge.calendar_get(event_uid=event_uid, calendar=calendar)
                )
            if action == "create":
                if not calendar:
                    return _error("'calendar' is required for create")
                if not title:
                    return _error("'title' is required for create")
                if not start or not end:
                    return _error("'start' and 'end' are required for create")
                start_dt = _parse_local_datetime(start, field_name="start")
                end_dt = _parse_local_datetime(end, field_name="end")
                if _datetime_in_app_tz(end_dt) < _datetime_in_app_tz(start_dt):
                    return _error("'end' must be after or equal to 'start'")
                return _json_output(
                    bridge.calendar_create(
                        calendar=calendar,
                        title=title,
                        start=start_dt,
                        end=end_dt,
                        notes=notes,
                        location=location,
                        url=url,
                        all_day=all_day,
                    )
                )
            if action == "update":
                if not event_uid:
                    return _error("'event_uid' is required for update")
                start_dt = _parse_local_datetime(start, field_name="start") if start else None
                end_dt = _parse_local_datetime(end, field_name="end") if end else None
                if (
                    start_dt is not None
                    and end_dt is not None
                    and _datetime_in_app_tz(end_dt) < _datetime_in_app_tz(start_dt)
                ):
                    return _error("'end' must be after or equal to 'start'")
                if (
                    title is None
                    and notes is None
                    and location is None
                    and url is None
                    and start_dt is None
                    and end_dt is None
                    and all_day is None
                ):
                    return _error("update requires at least one field to change")
                return _json_output(
                    bridge.calendar_update(
                        event_uid=event_uid,
                        calendar=calendar,
                        title=title,
                        start=start_dt,
                        end=end_dt,
                        notes=notes,
                        location=location,
                        url=url,
                        all_day=all_day,
                    )
                )
            return _error(f"unknown action '{action}'")


    return calendar_tool
