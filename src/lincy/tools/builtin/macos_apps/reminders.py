"""macOS app tool implementation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ....llm.schema import ToolDefinition, ToolParameter
from .runtime import (
    _datetime_to_app_iso,
    _error,
    _json_output,
    _localize_reminder_datetime_fields,
    _parse_calendar_payload_datetime,
    _parse_local_datetime,
    build_partial_update_payload,
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

class MacOSAppBridge:

    def reminders_catalog(self) -> dict[str, Any]:
        """List reminder accounts and lists."""
        script = """
const app = Application("Reminders");
const accounts = app.accounts().map((account) => ({
  id: account.id(),
  name: account.name(),
  lists: account.lists().map((list) => ({
    id: list.id(),
    name: list.name(),
    account: account.name(),
  })),
}));
return { ok: true, accounts, count: accounts.reduce((n, account) => n + account.lists.length, 0) };
"""
        return self._run_jxa_json(script)

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
        due_start = _parse_calendar_payload_datetime(due_start, field_name="due_start")
        due_end = _parse_calendar_payload_datetime(due_end, field_name="due_end")
        script = f"""
const app = Application("Reminders");
const payload = readPayload();
const limit = clampLimit(payload.limit, {self._max_search_results});
const query = lower(payload.query || "");
const dueStart = payload.due_start ? new Date(payload.due_start) : null;
const dueEnd = payload.due_end ? new Date(payload.due_end) : null;
let lists = [];
if (payload.list_id) {{
  lists = app.lists.whose({{ id: payload.list_id }})();
}} else if (payload.list_path) {{
  for (const account of app.accounts()) {{
    for (const list of account.lists()) {{
      const path = `${{account.name()}}/${{list.name()}}`;
      if (path === payload.list_path) {{
        lists = [list];
        break;
      }}
    }}
    if (lists.length > 0) {{
      break;
    }}
  }}
}} else if (payload.list_name) {{
  lists = [app.lists.byName(payload.list_name)];
}} else {{
  lists = app.lists();
}}
const results = [];
for (const list of lists) {{
  if (!list.exists()) {{
    continue;
  }}
  for (const reminder of list.reminders()) {{
    const row = {{
      id: reminder.id(),
      title: reminder.name(),
      notes: valueOrNull(reminder.body()),
      completed: !!reminder.completed(),
      due: iso(reminder.dueDate()),
      priority: reminder.priority(),
      flagged: !!reminder.flagged(),
      list_id: list.id(),
      list_name: list.name(),
      list_path: `${{list.container().name()}}/${{list.name()}}`,
    }};
    if (payload.completed !== null && payload.completed !== undefined && row.completed !== payload.completed) {{
      continue;
    }}
    if (payload.flagged !== null && payload.flagged !== undefined && row.flagged !== payload.flagged) {{
      continue;
    }}
    if (payload.priority_min !== null && payload.priority_min !== undefined && row.priority < payload.priority_min) {{
      continue;
    }}
    if (payload.priority_max !== null && payload.priority_max !== undefined && row.priority > payload.priority_max) {{
      continue;
    }}
    if (dueStart && (!row.due || new Date(row.due) < dueStart)) {{
      continue;
    }}
    if (dueEnd && (!row.due || new Date(row.due) > dueEnd)) {{
      continue;
    }}
    const haystack = lower(`${{row.title || ""}}\\n${{row.notes || ""}}`);
    if (query && !haystack.includes(query)) {{
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
if (payload.sort_by === "due_desc") {{
  results.sort((a, b) => compareIsoDesc(a.due, b.due));
}} else if (payload.sort_by === "title_asc") {{
  results.sort((a, b) => compareTextAsc(a.title, b.title));
}} else {{
  results.sort((a, b) => compareIsoAsc(a.due, b.due));
}}
return {{ ok: true, results, count: results.length }};
"""
        result = self._run_jxa_json(
            script,
            payload={
                "list_id": list_id,
                "list_name": list_name,
                "list_path": list_path,
                "query": query,
                "due_start": due_start,
                "due_end": due_end,
                "completed": completed,
                "flagged": flagged,
                "priority_min": priority_min,
                "priority_max": priority_max,
                "sort_by": sort_by,
                "limit": limit,
            },
        )
        return _localize_reminder_datetime_fields(result)

    def reminders_get(self, *, reminder_id: str) -> dict[str, Any]:
        """Fetch one reminder by id."""
        result = self._run_jxa_json(
            """
const app = Application("Reminders");
const payload = readPayload();
const matches = app.reminders.whose({ id: payload.reminder_id })();
if (matches.length === 0) {
  return { ok: false, error: `reminder not found: ${payload.reminder_id}` };
}
const reminder = matches[0];
const list = reminder.container();
return {
  ok: true,
  reminder: {
    id: reminder.id(),
    title: reminder.name(),
    notes: valueOrNull(reminder.body()),
    completed: !!reminder.completed(),
    due: iso(reminder.dueDate()),
    priority: reminder.priority(),
      flagged: !!reminder.flagged(),
      list_id: list.id(),
      list_name: list.name(),
      list_path: `${list.container().name()}/${list.name()}`,
    },
};
""",
            payload={"reminder_id": reminder_id},
        )
        return _localize_reminder_datetime_fields(result)

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
        resolved = self._resolve_list_spec(
            list_id=list_id,
            list_name=list_name,
            list_path=list_path,
        )
        if not resolved.get("ok"):
            return resolved
        result = self._run_jxa_json(
            """
const app = Application("Reminders");
const payload = readPayload();
const matches = app.lists.whose({ id: payload.list_id })();
if (matches.length === 0) {
  return { ok: false, error: `reminders list not found: ${payload.list_id}` };
}
const list = matches[0];
const properties = { name: payload.title };
if (payload.notes) {
  properties.body = payload.notes;
}
if (payload.has_priority) {
  properties.priority = payload.priority;
}
if (payload.has_flagged) {
  properties.flagged = !!payload.flagged;
}
if (payload.due) {
  const dueDate = new Date(payload.due);
  if (Number.isNaN(dueDate.getTime())) {
    return { ok: false, error: `invalid due: ${payload.due}` };
  }
  properties.dueDate = dueDate;
}
const newReminder = app.Reminder(properties);
list.reminders.push(newReminder);
return { ok: true, reminder_id: newReminder.id() };
""",
            payload={
                "list_id": resolved["list_id"],
                "title": title,
                "notes": notes,
                "has_priority": priority is not None,
                "priority": priority or 0,
                "has_flagged": flagged is not None,
                "flagged": bool(flagged),
                "due": _datetime_to_app_iso(due) if due is not None else None,
            },
        )
        if not result.get("ok"):
            return result
        return self.reminders_get(reminder_id=result["reminder_id"])

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
        result = self._run_jxa_json(
            """
const app = Application("Reminders");
const payload = readPayload();
const matches = app.reminders.whose({ id: payload.reminder_id })();
if (matches.length === 0) {
  return { ok: false, error: `reminder not found: ${payload.reminder_id}` };
}
const reminder = matches[0];
if (payload.due) {
  const dueDate = new Date(payload.due);
  if (Number.isNaN(dueDate.getTime())) {
    return { ok: false, error: `invalid due: ${payload.due}` };
  }
  reminder.dueDate.set(dueDate);
}
if (payload.has_title) {
  reminder.name.set(payload.title || "");
}
if (payload.has_notes) {
  reminder.body.set(payload.notes || "");
}
if (payload.has_priority) {
  reminder.priority.set(payload.priority);
}
if (payload.has_flagged) {
  reminder.flagged.set(!!payload.flagged);
}
if (payload.has_completed) {
  reminder.completed.set(!!payload.completed);
}
return { ok: true, reminder_id: reminder.id() };
""",
            payload={
                "reminder_id": reminder_id,
                **build_partial_update_payload(
                    {
                        "title": title,
                        "notes": notes,
                        "priority": priority,
                        "flagged": flagged,
                        "completed": completed,
                        "due": _datetime_to_app_iso(due) if due is not None else None,
                    }
                ),
            },
        )
        if not result.get("ok"):
            return result
        return self.reminders_get(reminder_id=result["reminder_id"])

    def _resolve_list_spec(
        self,
        *,
        list_id: str | None,
        list_name: str | None,
        list_path: str | None,
    ) -> dict[str, Any]:
        """Resolve a reminders list."""
        result = self._run_jxa_json(
            """
const app = Application("Reminders");
const payload = readPayload();
let list = null;
if (payload.list_id) {
  const matches = app.lists.whose({ id: payload.list_id })();
  if (matches.length > 0) {
    list = matches[0];
  }
} else if (payload.list_name) {
  list = app.lists.byName(payload.list_name);
  if (!list.exists()) {
    list = null;
  }
} else if (payload.list_path) {
  for (const account of app.accounts()) {
    for (const candidate of account.lists()) {
      const path = `${account.name()}/${candidate.name()}`;
      if (path === payload.list_path) {
        list = candidate;
        break;
      }
    }
    if (list) {
      break;
    }
  }
} else {
  list = app.defaultList();
}
if (!list) {
  return { ok: false, error: "reminders list not found" };
}
return {
  ok: true,
  list_id: list.id(),
  list_name: list.name(),
  list_path: `${list.container().name()}/${list.name()}`,
};
""",
            payload={
                "list_id": list_id,
                "list_name": list_name,
                "list_path": list_path,
            },
        )
        return result


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
