"""Shared EventKit plumbing for the Calendar and Reminders tools."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import EventKit
from Foundation import (
    NSCalendar,
    NSCalendarIdentifierGregorian,
    NSCalendarUnitDay,
    NSCalendarUnitEra,
    NSCalendarUnitHour,
    NSCalendarUnitMinute,
    NSCalendarUnitMonth,
    NSCalendarUnitSecond,
    NSCalendarUnitYear,
    NSDate,
    NSTimeZone,
)

from .reminders_db import read_flags
from .runtime import _datetime_in_app_tz, _datetime_to_app_iso

_STATUS_NAMES = {
    EventKit.EKAuthorizationStatusNotDetermined: "not determined",
    EventKit.EKAuthorizationStatusRestricted: "restricted",
    EventKit.EKAuthorizationStatusDenied: "denied",
    EventKit.EKAuthorizationStatusWriteOnly: "write-only",
}
_ENTITY_SETTINGS_PANES = {
    EventKit.EKEntityTypeEvent: "Calendars",
    EventKit.EKEntityTypeReminder: "Reminders",
}
_DUE_UNITS = (
    NSCalendarUnitEra
    | NSCalendarUnitYear
    | NSCalendarUnitMonth
    | NSCalendarUnitDay
    | NSCalendarUnitHour
    | NSCalendarUnitMinute
    | NSCalendarUnitSecond
)


def _permission_problem(entity_type: int) -> str | None:
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(entity_type)
    if status == EventKit.EKAuthorizationStatusFullAccess:
        return None
    pane = _ENTITY_SETTINGS_PANES[entity_type]
    return (
        f"{pane} full access is not granted (status: {_STATUS_NAMES.get(status, status)}). "
        f"Grant it in System Settings > Privacy & Security > {pane}, "
        "choosing Full Access for the app that runs lincy (e.g. Terminal)."
    )


def eventkit_permission_problems() -> list[str]:
    """Return human-readable permission problems for the Calendar and Reminders tools.

    Covers EventKit full access for events and reminders, plus Full Disk Access
    for the Reminders database that supplies the flagged state.
    """
    problems = [
        _permission_problem(EventKit.EKEntityTypeEvent),
        _permission_problem(EventKit.EKEntityTypeReminder),
    ]
    try:
        read_flags()
    except PermissionError as exc:
        problems.append(str(exc))
    return [problem for problem in problems if problem]


_stores: dict[int, Any] = {}


def open_store(entity_type: int) -> Any:
    """Return the process-wide EKEventStore for entity_type after checking full access.

    Measured on macOS 26.7.1: a fresh store per call breaks after ~9 predicate
    queries in one process (calendarItemWithIdentifier_ on new stores returns
    None), and a long-lived store without a run loop keeps serving stale copies
    of items edited by other processes. One store per entity type, reset before
    every use (~2ms), avoids both.
    """
    problem = _permission_problem(entity_type)
    if problem:
        raise PermissionError(problem)
    store = _stores.get(entity_type)
    if store is None:
        store = _stores[entity_type] = EventKit.EKEventStore.alloc().init()
    else:
        store.reset()
    return store


def save_error(error: Any) -> dict[str, Any]:
    """Convert an NSError from an EventKit save/remove into a tool error payload."""
    return {"ok": False, "error": str(error.localizedDescription())}


def to_nsdate(value: datetime) -> Any:
    """Convert a datetime (naive means app timezone) to NSDate."""
    return NSDate.dateWithTimeIntervalSince1970_(_datetime_in_app_tz(value).timestamp())


def nsdate_to_iso(value: Any) -> str | None:
    """Render an NSDate as app-local ISO text with offset."""
    if value is None:
        return None
    return _datetime_to_app_iso(datetime.fromtimestamp(value.timeIntervalSince1970(), timezone.utc))


def _gregorian(time_zone: Any) -> Any:
    calendar = NSCalendar.calendarWithIdentifier_(NSCalendarIdentifierGregorian)
    calendar.setTimeZone_(time_zone)
    return calendar


def datetime_to_components(value: datetime) -> Any:
    """Build due date components in the system timezone.

    Reminders.app stores JXA-written due dates this way (system zone attached),
    so EventKit writes stay indistinguishable from what the user's app creates.
    """
    time_zone = NSTimeZone.localTimeZone()
    components = _gregorian(time_zone).components_fromDate_(_DUE_UNITS, to_nsdate(value))
    components.setTimeZone_(time_zone)
    return components


def components_to_iso(components: Any) -> str | None:
    """Render due date components as app-local ISO text.

    Date-only (floating) components resolve to local midnight, matching what
    Reminders.app reported through JXA.
    """
    if components is None:
        return None
    calendar = _gregorian(components.timeZone() or NSTimeZone.localTimeZone())
    return nsdate_to_iso(calendar.dateFromComponents_(components))
