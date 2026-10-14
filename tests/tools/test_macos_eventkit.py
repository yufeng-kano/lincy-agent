from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import EventKit
from Foundation import NSDateComponents
import pytest

from lincy.tools.builtin.macos_apps import MacOSAppBridge, create_reminders_tool
from lincy.tools.builtin.macos_apps import calendar as calendar_module
from lincy.tools.builtin.macos_apps import eventkit
from lincy.tools.builtin.macos_apps import reminders as reminders_module
from lincy.tools.builtin.macos_apps import reminders_db


def _bridge(tmp_path: Path, *, timeout_seconds: float = 5) -> MacOSAppBridge:
    return MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=timeout_seconds,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )


class FakeCalendar:
    def __init__(self, title: str, *, source: str = "iCloud", ident: str = "cal-1", writable: bool = True):
        self._title = title
        self._source = SimpleNamespace(title=lambda: source)
        self._ident = ident
        self._writable = writable

    def title(self):
        return self._title

    def source(self):
        return self._source

    def calendarIdentifier(self):
        return self._ident

    def allowsContentModifications(self):
        return self._writable


class FakeReminder:
    def __init__(self, ident: str, title: str, *, due: datetime | None = None, priority: int = 0,
                 notes: str | None = None, completed: bool = False, calendar: FakeCalendar | None = None):
        self._ident = ident
        self._title = title
        self._due = eventkit.datetime_to_components(due) if due else None
        self._priority = priority
        self._notes = notes
        self._completed = completed
        self._calendar = calendar or FakeCalendar("Inbox", ident="list-1")

    def calendarItemIdentifier(self):
        return self._ident

    def title(self):
        return self._title

    def notes(self):
        return self._notes

    def isCompleted(self):
        return self._completed

    def dueDateComponents(self):
        return self._due

    def priority(self):
        return self._priority

    def calendar(self):
        return self._calendar


class FakeEvent:
    def __init__(self, uid: str, title: str, start: datetime, end: datetime, *, all_day: bool = False):
        self._uid = uid
        self._title = title
        self._start = eventkit.to_nsdate(start)
        self._end = eventkit.to_nsdate(end)
        self._all_day = all_day

    def calendarItemIdentifier(self):
        return self._uid

    def title(self):
        return self._title

    def startDate(self):
        return self._start

    def endDate(self):
        return self._end

    def location(self):
        return None

    def notes(self):
        return None

    def calendar(self):
        return FakeCalendar("Work")

    def isAllDay(self):
        return self._all_day

    def URL(self):
        return None

    def hasRecurrenceRules(self):
        return False


class FakeReminderStore:
    def __init__(self, reminders: list[FakeReminder], *, deliver: bool = True):
        self.reminders = reminders
        self.deliver = deliver
        self.predicates: list[tuple] = []
        self.calendars = [FakeCalendar("Inbox", ident="list-1"), FakeCalendar("Work", ident="list-2")]

    def calendarsForEntityType_(self, entity_type):
        return self.calendars

    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, start, end, calendars):
        self.predicates.append(("incomplete", start, end, calendars))
        return "predicate"

    def predicateForCompletedRemindersWithCompletionDateStarting_ending_calendars_(self, start, end, calendars):
        self.predicates.append(("completed", start, end, calendars))
        return "predicate"

    def predicateForRemindersInCalendars_(self, calendars):
        self.predicates.append(("all", calendars))
        return "predicate"

    def fetchRemindersMatchingPredicate_completion_(self, predicate, completion):
        if self.deliver:
            completion(self.reminders)


@pytest.fixture(autouse=True)
def _no_reminders_db(monkeypatch: pytest.MonkeyPatch):
    # Search/get read flags from the real Reminders database; tests opt in to fake flags.
    monkeypatch.setattr(reminders_module, "read_flags", dict)


def _search(bridge: MacOSAppBridge, **overrides):
    params = {
        "list_id": None, "list_name": None, "list_path": None, "query": None,
        "due_start": None, "due_end": None, "completed": None, "flagged": None,
        "priority_min": None, "priority_max": None, "sort_by": None, "limit": None,
    }
    params.update(overrides)
    return bridge.reminders_search(**params)


def test_due_components_round_trip_in_app_offset():
    assert eventkit.components_to_iso(eventkit.datetime_to_components(datetime(2026, 10, 21, 9, 30))) == (
        "2026-10-21T09:30:00+08:00"
    )
    aware = datetime.fromisoformat("2026-10-21T01:30:00+00:00")
    assert eventkit.components_to_iso(eventkit.datetime_to_components(aware)) == "2026-10-21T09:30:00+08:00"


def test_date_only_due_components_resolve_to_local_midnight():
    components = NSDateComponents.alloc().init()
    components.setYear_(2026)
    components.setMonth_(3)
    components.setDay_(21)
    expected = datetime(2026, 3, 21).astimezone()

    assert datetime.fromisoformat(eventkit.components_to_iso(components)) == expected


def test_nsdate_to_iso_renders_app_offset():
    assert eventkit.nsdate_to_iso(eventkit.to_nsdate(datetime(2026, 5, 6, 19, 0))) == "2026-05-06T19:00:00+08:00"
    assert eventkit.nsdate_to_iso(None) is None


def test_open_store_refuses_without_full_access(monkeypatch: pytest.MonkeyPatch):
    fake_eventkit = SimpleNamespace(
        EKEventStore=SimpleNamespace(authorizationStatusForEntityType_=lambda entity_type: 4),
        EKAuthorizationStatusFullAccess=3,
        EKEntityTypeEvent=EventKit.EKEntityTypeEvent,
        EKEntityTypeReminder=EventKit.EKEntityTypeReminder,
    )
    monkeypatch.setattr(eventkit, "EventKit", fake_eventkit)
    monkeypatch.setattr(reminders_db, "STORES_DIR", Path("/nonexistent"))

    with pytest.raises(PermissionError, match=r"Reminders full access is not granted \(status: write-only\)"):
        with eventkit.open_store(EventKit.EKEntityTypeReminder):
            pass
    problems = eventkit.eventkit_permission_problems()
    assert len(problems) == 3
    assert "Privacy & Security > Calendars" in problems[0]
    assert "Privacy & Security > Reminders" in problems[1]
    assert "Full Disk Access" in problems[2]


def test_find_reminder_accepts_jxa_url_form():
    reminder = EventKit.EKReminder.reminderWithEventStore_(EventKit.EKEventStore.alloc().init())
    asked: list[str] = []
    store = SimpleNamespace(calendarItemWithIdentifier_=lambda ident: asked.append(ident) or reminder)

    assert reminders_module._find_reminder(store, "x-apple-reminder://ABC-1") is reminder
    assert reminders_module._find_reminder(store, "ABC-1") is reminder
    assert asked == ["ABC-1", "ABC-1"]


def test_find_reminder_ignores_non_reminder_items():
    store = SimpleNamespace(calendarItemWithIdentifier_=lambda ident: FakeReminder("x", "event-like"))

    assert reminders_module._find_reminder(store, "x") is None


def test_reminder_row_shape():
    row = reminders_module._reminder_row(
        FakeReminder("r1", "Pay", due=datetime(2026, 4, 20, 9, 0), priority=5, notes="n", completed=True),
        True,
    )

    assert row == {
        "id": "r1",
        "title": "Pay",
        "notes": "n",
        "completed": True,
        "due": "2026-04-20T09:00:00+08:00",
        "priority": 5,
        "flagged": True,
        "list_id": "list-1",
        "list_name": "Inbox",
        "list_path": "iCloud/Inbox",
    }


@pytest.mark.parametrize(
    ("completed", "kind"),
    [(False, "incomplete"), (True, "completed"), (None, "all")],
)
def test_reminders_search_picks_predicate_by_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completed, kind):
    store = FakeReminderStore([])
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))

    _search(_bridge(tmp_path), completed=completed, list_path="iCloud/Work")

    assert store.predicates[0][0] == kind
    assert store.predicates[0][-1] == [store.calendars[1]]


def test_reminders_search_widens_incomplete_due_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = FakeReminderStore([])
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))

    _search(_bridge(tmp_path), completed=False, due_start="2026-04-20T00:00", due_end="2026-04-21T00:00")

    _, start, end, calendars = store.predicates[0]
    assert eventkit.nsdate_to_iso(start) == "2026-04-19T00:00:00+08:00"
    assert eventkit.nsdate_to_iso(end) == "2026-04-22T00:00:00+08:00"
    assert calendars is None


def test_reminders_search_unknown_list_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(FakeReminderStore([])))

    with pytest.raises(ValueError, match="reminders list not found: Nope"):
        _search(_bridge(tmp_path), list_name="Nope")


def test_reminders_search_filters_sorts_and_truncates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = datetime(2026, 4, 20, 9, 0)
    reminders = [FakeReminder(f"r{i}", f"task {i}", due=base + timedelta(days=i), priority=i % 3) for i in range(12)]
    reminders.append(FakeReminder("nodue", "task nodue"))
    reminders.append(FakeReminder("other", "unrelated", due=base))
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(FakeReminderStore(reminders)))
    monkeypatch.setattr(reminders_module, "read_flags", lambda: {"r3": True, "r4": False})
    bridge = _bridge(tmp_path)

    result = _search(bridge, query="TASK")
    assert [row["id"] for row in result["results"]] == [f"r{i}" for i in range(10)]
    assert result["truncated"] is True
    assert result["warning"]

    result = _search(bridge, query="task", sort_by="due_desc", limit=3)
    assert [row["id"] for row in result["results"]] == ["r11", "r10", "r9"]

    # Bounds are inclusive and exclude reminders without a due date.
    result = _search(bridge, due_start="2026-04-21T09:00", due_end="2026-04-23T09:00", priority_min=1)
    assert [row["id"] for row in result["results"]] == ["r1", "r2"]
    assert result["truncated"] is False

    result = _search(bridge, flagged=True)
    assert [(row["id"], row["flagged"]) for row in result["results"]] == [("r3", True)]
    result = _search(bridge, flagged=False)
    assert [row["id"] for row in result["results"]] == ["r4"]
    assert _search(bridge, query="nodue")["results"][0]["flagged"] is None


def test_reminders_search_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(FakeReminderStore([], deliver=False)))

    with pytest.raises(RuntimeError, match="reminders search timed out after 0.1 seconds"):
        _search(_bridge(tmp_path, timeout_seconds=0.1))


def test_set_reminder_flagged_uses_one_by_id_reference(tmp_path: Path):
    bridge = _bridge(tmp_path)
    calls = []
    bridge._run_jxa_json = lambda body, **kwargs: calls.append((body, kwargs)) or {"ok": True}

    bridge._set_reminder_flagged("ABC-1", True)

    body, kwargs = calls[0]
    assert "reminders.byId(`x-apple-reminder://${payload.reminder_id}`).flagged.set(payload.flagged)" in body
    assert "whose" not in body
    assert kwargs["payload"] == {"reminder_id": "ABC-1", "flagged": True}


def _write_reminders_store(path: Path, rows: list[tuple[int, bytes | None, int]], *, reminder_ent: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER)")
    connection.executemany(
        "INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME) VALUES (?, ?)",
        [(reminder_ent, "REMCDReminder"), (reminder_ent + 3, "REMCDSavedReminder")],
    )
    connection.execute(
        "CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZIDENTIFIER BLOB, "
        "ZFLAGGED INTEGER, ZMARKEDFORDELETION INTEGER, ZTITLE VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO ZREMCDREMINDER (Z_ENT, ZIDENTIFIER, ZFLAGGED, ZMARKEDFORDELETION) VALUES (?, ?, ?, 0)", rows
    )
    connection.commit()
    connection.close()


def test_read_flags_maps_identifier_blob_to_event_kit_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    flagged_id = uuid.UUID("0B964282-BAC0-4D87-AE17-4463363F3225")
    plain_id = uuid.uuid4()
    saved_id = uuid.uuid4()
    other_store_id = uuid.uuid4()
    _write_reminders_store(
        tmp_path / "Data-ABC.sqlite",
        [(39, flagged_id.bytes, 1), (39, plain_id.bytes, 0), (42, saved_id.bytes, 1), (39, None, 1)],
        reminder_ent=39,
    )
    _write_reminders_store(tmp_path / "Data-local.sqlite", [(40, other_store_id.bytes, 1)], reminder_ent=40)
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path)

    assert reminders_db.read_flags() == {
        "0B964282-BAC0-4D87-AE17-4463363F3225": True,
        str(plain_id).upper(): False,
        str(other_store_id).upper(): True,
    }


def test_read_flags_without_stores_points_at_full_disk_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path / "missing")

    with pytest.raises(PermissionError, match="Full Disk Access"):
        reminders_db.read_flags()


def test_read_flags_reports_unreadable_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "Data-ABC.sqlite").write_bytes(b"not a database")
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path)

    with pytest.raises(PermissionError, match="Data-ABC.sqlite"):
        reminders_db.read_flags()


class FakeEventStore:
    def __init__(self, events, calendars=None):
        self.events = events
        self.calendars = calendars or [FakeCalendar("Work")]
        self.range = None

    def calendarsForEntityType_(self, entity_type):
        return self.calendars

    def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, calendars):
        self.range = (start, end, calendars)
        return "predicate"

    def eventsMatchingPredicate_(self, predicate):
        return self.events


def test_calendar_conflicts_uses_strict_overlap_and_exclusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    day = datetime(2026, 4, 20)
    events = [
        FakeEvent("touching", "Before", day.replace(hour=13), day.replace(hour=14)),
        FakeEvent("overlap", "Overlap", day.replace(hour=14, minute=30), day.replace(hour=16)),
        FakeEvent("self", "Self", day.replace(hour=14), day.replace(hour=15)),
        FakeEvent("after", "After", day.replace(hour=15), day.replace(hour=16)),
    ]
    store = FakeEventStore(events)
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(store))

    result = _bridge(tmp_path).calendar_conflicts(
        calendar=None, calendars=None, start="2026-04-20T14:00", end="2026-04-20T15:00",
        exclude_event_uid="self", all_day=None, limit=None,
    )

    assert [row["uid"] for row in result["conflicts"]] == ["overlap"]
    assert result["requested_range"] == {"start": "2026-04-20T14:00:00+08:00", "end": "2026-04-20T15:00:00+08:00"}
    assert result["conflicts"][0]["start"] == "2026-04-20T14:30:00+08:00"


def test_calendar_search_fills_missing_bound_and_truncates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    day = datetime(2026, 4, 20, 9)
    events = [FakeEvent(f"e{i}", f"Meeting {i}", day + timedelta(hours=i), day + timedelta(hours=i, minutes=30)) for i in range(12)]
    store = FakeEventStore(list(reversed(events)), calendars=[FakeCalendar("Work"), FakeCalendar("Home")])
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(store))

    result = _bridge(tmp_path).calendar_search(
        calendar="iCloud/Work", calendars=None, query="meeting", start="2026-04-20T00:00", end=None,
        all_day=None, sort_by=None, limit=None,
    )

    assert result["searched_range"] == {"start": "2026-04-20T00:00:00+08:00", "end": "2030-04-19T00:00:00+08:00"}
    assert store.range[2] == [store.calendars[0]]
    assert [row["uid"] for row in result["results"]] == [f"e{i}" for i in range(10)]
    assert result["truncated"] is True


def test_calendar_unknown_name_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(FakeEventStore([])))

    with pytest.raises(ValueError, match="calendar not found: Nope"):
        _bridge(tmp_path).calendar_search(
            calendar=None, calendars=["Nope"], query=None, start=None, end=None,
            all_day=None, sort_by=None, limit=None,
        )


def test_calendar_create_rejects_ambiguous_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calendars = [FakeCalendar("Work", source="iCloud"), FakeCalendar("Work", source="Google")]
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(FakeEventStore([], calendars)))

    result = _bridge(tmp_path).calendar_create(
        calendar="Work", title="t", start=datetime(2026, 4, 20, 9), end=datetime(2026, 4, 20, 10),
        notes=None, location=None, url=None, all_day=None,
    )

    assert result["ok"] is False
    assert "ambiguous: iCloud/Work, Google/Work" in result["error"]


def test_calendar_update_refuses_to_move_recurring_series(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    event = EventKit.EKEvent.eventWithEventStore_(EventKit.EKEventStore.alloc().init())
    event.addRecurrenceRule_(
        EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
            EventKit.EKRecurrenceFrequencyDaily, 1, None
        )
    )
    store = SimpleNamespace(calendarItemWithIdentifier_=lambda uid: event)
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(store))

    result = _bridge(tmp_path).calendar_update(
        event_uid="series", calendar=None, title=None, start=datetime(2026, 4, 20, 9), end=None,
        notes=None, location=None, url=None, all_day=None,
    )

    assert result["ok"] is False
    assert "recurring" in result["error"]


def test_all_day_end_at_midnight_is_exclusive():
    start = datetime(2026, 10, 31)

    assert calendar_module._all_day_end(start, datetime(2026, 11, 1)).isoformat() == "2026-10-31T23:59:59+08:00"
    assert calendar_module._all_day_end(start, start).isoformat() == "2026-10-31T00:00:00+08:00"
    assert calendar_module._all_day_end(start, datetime(2026, 10, 31, 18)).isoformat() == "2026-10-31T18:00:00+08:00"


def test_open_store_reuses_one_store_and_resets_it(monkeypatch: pytest.MonkeyPatch):
    created: list[MagicMock] = []

    def new_store():
        created.append(MagicMock())
        return created[-1]

    fake_eventkit = SimpleNamespace(
        EKEventStore=SimpleNamespace(
            authorizationStatusForEntityType_=lambda entity_type: 3,
            alloc=lambda: SimpleNamespace(init=new_store),
        ),
        EKAuthorizationStatusFullAccess=3,
    )
    monkeypatch.setattr(eventkit, "EventKit", fake_eventkit)
    monkeypatch.setattr(eventkit, "_stores", {})

    with eventkit.open_store(EventKit.EKEntityTypeEvent) as first:
        pass
    with eventkit.open_store(EventKit.EKEntityTypeEvent) as second:
        pass

    assert first is second
    assert len(created) == 1
    first.reset.assert_called_once_with()


def test_reminders_tool_flagged_only_update_reaches_bridge():
    bridge = MagicMock()
    bridge.reminders_update.return_value = {"ok": True, "reminder": {"id": "r1", "flagged": True}}
    tool = create_reminders_tool(bridge)

    tool(action="update", reminder_id="r1", flagged=True)

    assert bridge.reminders_update.call_args.kwargs["flagged"] is True


def test_open_store_holds_the_store_for_the_whole_operation(monkeypatch: pytest.MonkeyPatch):
    store = MagicMock()
    monkeypatch.setattr(eventkit, "_permission_problem", lambda entity_type: None)
    monkeypatch.setattr(eventkit, "_stores", {EventKit.EKEntityTypeReminder: store})
    order: list[str] = []

    def other_operation():
        with eventkit.open_store(EventKit.EKEntityTypeReminder):
            order.append("other")

    other = threading.Thread(target=other_operation)
    with eventkit.open_store(EventKit.EKEntityTypeReminder) as outer:
        # A nested call in the same operation neither deadlocks nor resets.
        with eventkit.open_store(EventKit.EKEntityTypeReminder) as inner:
            assert inner is outer
        assert store.reset.call_count == 1
        other.start()
        other.join(0.2)
        assert other.is_alive()
        order.append("outer")
    other.join(5)

    assert order == ["outer", "other"]
    assert store.reset.call_count == 2


def _alarm_dates(reminder) -> list[str]:
    # EventKit keeps alarms in its own order.
    return sorted(
        eventkit.nsdate_to_iso(alarm.absoluteDate()) if alarm.absoluteDate() else f"offset {alarm.relativeOffset():g}"
        for alarm in reminder.alarms() or []
    )


def test_set_due_moves_only_the_alarm_at_the_old_due():
    reminder = EventKit.EKReminder.reminderWithEventStore_(EventKit.EKEventStore.alloc().init())
    reminder.setDueDateComponents_(eventkit.datetime_to_components(datetime(2026, 4, 20, 9, 0)))
    reminder.setAlarms_(
        [
            EventKit.EKAlarm.alarmWithAbsoluteDate_(eventkit.to_nsdate(datetime(2026, 4, 20, 9, 0))),
            EventKit.EKAlarm.alarmWithRelativeOffset_(-86400),
            EventKit.EKAlarm.alarmWithAbsoluteDate_(eventkit.to_nsdate(datetime(2026, 4, 19, 20, 0))),
        ]
    )

    reminders_module._set_due(reminder, datetime(2026, 4, 21, 10, 0, 0, 500000))
    reminders_module._set_due(reminder, datetime(2026, 4, 22, 11, 0))

    assert eventkit.components_to_iso(reminder.dueDateComponents()) == "2026-04-22T11:00:00+08:00"
    assert _alarm_dates(reminder) == ["2026-04-19T20:00:00+08:00", "2026-04-22T11:00:00+08:00", "offset -86400"]


def test_set_due_on_new_reminder_adds_one_absolute_alarm():
    reminder = EventKit.EKReminder.reminderWithEventStore_(EventKit.EKEventStore.alloc().init())

    reminders_module._set_due(reminder, datetime(2026, 4, 20, 9, 0))

    assert _alarm_dates(reminder) == ["2026-04-20T09:00:00+08:00"]


def test_reminders_create_succeeds_with_warning_when_flag_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reminder = FakeReminder("r1", "Pay")
    reminder.setCalendar_ = lambda calendar: None
    reminder.setTitle_ = lambda title: None
    saved: list[FakeReminder] = []
    store = SimpleNamespace(
        defaultCalendarForNewReminders=lambda: None,
        saveReminder_commit_error_=lambda item, commit, error: saved.append(item) or (True, None),
    )
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))
    monkeypatch.setattr(
        reminders_module,
        "EventKit",
        SimpleNamespace(
            EKEntityTypeReminder=EventKit.EKEntityTypeReminder,
            EKReminder=SimpleNamespace(reminderWithEventStore_=lambda store: reminder),
        ),
    )
    bridge = _bridge(tmp_path)

    def jxa_fails(body, **kwargs):
        raise RuntimeError("reminders set flagged timed out after 5.0 seconds")

    bridge._run_jxa_json = jxa_fails

    result = bridge.reminders_create(
        list_id=None, list_name=None, list_path=None, title="Pay", notes=None, due=None, priority=None, flagged=True,
    )

    assert saved == [reminder]
    assert result["ok"] is True
    assert result["reminder"]["id"] == "r1"
    assert result["reminder"]["flagged"] is None
    assert result["warning"] == (
        "reminder saved, but flagged=True was not applied: reminders set flagged timed out after 5.0 seconds"
    )


class WindowedEventStore(FakeEventStore):
    """Return only events overlapping the predicate range, like EventKit."""

    def __init__(self, events):
        super().__init__(events)
        self.ranges: list[tuple] = []

    def predicateForEventsWithStartDate_endDate_calendars_(self, start, end, calendars):
        self.ranges.append((start, end))
        return start, end

    def eventsMatchingPredicate_(self, predicate):
        start, end = predicate
        return [
            event for event in self.events
            if event.startDate().compare_(end) == -1 and event.endDate().compare_(start) == 1
        ]


def test_calendar_search_splits_long_range_into_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = [
        FakeEvent("late", "Late", datetime(2029, 6, 1, 9), datetime(2029, 6, 1, 10)),
        # Spans the first window boundary (2023-12-31), so two windows return it.
        FakeEvent("boundary", "Trip", datetime(2023, 12, 30), datetime(2024, 1, 2), all_day=True),
        FakeEvent("early", "Early", datetime(2020, 6, 1, 9), datetime(2020, 6, 1, 10)),
    ]
    store = WindowedEventStore(events)
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(store))
    bridge = _bridge(tmp_path)

    def search(**overrides):
        params = dict(calendar=None, calendars=None, query=None, start="2020-01-01T00:00", end="2030-01-01T00:00",
                      all_day=None, sort_by=None, limit=None)
        params.update(overrides)
        return bridge.calendar_search(**params)

    result = search()

    assert [(eventkit.nsdate_to_iso(start), eventkit.nsdate_to_iso(end)) for start, end in store.ranges] == [
        ("2020-01-01T00:00:00+08:00", "2023-12-31T00:00:00+08:00"),
        ("2023-12-31T00:00:00+08:00", "2027-12-30T00:00:00+08:00"),
        ("2027-12-30T00:00:00+08:00", "2030-01-01T00:00:00+08:00"),
    ]
    assert [row["uid"] for row in result["results"]] == ["early", "boundary", "late"]
    assert result["searched_range"] == {"start": "2020-01-01T00:00:00+08:00", "end": "2030-01-01T00:00:00+08:00"}
    assert result["truncated"] is False

    limited = search(sort_by="start_desc", limit=2)
    assert [row["uid"] for row in limited["results"]] == ["late", "boundary"]
    assert limited["truncated"] is True


def test_calendar_conflicts_splits_long_range_into_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = [
        FakeEvent("late", "Late", datetime(2029, 6, 1, 9), datetime(2029, 6, 1, 10)),
        # Spans the first window boundary (2023-12-31), so two windows return it.
        FakeEvent("boundary", "Trip", datetime(2023, 12, 30), datetime(2024, 1, 2), all_day=True),
        FakeEvent("early", "Early", datetime(2020, 6, 1, 9), datetime(2020, 6, 1, 10)),
    ]
    store = WindowedEventStore(events)
    monkeypatch.setattr(calendar_module, "open_store", lambda entity_type: nullcontext(store))

    result = _bridge(tmp_path).calendar_conflicts(
        calendar=None, calendars=None, start="2020-01-01T00:00", end="2030-01-01T00:00",
        exclude_event_uid=None, all_day=None, limit=None,
    )

    assert [(eventkit.nsdate_to_iso(start), eventkit.nsdate_to_iso(end)) for start, end in store.ranges] == [
        ("2020-01-01T00:00:00+08:00", "2023-12-31T00:00:00+08:00"),
        ("2023-12-31T00:00:00+08:00", "2027-12-30T00:00:00+08:00"),
        ("2027-12-30T00:00:00+08:00", "2030-01-01T00:00:00+08:00"),
    ]
    assert [row["uid"] for row in result["conflicts"]] == ["early", "boundary", "late"]
