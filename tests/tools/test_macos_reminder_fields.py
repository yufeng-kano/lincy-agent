"""Reminder fields beyond title and due time: all-day, early reminder, recurrence,
location, urgent, tags, URL, subtasks, and images."""

from contextlib import nullcontext
from datetime import date, datetime
from pathlib import Path
import sqlite3
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid
import zlib

import EventKit
import pytest

from lincy.tools.builtin.macos_apps import MacOSAppBridge, create_reminders_tool
from lincy.tools.builtin.macos_apps import eventkit
from lincy.tools.builtin.macos_apps import reminderkit
from lincy.tools.builtin.macos_apps import reminders as reminders_module
from lincy.tools.builtin.macos_apps import reminders_db
from lincy.tools.builtin.macos_apps.reminders import ReminderLocation
from lincy.tools.builtin.macos_apps.reminders_db import ReminderExtras

PRIVATE_NONE = {"urgent": None, "tags": None, "url": None, "early_reminder": None, "subtasks": None, "images": None}


def _bridge(tmp_path: Path) -> MacOSAppBridge:
    return MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )


def _write_png(path: Path) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\x20\x60\xd0" * 4 for _ in range(3))
    header = struct.pack(">IIBBBBB", 4, 3, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _reminder(due: datetime | None = None):
    reminder = EventKit.EKReminder.reminderWithEventStore_(EventKit.EKEventStore.alloc().init())
    if due is not None:
        reminders_module._set_due(reminder, due)
    return reminder


class StubReminder:
    """A saved reminder as _reminder_row reads it; real EKReminders have no list until saved."""

    def __init__(self, source):
        self._source = source

    def calendarItemIdentifier(self):
        return "R1"

    def title(self):
        return "Pay"

    def notes(self):
        return None

    def isCompleted(self):
        return False

    def priority(self):
        return 0

    def calendar(self):
        return SimpleNamespace(
            calendarIdentifier=lambda: "list-1",
            title=lambda: "Inbox",
            source=lambda: SimpleNamespace(title=lambda: "iCloud"),
        )

    def dueDateComponents(self):
        return self._source.dueDateComponents()

    def alarms(self):
        return self._source.alarms()

    def recurrenceRules(self):
        return self._source.recurrenceRules()


def test_set_due_with_a_date_makes_an_all_day_reminder_without_an_alarm():
    reminder = _reminder(datetime(2026, 4, 20, 9, 0))

    reminders_module._set_due(reminder, date(2026, 4, 22))

    assert eventkit.is_date_only(reminder.dueDateComponents())
    assert eventkit.components_to_iso(reminder.dueDateComponents()) == "2026-04-22T00:00:00+08:00"
    assert not reminder.alarms()


def test_set_due_with_a_time_after_a_date_adds_the_due_alarm():
    reminder = _reminder()
    reminders_module._set_due(reminder, date(2026, 4, 22))

    reminders_module._set_due(reminder, datetime(2026, 4, 23, 8, 30))

    assert not eventkit.is_date_only(reminder.dueDateComponents())
    assert [eventkit.nsdate_to_iso(alarm.absoluteDate()) for alarm in reminder.alarms()] == ["2026-04-23T08:30:00+08:00"]


def test_set_recurrence_replaces_then_removes_the_rule():
    reminder = _reminder(datetime(2026, 4, 20, 9, 0))

    reminders_module._set_recurrence(reminder, "daily", 1)
    reminders_module._set_recurrence(reminder, "weekly", 2)

    rules = reminder.recurrenceRules()
    assert [(rule.frequency(), rule.interval()) for rule in rules] == [(EventKit.EKRecurrenceFrequencyWeekly, 2)]

    reminders_module._set_recurrence(reminder, None, 1)

    assert not reminder.recurrenceRules()


def test_set_location_replaces_the_location_alarm_and_keeps_the_due_alarm():
    reminder = _reminder(datetime(2026, 4, 20, 9, 0))

    reminders_module._set_location(reminder, ReminderLocation(title="Office", latitude=25.0, longitude=121.5))
    reminders_module._set_location(
        reminder, ReminderLocation(title="Station", latitude=25.0478, longitude=121.517, radius_m=150, trigger="leave")
    )

    assert reminders_module._location_row(reminder) == {
        "title": "Station",
        "latitude": 25.0478,
        "longitude": 121.517,
        "radius_m": 150.0,
        "trigger": "leave",
    }
    assert len(reminder.alarms()) == 2

    reminders_module._set_location(reminder, None)

    assert reminders_module._location_row(reminder) is None
    assert [eventkit.nsdate_to_iso(alarm.absoluteDate()) for alarm in reminder.alarms()] == ["2026-04-20T09:00:00+08:00"]


def test_reminder_row_carries_optional_fields_only_when_set():
    plain = StubReminder(_reminder(datetime(2026, 4, 20, 9, 0)))
    source = _reminder()
    reminders_module._set_due(source, date(2026, 4, 22))
    reminders_module._set_recurrence(source, "monthly", 3)
    extras = ReminderExtras(
        urgent=True, early_reminders=["1d"], tags=["school"], url="https://example.com", image_count=2,
        parent_id="P1", subtask_ids=["S1"],
    )

    plain_row = reminders_module._reminder_row(plain, False, ReminderExtras())
    full_row = reminders_module._reminder_row(StubReminder(source), False, extras)

    optional = {
        "all_day", "recurrence", "location", "early_reminders", "urgent", "tags", "url", "image_count",
        "parent_id", "subtask_ids",
    }
    assert optional.isdisjoint(plain_row)
    assert {key: full_row[key] for key in optional - {"location"}} == {
        "all_day": True,
        "recurrence": {"frequency": "monthly", "interval": 3},
        "early_reminders": ["1d"],
        "urgent": True,
        "tags": ["school"],
        "url": "https://example.com",
        "image_count": 2,
        "parent_id": "P1",
        "subtask_ids": ["S1"],
    }
    assert "location" not in full_row


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"early_reminder": "-1d", "due": datetime(2026, 4, 20, 9)}, "invalid early_reminder"),
        ({"early_reminder": "0d", "due": datetime(2026, 4, 20, 9)}, "invalid early_reminder"),
        ({"early_reminder": "1 day", "due": datetime(2026, 4, 20, 9)}, "invalid early_reminder"),
        ({"early_reminder": "1d"}, "'early_reminder' needs 'due'"),
        ({"recurrence": "weekly"}, "'recurrence' needs 'due'"),
        ({"recurrence": "hourly", "due": datetime(2026, 4, 20, 9)}, "invalid recurrence"),
        ({"recurrence_interval": 2, "due": datetime(2026, 4, 20, 9)}, "needs 'recurrence'"),
        ({"recurrence": "daily", "recurrence_interval": 0, "due": datetime(2026, 4, 20, 9)}, "must be 1 or more"),
        ({"urgent": True}, "'urgent' needs 'due' with a time"),
        ({"urgent": True, "due": date(2026, 4, 20)}, "'urgent' needs 'due' with a time"),
        ({"tags": []}, "at least one tag"),
        ({"tags": ["two words"]}, "without '#' or spaces"),
        ({"tags": ["#school"]}, "without '#' or spaces"),
        ({"url": "example.com"}, "include the scheme"),
        ({"url": "https://a b.com"}, "invalid url"),
        ({"subtasks": ["ok", " "]}, "'subtasks' titles"),
        ({"images": [""]}, "'images' paths"),
        ({"clear": ["tags"]}, "'clear' only applies to update"),
        ({"creating": False, "clear": ["due"]}, "cannot clear 'due'"),
        ({"creating": False, "clear": ["tags"], "tags": ["a"]}, "'tags' is both set and cleared"),
        ({"creating": False, "clear": ["location"], "location": {"title": "x"}}, "'location' is both set and cleared"),
    ],
)
def test_field_problem_refuses_bad_input(fields, expected):
    params = {
        "creating": True, "due": None, "early_reminder": None, "recurrence": None, "recurrence_interval": None,
        "urgent": None, "tags": None, "url": None, "location": None, "subtasks": None, "images": None, "clear": None,
    }
    params.update(fields)

    assert expected in reminders_module._field_problem(**params)


def test_field_problem_accepts_valid_input():
    assert reminders_module._field_problem(
        creating=True, due=datetime(2026, 4, 20, 9), early_reminder="2w", recurrence="weekly", recurrence_interval=2,
        urgent=True, tags=["school"], url="https://example.com", location=None, subtasks=["a"], images=["a.png"],
        clear=None,
    ) is None
    # On update the due date may already be on the reminder; the bridge checks it.
    assert reminders_module._field_problem(
        creating=False, due=None, early_reminder="1d", recurrence="daily", recurrence_interval=None, urgent=True,
        tags=None, url=None, location=None, subtasks=None, images=None, clear=["url"],
    ) is None


def test_reminders_tool_passes_a_date_only_due_as_a_date():
    bridge = MagicMock()
    bridge.reminders_create.return_value = {"ok": True, "reminder": {"id": "r1"}}
    tool = create_reminders_tool(bridge)

    tool(action="create", title="Hand in", due="2026-11-22", early_reminder="2d")

    kwargs = bridge.reminders_create.call_args.kwargs
    assert kwargs["due"] == date(2026, 11, 22)
    assert not isinstance(kwargs["due"], datetime)
    assert kwargs["early_reminder"] == "2d"


def test_reminders_tool_treats_any_iso_date_without_time_as_all_day():
    bridge = MagicMock()
    bridge.reminders_create.return_value = {"ok": True, "reminder": {"id": "r1"}}
    tool = create_reminders_tool(bridge)

    tool(action="create", title="t", due="20261122")
    assert bridge.reminders_create.call_args.kwargs["due"] == date(2026, 11, 22)

    tool(action="create", title="t", due="2026-11-22T09:30")
    assert bridge.reminders_create.call_args.kwargs["due"] == datetime(2026, 11, 22, 9, 30)

    with pytest.raises(ValueError, match="invalid due: '2026/11/22'"):
        tool(action="create", title="t", due="2026/11/22")


def test_reminders_tool_validates_location_and_forwards_clear():
    bridge = MagicMock()
    bridge.reminders_update.return_value = {"ok": True, "reminder": {"id": "r1"}}
    tool = create_reminders_tool(bridge)

    tool(
        action="update", reminder_id="r1", clear=["recurrence"],
        location={"title": "Station", "latitude": 25.0478, "longitude": 121.517},
    )

    kwargs = bridge.reminders_update.call_args.kwargs
    assert kwargs["clear"] == ["recurrence"]
    assert kwargs["location"] == ReminderLocation(title="Station", latitude=25.0478, longitude=121.517)


def test_reminders_tool_refuses_bad_fields_before_reaching_the_bridge():
    bridge = MagicMock()
    tool = create_reminders_tool(bridge)

    assert "invalid early_reminder" in tool(action="create", title="t", due="2026-11-22T09:00", early_reminder="-1d")
    assert "invalid location: latitude" in tool(
        action="create", title="t", location={"title": "x", "latitude": 200, "longitude": 0}
    )
    assert "invalid location: radius" in tool(
        action="update", reminder_id="r1", location={"title": "x", "latitude": 1, "longitude": 1, "radius": 5}
    )
    bridge.reminders_create.assert_not_called()
    bridge.reminders_update.assert_not_called()


def test_reminders_tool_clear_alone_counts_as_a_change():
    bridge = MagicMock()
    bridge.reminders_update.return_value = {"ok": True, "reminder": {"id": "r1"}}
    tool = create_reminders_tool(bridge)

    tool(action="update", reminder_id="r1", clear=["early_reminder"])

    assert bridge.reminders_update.call_args.kwargs["clear"] == ["early_reminder"]


@pytest.mark.parametrize(
    ("due", "fields", "expected"),
    [
        (None, {"early_reminder": "1d"}, "'early_reminder' needs a due date"),
        (None, {"recurrence": "daily"}, "'recurrence' needs a due date"),
        (None, {"urgent": True}, "'urgent' needs a due date with a time"),
        (date(2026, 4, 20), {"urgent": True}, "'urgent' needs a due date with a time"),
    ],
)
def test_reminders_update_checks_the_due_date_before_saving(tmp_path: Path, monkeypatch, due, fields, expected):
    reminder = _reminder()
    if due is not None:
        reminders_module._set_due(reminder, due)
    save = MagicMock()
    store = SimpleNamespace(calendarItemWithIdentifier_=lambda identifier: reminder, saveReminder_commit_error_=save)
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))

    result = _bridge(tmp_path).reminders_update(
        reminder_id="R1", title=None, notes=None, due=None, priority=None, flagged=None, completed=None, **fields
    )

    assert result == {"ok": False, "error": f"{expected}; pass 'due' as well"}
    save.assert_not_called()


def test_reminder_image_paths_must_be_files_inside_allowed_paths(tmp_path: Path):
    bridge = _bridge(tmp_path)
    image = tmp_path / "photo.png"
    _write_png(image)
    not_an_image = tmp_path / "notes.txt"
    not_an_image.write_text("text")

    assert reminderkit.image_size(image) == (4, 3)
    assert bridge._reminder_image_paths([str(image)]) == [image.resolve()]
    with pytest.raises(ValueError, match="not a readable image"):
        bridge._reminder_image_paths([str(not_an_image)])
    assert bridge._reminder_image_paths(None) is None
    with pytest.raises(ValueError, match="inside allowed paths"):
        bridge._reminder_image_paths(["/etc/hosts"])
    with pytest.raises(ValueError, match="inside allowed paths"):
        bridge._reminder_image_paths([str(tmp_path / "missing.png")])


def test_finish_reminder_write_replaces_the_existing_early_reminder(tmp_path: Path, monkeypatch):
    before = ReminderExtras(early_reminders=["1d"], early_reminder_ids=["ALERT-1"])
    after = ReminderExtras(early_reminders=["2h"], early_reminder_ids=["ALERT-2"], urgent=True)
    reads = iter([{"R1": before}, {"R1": after}])
    writes = []
    monkeypatch.setattr(reminders_module, "read_extras", lambda: next(reads))
    monkeypatch.setattr(reminders_module, "read_flags", lambda: {"R1": True})
    monkeypatch.setattr(reminders_module, "write_private_fields", lambda identifier, **kwargs: writes.append((identifier, kwargs)))

    result = _bridge(tmp_path)._finish_reminder_write(
        StubReminder(_reminder(datetime(2026, 4, 20, 9))),
        created=False,
        flagged=None,
        private={**PRIVATE_NONE, "early_reminder": "2h", "urgent": True},
    )

    assert writes == [("R1", {**PRIVATE_NONE, "early_reminder": "2h", "urgent": True, "early_reminder_ids": ["ALERT-1"]})]
    assert "warning" not in result
    assert result["reminder"]["early_reminders"] == ["2h"]
    assert result["reminder"]["urgent"] is True
    assert result["reminder"]["flagged"] is True


def test_finish_reminder_write_skips_reminderkit_without_private_fields(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reminders_module, "write_private_fields", MagicMock(side_effect=AssertionError("not expected")))
    monkeypatch.setattr(reminders_module, "read_extras", MagicMock(side_effect=AssertionError("not expected")))

    result = _bridge(tmp_path)._finish_reminder_write(
        StubReminder(_reminder()), created=True, flagged=None, private=dict(PRIVATE_NONE)
    )

    assert result == {"ok": True, "reminder": reminders_module._reminder_row(StubReminder(_reminder()), False, None)}


@pytest.mark.parametrize("error", [RuntimeError("ReminderKit save failed"), AttributeError("no selector"), PermissionError("db")])
def test_finish_reminder_write_reports_any_failure_after_the_save_as_a_warning(tmp_path: Path, monkeypatch, error):
    def fail(identifier, **kwargs):
        raise error

    monkeypatch.setattr(reminders_module, "write_private_fields", fail)

    result = _bridge(tmp_path)._finish_reminder_write(
        StubReminder(_reminder(datetime(2026, 4, 20, 9))),
        created=True,
        flagged=None,
        private={**PRIVATE_NONE, "early_reminder": "1d", "tags": ["school"]},
    )

    assert result["ok"] is True
    assert result["reminder"]["id"] == "R1"
    assert result["warning"] == f"reminder saved, but tags, early_reminder may not be applied: {error}"


def test_finish_reminder_write_warns_when_the_readback_differs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reminders_module, "write_private_fields", lambda identifier, **kwargs: None)
    monkeypatch.setattr(reminders_module, "read_extras", lambda: {"R1": ReminderExtras(subtask_ids=["S1"])})

    result = _bridge(tmp_path)._finish_reminder_write(
        StubReminder(_reminder(datetime(2026, 4, 20, 9))),
        created=True,
        flagged=None,
        private={**PRIVATE_NONE, "urgent": True, "subtasks": ["a", "b"], "images": [Path("a.png")]},
    )

    assert result["warning"] == (
        "reminder saved, but 1 of 2 subtasks read back; 0 of 1 images read back; urgent reads back as False"
    )


def test_reminders_update_maps_clear_to_reminderkit_values(tmp_path: Path, monkeypatch):
    reminder = _reminder(datetime(2026, 4, 20, 9))
    reminders_module._set_recurrence(reminder, "daily", 1)
    store = SimpleNamespace(
        calendarItemWithIdentifier_=lambda identifier: reminder,
        saveReminder_commit_error_=lambda item, commit, error: (True, None),
    )
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))
    bridge = _bridge(tmp_path)
    bridge._finish_reminder_write = MagicMock(return_value={"ok": True})

    bridge.reminders_update(
        reminder_id="R1", title=None, notes=None, due=None, priority=None, flagged=None, completed=None,
        clear=["early_reminder", "recurrence", "url", "tags"],
    )

    assert not reminder.recurrenceRules()
    assert bridge._finish_reminder_write.call_args.kwargs["private"] == {
        **PRIVATE_NONE, "tags": [], "url": "", "early_reminder": "none",
    }


def test_reminders_update_refuses_to_make_an_urgent_reminder_all_day(tmp_path: Path, monkeypatch):
    reminder = _reminder(datetime(2026, 4, 20, 9))
    save = MagicMock()
    store = SimpleNamespace(calendarItemWithIdentifier_=lambda identifier: reminder, saveReminder_commit_error_=save)
    monkeypatch.setattr(reminders_module, "open_store", lambda entity_type: nullcontext(store))
    monkeypatch.setattr(reminders_module, "read_extras", lambda: {reminder.calendarItemIdentifier(): ReminderExtras(urgent=True)})

    result = _bridge(tmp_path).reminders_update(
        reminder_id="R1", title=None, notes=None, due=date(2026, 4, 21), priority=None, flagged=None, completed=None,
    )

    assert result["ok"] is False
    assert "pass urgent=false" in result["error"]
    save.assert_not_called()


def test_write_private_fields_writes_a_negative_delta_and_removes_old_alerts(monkeypatch):
    classes = {name: MagicMock(name=name) for name in ("REMStore", "REMObjectID", "REMSaveRequest", "REMDueDateDeltaInterval")}
    store = classes["REMStore"].alloc.return_value.init.return_value
    store.fetchReminderWithObjectID_error_.return_value = (MagicMock(), None)
    save = classes["REMSaveRequest"].alloc.return_value.initWithStore_.return_value
    save.saveSynchronouslyWithError_.return_value = (True, None)
    alerts = save.updateReminder_.return_value.dueDateDeltaAlertContext.return_value
    monkeypatch.setattr(reminderkit, "_load", lambda: None)
    monkeypatch.setattr(reminderkit.objc, "lookUpClass", lambda name: classes[name])

    reminderkit.write_private_fields(
        "0B964282-BAC0-4D87-AE17-4463363F3225",
        urgent=None, tags=None, url=None, early_reminder="3w",
        early_reminder_ids=["11111111-2222-3333-4444-555555555555"], subtasks=None, images=None,
    )

    removed = alerts.removeDueDateDeltaAlertsWithIdentifiers_.call_args.args[0]
    assert [str(identifier.UUIDString()) for identifier in removed] == ["11111111-2222-3333-4444-555555555555"]
    # Unit 3 is weeks; the count is negative because it is added to the due date.
    classes["REMDueDateDeltaInterval"].alloc.return_value.initWithUnit_count_.assert_called_once_with(3, -3)
    alerts.addDueDateDeltaAlertWithDueDateDelta_.assert_called_once()
    object_url = classes["REMObjectID"].objectIDWithURL_.call_args.args[0]
    assert str(object_url.absoluteString()) == "x-apple-reminderkit://REMCDReminder/0B964282-BAC0-4D87-AE17-4463363F3225"


def test_write_private_fields_raises_when_the_save_fails(monkeypatch):
    classes = {name: MagicMock(name=name) for name in ("REMStore", "REMObjectID", "REMSaveRequest")}
    classes["REMStore"].alloc.return_value.init.return_value.fetchReminderWithObjectID_error_.return_value = (MagicMock(), None)
    save = classes["REMSaveRequest"].alloc.return_value.initWithStore_.return_value
    save.saveSynchronouslyWithError_.return_value = (False, None)
    monkeypatch.setattr(reminderkit, "_load", lambda: None)
    monkeypatch.setattr(reminderkit.objc, "lookUpClass", lambda name: classes[name])

    with pytest.raises(RuntimeError, match="ReminderKit save failed"):
        reminderkit.write_private_fields(
            "R1", urgent=True, tags=None, url=None, early_reminder=None, early_reminder_ids=[], subtasks=None, images=None
        )


def test_readback_problems_compares_each_written_field():
    before = ReminderExtras(subtask_ids=["S1"], image_count=1)
    extras = ReminderExtras(
        urgent=True, tags=["School"], url="https://example.com", early_reminders=["1d"],
        subtask_ids=["S1", "S2"], image_count=2,
    )

    assert reminders_module._readback_problems(
        {
            **PRIVATE_NONE, "urgent": True, "tags": ["school"], "url": "https://example.com", "early_reminder": "1d",
            "subtasks": ["new"], "images": [Path("a.png")],
        },
        before,
        extras,
    ) == []
    assert reminders_module._readback_problems(
        {**PRIVATE_NONE, "urgent": False, "tags": [], "url": "", "early_reminder": "none"}, extras, extras
    ) == [
        "urgent reads back as True",
        "tags read back as ['School']",
        "url reads back as 'https://example.com'",
        "early_reminder reads back as ['1d']",
    ]
    assert reminders_module._readback_problems({**PRIVATE_NONE, "urgent": True}, before, None) == [
        "the reminder could not be read back from the Reminders database"
    ]


def test_readback_compares_the_url_in_the_form_nsurl_stores():
    stored = ReminderExtras(url="https://example.com/a?y=%E4%BA%8C")

    assert reminders_module._readback_problems(
        {**PRIVATE_NONE, "url": "https://example.com/a?y=\u4e8c"}, ReminderExtras(), stored
    ) == []


def _write_extras_store(path: Path, *, urgent_column: bool = True) -> dict[str, uuid.UUID]:
    """Build a store with the real table and column names, including Core Data's numbered columns."""
    ids = {name: uuid.uuid4() for name in ("parent", "child", "plain", "deleted", "alert")}
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR)")
    connection.executemany(
        "INSERT INTO Z_PRIMARYKEY VALUES (?, ?)",
        [(39, "REMCDReminder"), (25, "REMCDImageAttachment"), (26, "REMCDURLAttachment"), (32, "REMCDHashtag"), (15, "REMCDAlarm")],
    )
    urgent = ", ZISURGENTSTATEENABLEDFORCURRENTUSER INTEGER" if urgent_column else ""
    connection.execute(
        "CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZIDENTIFIER BLOB, ZFLAGGED INTEGER, "
        f"ZMARKEDFORDELETION INTEGER, ZPARENTREMINDER INTEGER{urgent})"
    )
    rows = [(1, ids["parent"].bytes, 0, None), (2, ids["child"].bytes, 0, 1), (3, ids["plain"].bytes, 0, None), (4, ids["deleted"].bytes, 1, 1)]
    connection.executemany(
        "INSERT INTO ZREMCDREMINDER (Z_PK, Z_ENT, ZIDENTIFIER, ZFLAGGED, ZMARKEDFORDELETION, ZPARENTREMINDER) VALUES (?, 39, ?, 0, ?, ?)",
        rows,
    )
    if urgent_column:
        connection.execute("UPDATE ZREMCDREMINDER SET ZISURGENTSTATEENABLEDFORCURRENTUSER = (Z_PK = 1)")
    connection.execute(
        "CREATE TABLE ZREMCDDUEDATEDELTAALERT (Z_PK INTEGER PRIMARY KEY, ZREMINDERIDENTIFIER BLOB, ZIDENTIFIER BLOB, "
        "ZDUEDATEDELTACOUNT INTEGER, ZDUEDATEDELTAUNIT INTEGER, ZCREATIONDATE TIMESTAMP)"
    )
    connection.executemany(
        "INSERT INTO ZREMCDDUEDATEDELTAALERT (ZREMINDERIDENTIFIER, ZIDENTIFIER, ZDUEDATEDELTACOUNT, ZDUEDATEDELTAUNIT, ZCREATIONDATE) "
        "VALUES (?, ?, ?, ?, ?)",
        [(ids["parent"].bytes, ids["alert"].bytes, -2, 2, 1.0), (ids["plain"].bytes, uuid.uuid4().bytes, 1, 1, 2.0)],
    )
    connection.execute(
        "CREATE TABLE ZREMCDOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZMARKEDFORDELETION INTEGER, ZREMINDER INTEGER, "
        "ZREMINDER2 INTEGER, ZREMINDER3 INTEGER, ZREMINDERIDENTIFIER1 BLOB, ZNAME VARCHAR, ZNAME1 VARCHAR, ZURL VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO ZREMCDOBJECT (Z_ENT, ZMARKEDFORDELETION, ZREMINDER, ZREMINDER2, ZREMINDER3, ZNAME1, ZURL) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (15, 0, 1, None, None, None, None),
            (32, 0, None, None, 1, "school", None),
            (32, 0, None, None, 1, "exam", None),
            (32, 1, None, None, 1, "removed", None),
            (26, 0, None, 1, None, None, "https://example.com/a"),
            (25, 0, None, 1, None, None, None),
            (25, 0, None, 1, None, None, None),
            (25, 1, None, 1, None, None, None),
        ],
    )
    connection.commit()
    connection.close()
    return ids


def test_read_extras_collects_the_fields_event_kit_lacks(tmp_path: Path, monkeypatch):
    ids = _write_extras_store(tmp_path / "Data-ABC.sqlite")
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path)

    extras = reminders_db.read_extras()

    key = {name: str(value).upper() for name, value in ids.items()}
    assert set(extras) == {key["parent"], key["child"], key["plain"]}
    assert extras[key["parent"]] == ReminderExtras(
        urgent=True,
        early_reminders=["2d"],
        early_reminder_ids=[key["alert"]],
        tags=["school", "exam"],
        url="https://example.com/a",
        image_count=2,
        subtask_ids=[key["child"]],
    )
    assert extras[key["child"]] == ReminderExtras(parent_id=key["parent"])
    # A positive delta fires after the due date, which is not an early reminder.
    assert extras[key["plain"]].early_reminders == ["after-due:1h"]


def test_read_extras_reads_a_store_from_before_urgent_reminders(tmp_path: Path, monkeypatch):
    ids = _write_extras_store(tmp_path / "Data-local.sqlite", urgent_column=False)
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path)

    assert reminders_db.read_extras()[str(ids["parent"]).upper()].urgent is False


def test_read_extras_reports_a_store_with_an_unknown_schema(tmp_path: Path, monkeypatch):
    connection = sqlite3.connect(tmp_path / "Data-ABC.sqlite")
    connection.execute("CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(reminders_db, "STORES_DIR", tmp_path)

    with pytest.raises(PermissionError, match="Data-ABC.sqlite"):
        reminders_db.read_extras()


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30m", (30, "m")), ("2h", (2, "h")), ("1d", (1, "d")), ("1w", (1, "w")), ("1mo", (1, "mo")), ("12mo", (12, "mo"))],
)
def test_parse_early_reminder(text, expected):
    assert reminderkit.parse_early_reminder(text) == expected


@pytest.mark.parametrize("text", ["0d", "-1d", "1", "d", "1 day", "1D", "1.5h", "none", ""])
def test_parse_early_reminder_refuses_anything_else(text):
    with pytest.raises(ValueError, match="invalid early_reminder"):
        reminderkit.parse_early_reminder(text)


def test_reminderkit_problems_is_empty_on_this_macos():
    assert reminderkit.reminderkit_problems() == []


def test_reminderkit_problems_names_a_missing_selector_and_class(monkeypatch):
    monkeypatch.setattr(
        reminderkit,
        "_REQUIRED_SELECTORS",
        {"REMSaveRequest": [b"saveSynchronouslyWithError:", b"renamedAway:"], "REMGoneClass": []},
    )
    monkeypatch.setattr(reminderkit, "_REQUIRED_CLASS_SELECTORS", {"REMObjectID": [b"objectIDWithURL:", b"goneFactory:"]})

    problems = reminderkit.reminderkit_problems()

    assert len(problems) == 1
    assert "-[REMSaveRequest renamedAway:]" in problems[0]
    assert "+[REMObjectID goneFactory:]" in problems[0]
    assert "objectIDWithURL" not in problems[0]
    assert "REMGoneClass" in problems[0]
    assert "saveSynchronouslyWithError" not in problems[0]


def test_eventkit_permission_problems_include_reminderkit_and_database_schema(monkeypatch):
    monkeypatch.setattr(eventkit, "_permission_problem", lambda entity_type: None)
    monkeypatch.setattr(eventkit, "automation_problem", lambda bundle_id, name: None)
    monkeypatch.setattr(eventkit, "reminderkit_problems", lambda: ["ReminderKit private API changed"])
    monkeypatch.setattr(eventkit, "read_flags", dict)

    def unreadable():
        raise PermissionError("Reminders database is not readable (Data-ABC.sqlite: no such column)")

    monkeypatch.setattr(eventkit, "read_extras", unreadable)

    assert eventkit.eventkit_permission_problems() == [
        "ReminderKit private API changed",
        "Reminders database is not readable (Data-ABC.sqlite: no such column)",
    ]
