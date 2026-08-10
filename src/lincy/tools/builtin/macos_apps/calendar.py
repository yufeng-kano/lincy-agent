"""macOS app tool implementation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from .runtime import *

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

CALENDAR_JXA_HELPERS = """
function eventRow(event, calendarName) {
  return {
    uid: event.uid(),
    title: event.summary(),
    start: iso(event.startDate()),
    end: iso(event.endDate()),
    location: valueOrNull(event.location()),
    notes: valueOrNull(event.description()),
    calendar: calendarName,
    all_day: !!event.alldayEvent(),
    url: valueOrNull(event.url()),
  };
}
function selectedCalendars(app, payload) {
  if (payload.calendars && payload.calendars.length > 0) {
    return payload.calendars.map((name) => app.calendars.byName(name));
  }
  if (payload.calendar) {
    return [app.calendars.byName(payload.calendar)];
  }
  return app.calendars();
}
"""


class MacOSAppBridge:

    def _run_calendar_jxa_json(self, body: str, **kwargs: Any) -> dict[str, Any]:
        """Run Calendar JXA with its app-specific helper prelude."""
        return self._run_jxa_json(body, helpers=CALENDAR_JXA_HELPERS, **kwargs)

    def calendar_catalog(self) -> dict[str, Any]:
        """List calendars."""
        script = """
const app = Application("Calendar");
const calendars = app.calendars().map((cal) => ({
  name: cal.name(),
  writable: !!cal.writable(),
  description: valueOrNull(cal.description()),
  color: valueOrNull(cal.color()),
}));
return { ok: true, calendars, count: calendars.length };
"""
        return self._run_calendar_jxa_json(script)

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
        """Search calendar events."""
        start = _parse_calendar_payload_datetime(start, field_name="start")
        end = _parse_calendar_payload_datetime(end, field_name="end")
        script = f"""
const app = Application("Calendar");
const payload = readPayload();
const limit = clampLimit(payload.limit, {self._max_search_results});
const scanLimit = limit + 1;
const query = lower(payload.query || "");
const start = payload.start ? new Date(payload.start) : null;
const end = payload.end ? new Date(payload.end) : null;
const calendars = selectedCalendars(app, payload);
let results = [];
for (const cal of calendars) {{
  if (!cal.exists()) {{
    return {{ ok: false, error: `calendar not found: ${{payload.calendar || payload.calendars[0]}}` }};
  }}
  const dateFilter = {{}};
  if (start) {{
    dateFilter.endDate = {{ ">": start }};
  }}
  if (end) {{
    dateFilter.startDate = {{ "<": end }};
  }}
  const events = (start || end) ? cal.events.whose(dateFilter)() : cal.events();
  for (const event of events) {{
    const row = eventRow(event, cal.name());
    if (payload.all_day !== null && payload.all_day !== undefined && row.all_day !== payload.all_day) {{
      continue;
    }}
    if (start && row.end && new Date(row.end) < start) {{
      continue;
    }}
    if (end && row.start && new Date(row.start) > end) {{
      continue;
    }}
    const haystack = lower(`${{row.title || ""}}\\n${{row.location || ""}}\\n${{row.notes || ""}}`);
    if (query && !haystack.includes(query)) {{
      continue;
    }}
    results.push(row);
    if (results.length >= scanLimit) {{
      break;
    }}
  }}
  if (results.length >= scanLimit) {{
    break;
  }}
}}
if (payload.sort_by === "start_desc") {{
  results.sort((a, b) => compareIsoDesc(a.start, b.start));
}} else {{
  results.sort((a, b) => compareIsoAsc(a.start, b.start));
}}
const truncated = results.length > limit;
if (truncated) {{
  results = results.slice(0, limit);
}}
return {{
  ok: true,
  results,
  count: results.length,
  limit,
  truncated,
  warning: truncated ? `results hit limit ${{limit}}; narrow the date range or increase limit` : null,
}};
"""
        result = self._run_calendar_jxa_json(
            script,
            payload={
                "calendar": calendar,
                "calendars": calendars,
                "query": query,
                "start": start,
                "end": end,
                "all_day": all_day,
                "sort_by": sort_by,
                "limit": limit,
            },
        )
        return _localize_calendar_datetime_fields(result)

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
        start = _parse_calendar_payload_datetime(start, field_name="start") or start
        end = _parse_calendar_payload_datetime(end, field_name="end") or end
        script = f"""
const app = Application("Calendar");
const payload = readPayload();
const start = new Date(payload.start);
const end = new Date(payload.end);
const limit = clampLimit(payload.limit, {self._max_search_results});
const calendars = selectedCalendars(app, payload);
const results = [];
for (const cal of calendars) {{
  if (!cal.exists()) {{
    return {{ ok: false, error: `calendar not found: ${{payload.calendar || payload.calendars[0]}}` }};
  }}
  const matches = cal.events.whose({{ startDate: {{ "<": end }}, endDate: {{ ">": start }} }})();
  for (const event of matches) {{
    const row = eventRow(event, cal.name());
    if (payload.exclude_event_uid && row.uid === payload.exclude_event_uid) {{
      continue;
    }}
    if (payload.all_day !== null && payload.all_day !== undefined && row.all_day !== payload.all_day) {{
      continue;
    }}
    results.push(row);
    if (results.length >= limit) {{
      break;
    }}
  }}
  if (results.length >= limit) {{
    break;
  }}
}}
results.sort((a, b) => compareIsoAsc(a.start, b.start));
return {{
  ok: true,
  requested_range: {{ start: payload.start, end: payload.end }},
  conflicts: results,
  count: results.length,
}};
"""
        result = self._run_calendar_jxa_json(
            script,
            payload={
                "calendar": calendar,
                "calendars": calendars,
                "start": start,
                "end": end,
                "exclude_event_uid": exclude_event_uid,
                "all_day": all_day,
                "limit": limit,
            },
        )
        return _localize_calendar_datetime_fields(result)

    def calendar_get(
        self,
        *,
        event_uid: str,
        calendar: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one calendar event by uid."""
        result = self._run_calendar_jxa_json(
            """
const app = Application("Calendar");
const payload = readPayload();
const calendars = payload.calendar ? [app.calendars.byName(payload.calendar)] : app.calendars();
for (const cal of calendars) {
  if (!cal.exists()) {
    continue;
  }
  const matches = cal.events.whose({ uid: payload.event_uid })();
  if (matches.length > 0) {
    const event = matches[0];
    return {
      ok: true,
      event: eventRow(event, cal.name()),
    };
  }
}
return { ok: false, error: `event not found: ${payload.event_uid}` };
""",
            payload={"event_uid": event_uid, "calendar": calendar},
        )
        return _localize_calendar_datetime_fields(result)

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
        result = self._run_calendar_jxa_json(
            """
const app = Application("Calendar");
const payload = readPayload();
const calendar = app.calendars.byName(payload.calendar);
if (!calendar.exists()) {
  return { ok: false, error: `calendar not found: ${payload.calendar}` };
}
const startDate = new Date(payload.start);
const endDate = new Date(payload.end);
if (Number.isNaN(startDate.getTime())) {
  return { ok: false, error: `invalid start: ${payload.start}` };
}
if (Number.isNaN(endDate.getTime())) {
  return { ok: false, error: `invalid end: ${payload.end}` };
}
const properties = {
  summary: payload.title,
  startDate,
  endDate,
  alldayEvent: !!payload.all_day,
};
if (payload.notes) {
  properties.description = payload.notes;
}
if (payload.location) {
  properties.location = payload.location;
}
if (payload.url) {
  properties.url = payload.url;
}
const newEvent = app.Event(properties);
calendar.events.push(newEvent);
return { ok: true, uid: newEvent.uid() };
""",
            payload={
                "calendar": calendar,
                "title": title,
                "start": _datetime_to_app_iso(start),
                "end": _datetime_to_app_iso(end),
                "notes": notes,
                "location": location,
                "url": url,
                "all_day": all_day,
            },
        )
        if not result.get("ok"):
            return result
        uid = result["uid"]
        return self.calendar_get(event_uid=uid, calendar=calendar)

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
        """Update a calendar event."""
        target = self.calendar_get(event_uid=event_uid, calendar=calendar)
        if not target.get("ok"):
            return target
        target_calendar = target["event"]["calendar"]
        result = self._run_calendar_jxa_json(
            """
const app = Application("Calendar");
const payload = readPayload();
const calendar = app.calendars.byName(payload.calendar);
if (!calendar.exists()) {
  return { ok: false, error: `calendar not found: ${payload.calendar}` };
}
const matches = calendar.events.whose({ uid: payload.event_uid })();
if (matches.length === 0) {
  return { ok: false, error: `event not found: ${payload.event_uid}` };
}
const event = matches[0];
let startDate = null;
let endDate = null;
if (payload.has_start) {
  startDate = new Date(payload.start);
  if (Number.isNaN(startDate.getTime())) {
    return { ok: false, error: `invalid start: ${payload.start}` };
  }
}
if (payload.has_end) {
  endDate = new Date(payload.end);
  if (Number.isNaN(endDate.getTime())) {
    return { ok: false, error: `invalid end: ${payload.end}` };
  }
}
if (payload.has_title) {
  event.summary.set(payload.title || "");
}
if (payload.has_notes) {
  event.description.set(payload.notes || "");
}
if (payload.has_location) {
  event.location.set(payload.location || "");
}
if (payload.has_url) {
  event.url.set(payload.url || "");
}
if (payload.has_all_day) {
  event.alldayEvent.set(!!payload.all_day);
}
if (payload.has_start && payload.has_end) {
  const currentEnd = event.endDate();
  if (currentEnd && startDate <= currentEnd) {
    event.startDate.set(startDate);
    event.endDate.set(endDate);
  } else {
    event.endDate.set(endDate);
    event.startDate.set(startDate);
  }
} else {
  if (payload.has_end) {
    event.endDate.set(endDate);
  }
  if (payload.has_start) {
    event.startDate.set(startDate);
  }
}
return { ok: true, uid: event.uid() };
""",
            payload={
                "event_uid": event_uid,
                "calendar": target_calendar,
                **build_partial_update_payload(
                    {
                        "title": title,
                        "notes": notes,
                        "location": location,
                        "url": url,
                        "all_day": all_day,
                        "start": _datetime_to_app_iso(start) if start is not None else None,
                        "end": _datetime_to_app_iso(end) if end is not None else None,
                    }
                ),
            },
        )
        if not result.get("ok"):
            return result
        uid = result["uid"]
        return self.calendar_get(event_uid=uid, calendar=target_calendar)



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
