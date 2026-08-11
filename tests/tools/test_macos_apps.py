"""Tests for macOS personal-app tool helpers."""

import base64
import builtins
import dis
from datetime import datetime
import importlib
import json
import pkgutil
from pathlib import Path
import subprocess
from types import CodeType
from unittest.mock import MagicMock

import pytest

from lincy.tools.builtin.macos_apps import (
    MacOSAppBridge,
    _applescript_utf8_file_read,
    _build_note_html,
    _datetime_to_app_iso,
    _ensure_note_title_html,
    _format_app_tool_log_details,
    _html_to_markdown,
    _localize_calendar_datetime_fields,
    _localize_mail_datetime_fields,
    _localize_reminder_datetime_fields,
    _render_note_template_html,
    create_calendar_tool,
    create_mail_tool,
    create_notes_tool,
    create_photos_tool,
    create_reminders_tool,
)


def test_calendar_tool_get_delegates_to_bridge():
    bridge = MagicMock()
    bridge.calendar_get.return_value = {"ok": True, "event": {"uid": "evt-1"}}

    tool = create_calendar_tool(bridge)
    payload = json.loads(tool(action="get", event_uid="evt-1"))

    assert payload["event"]["uid"] == "evt-1"
    bridge.calendar_get.assert_called_once_with(event_uid="evt-1", calendar=None)


def test_calendar_tool_conflicts_requires_range():
    bridge = MagicMock()
    tool = create_calendar_tool(bridge)

    result = tool(action="conflicts", start="2026-04-20T15:00")

    assert result == "Error: 'start' and 'end' are required for conflicts"
    bridge.calendar_conflicts.assert_not_called()


def test_calendar_tool_rejects_invalid_time_range():
    bridge = MagicMock()
    tool = create_calendar_tool(bridge)

    result = tool(
        action="create",
        calendar="Work",
        title="Lecture",
        start="2026-04-20T15:00",
        end="2026-04-20T14:00",
    )

    assert result == "Error: 'end' must be after or equal to 'start'"
    bridge.calendar_create.assert_not_called()


def test_calendar_tool_accepts_mixed_offset_time_range():
    bridge = MagicMock()
    bridge.calendar_create.return_value = {"ok": True, "event": {"uid": "evt-1"}}
    tool = create_calendar_tool(bridge)

    result = tool(
        action="create",
        calendar="Work",
        title="Lecture",
        start="2026-04-20T15:00:00+08:00",
        end="2026-04-20T16:00",
    )

    assert json.loads(result)["ok"] is True
    bridge.calendar_create.assert_called_once()


def test_calendar_datetime_to_app_iso_attaches_configured_timezone():
    assert (
        _datetime_to_app_iso(datetime(2026, 5, 6, 19, 0))
        == "2026-05-06T19:00:00+08:00"
    )


def test_calendar_output_localizes_utc_event_times():
    result = _localize_calendar_datetime_fields(
        {
            "ok": True,
            "event": {
                "start": "2026-05-06T11:00:00.000Z",
                "end": "2026-05-06T12:00:00.000Z",
            },
        }
    )

    assert result["event"]["start"] == "2026-05-06T19:00:00+08:00"
    assert result["event"]["end"] == "2026-05-06T20:00:00+08:00"


def test_calendar_create_uses_jxa_with_app_offset(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "uid": "event-1"}

    def fake_calendar_get(*, event_uid: str, calendar: str | None = None):
        return {
            "ok": True,
            "event": {"uid": event_uid, "calendar": calendar, "title": "Event"},
        }

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]
    bridge.calendar_get = fake_calendar_get  # type: ignore[method-assign]

    result = bridge.calendar_create(
        calendar="Work",
        title="Event",
        start=datetime(2026, 5, 6, 19, 0),
        end=datetime(2026, 5, 6, 20, 0),
        notes=None,
        location=None,
        url=None,
        all_day=None,
    )

    assert result["event"]["uid"] == "event-1"
    payload = captured["payload"]
    assert payload["start"] == "2026-05-06T19:00:00+08:00"
    assert payload["end"] == "2026-05-06T20:00:00+08:00"
    body = str(captured["body"])
    assert "const newEvent = app.Event(properties);" in body
    assert "calendar.events.push(newEvent);" in body


def test_calendar_update_uses_app_offset_and_ordered_date_sets(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured = {}
    get_calls = 0

    def fake_calendar_get(*, event_uid: str, calendar: str | None = None):
        nonlocal get_calls
        get_calls += 1
        return {
            "ok": True,
            "event": {
                "uid": event_uid,
                "calendar": calendar or "Work",
                "title": "Event",
                "start": "2040-01-01T10:00:00+08:00",
                "end": "2040-01-01T11:00:00+08:00",
            },
        }

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "uid": payload["event_uid"]}

    bridge.calendar_get = fake_calendar_get  # type: ignore[method-assign]
    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.calendar_update(
        event_uid="event-1",
        calendar="Work",
        title=None,
        start=datetime(2040, 1, 2, 3, 30),
        end=datetime(2040, 1, 2, 3, 45),
        notes=None,
        location=None,
        url=None,
        all_day=None,
    )

    assert result["ok"] is True
    assert get_calls == 2
    payload = captured["payload"]
    assert payload["start"] == "2040-01-02T03:30:00+08:00"
    assert payload["end"] == "2040-01-02T03:45:00+08:00"
    body = str(captured["body"])
    assert "const currentEnd = event.endDate();" in body
    assert "event.startDate.set(startDate);" in body
    assert "event.endDate.set(endDate);" in body


def test_calendar_search_normalizes_range_with_app_offset(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["payload"] = payload
        return {
            "ok": True,
            "results": [
                {
                    "uid": "event-1",
                    "start": "2026-05-06T11:00:00.000Z",
                    "end": "2026-05-06T12:00:00.000Z",
                }
            ],
        }

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.calendar_search(
        calendar="Work",
        calendars=None,
        query=None,
        start="2026-05-06T19:00",
        end="2026-05-06T20:00",
        all_day=None,
        sort_by=None,
        limit=10,
    )

    payload = captured["payload"]
    assert payload["start"] == "2026-05-06T19:00:00+08:00"
    assert payload["end"] == "2026-05-06T20:00:00+08:00"
    assert result["results"][0]["start"] == "2026-05-06T19:00:00+08:00"
    assert result["results"][0]["end"] == "2026-05-06T20:00:00+08:00"


def test_calendar_search_uses_date_filtered_events_when_range_present(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured: dict[str, object] = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "results": []}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.calendar_search(
        calendar=None,
        calendars=None,
        query=None,
        start="2026-05-08T00:00",
        end="2026-05-09T00:00",
        all_day=None,
        sort_by=None,
        limit=10,
    )

    assert result["ok"] is True
    body = str(captured["body"])
    assert 'dateFilter.endDate = { ">": start };' in body
    assert 'dateFilter.startDate = { "<": end };' in body
    assert "cal.events.whose(dateFilter)()" in body


def test_calendar_search_reports_truncation_when_limit_is_hit(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured: dict[str, object] = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.calendar_search(
        calendar="Work",
        calendars=None,
        query=None,
        start=None,
        end=None,
        all_day=None,
        sort_by=None,
        limit=10,
    )

    assert result == {"ok": True}
    body = str(captured["body"])
    assert "const scanLimit = limit + 1;" in body
    assert "results = results.slice(0, limit);" in body
    assert "truncated" in body
    assert "warning: truncated ?" in body


def test_mail_tool_search_delegates_without_account_or_mailbox_path():
    bridge = MagicMock()
    bridge.mail_search.return_value = {"ok": True, "results": []}
    tool = create_mail_tool(bridge)

    result = json.loads(
        tool(
            action="search",
            scope="all",
            query="invoice",
            date_after="2026-05-01",
            date_before="2026-05-05",
            unread=True,
            scan_limit=300,
            limit=20,
        )
    )

    assert result["ok"] is True
    bridge.mail_search.assert_called_once_with(
        scope="all",
        query="invoice",
        search_body=False,
        date_after="2026-05-01",
        date_before="2026-05-05",
        unread=True,
        flagged=None,
        has_attachments=None,
        scan_limit=300,
        limit=20,
        offset=None,
    )


def test_mail_tool_rejects_reversed_local_date_range():
    bridge = MagicMock()
    tool = create_mail_tool(bridge)

    result = tool(
        action="search",
        date_after="2026-05-06T10:00",
        date_before="2026-05-06T09:00",
    )

    assert result == "Error: 'date_before' must be after or equal to 'date_after'"
    bridge.mail_search.assert_not_called()


def test_mail_search_normalizes_range_with_app_offset(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["payload"] = payload
        return {
            "ok": True,
            "results": [
                {
                    "message_ref": "mailmsg:1",
                    "date": "2026-05-06T11:00:00.000Z",
                    "date_received": "2026-05-06T11:00:00.000Z",
                }
            ],
        }

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.mail_search(
        scope="inbox",
        query=None,
        search_body=False,
        date_after="2026-05-06",
        date_before="2026-05-06",
        unread=None,
        flagged=None,
        has_attachments=None,
        scan_limit=300,
        limit=10,
        offset=None,
    )

    payload = captured["payload"]
    assert payload["date_after"] == "2026-05-06T00:00:00+08:00"
    assert payload["date_before"] == "2026-05-06T23:59:59+08:00"
    assert result["results"][0]["date"] == "2026-05-06T19:00:00+08:00"
    assert result["results"][0]["date_received"] == "2026-05-06T19:00:00+08:00"


def test_mail_tool_trash_defaults_to_dry_run():
    bridge = MagicMock()
    bridge.mail_trash.return_value = {"ok": True, "dry_run": True}
    tool = create_mail_tool(bridge)

    result = json.loads(tool(action="trash", message_ref="mailmsg:1"))

    assert result["dry_run"] is True
    bridge.mail_trash.assert_called_once_with(
        message_refs=["mailmsg:1"],
        dry_run=True,
    )


def test_mail_tool_trash_limits_batch_size():
    bridge = MagicMock()
    tool = create_mail_tool(bridge)

    result = tool(
        action="trash",
        message_refs=[f"mailmsg:{index}" for index in range(21)],
    )

    assert result == "Error: trash accepts at most 20 messages"
    bridge.mail_trash.assert_not_called()


def test_prepare_mail_export_dir_rejects_path_outside_allowed_paths(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=1,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
        mail_export_dir="tmp/mail-attachments",
    )

    with pytest.raises(ValueError, match="outside allowed paths"):
        bridge._prepare_mail_export_dir("/etc")


def test_mail_output_localizes_utc_message_times():
    result = _localize_mail_datetime_fields(
        {
            "ok": True,
            "message": {
                "date": "2026-05-06T11:00:00.000Z",
                "date_received": "2026-05-06T11:00:00.000Z",
                "date_sent": "2026-05-06T10:30:00.000Z",
            },
        }
    )

    assert result["message"]["date"] == "2026-05-06T19:00:00+08:00"
    assert result["message"]["date_received"] == "2026-05-06T19:00:00+08:00"
    assert result["message"]["date_sent"] == "2026-05-06T18:30:00+08:00"


def test_reminders_tool_get_requires_id():
    bridge = MagicMock()
    tool = create_reminders_tool(bridge)

    result = tool(action="get")

    assert result == "Error: 'reminder_id' is required for get"
    bridge.reminders_get.assert_not_called()


def _make_bridge(tmp_path: Path) -> MacOSAppBridge:
    return MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )


def test_run_jxa_json_slow_path_keeps_successful_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    bridge = _make_bridge(tmp_path)
    calls = iter([0.0, 5.0])
    monkeypatch.setattr(
        "lincy.tools.builtin.macos_apps.bridge.time.monotonic",
        lambda: next(calls),
    )
    monkeypatch.setattr(
        "lincy.tools.builtin.macos_apps.bridge.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        ),
    )

    assert bridge._run_jxa_json("return { ok: true };", operation="test") == {"ok": True}


def test_macos_app_modules_have_no_missing_global_references():
    package_name = "lincy.tools.builtin.macos_apps"
    package = importlib.import_module(package_name)

    def global_loads(code: CodeType):
        for instruction in dis.get_instructions(code):
            if instruction.opname == "LOAD_GLOBAL":
                yield instruction.argval
        for value in code.co_consts:
            if isinstance(value, CodeType):
                yield from global_loads(value)

    modules = [
        package,
        *(
            importlib.import_module(module_info.name)
            for module_info in pkgutil.iter_modules(package.__path__, f"{package_name}.")
        ),
    ]
    missing_by_module: dict[str, list[str]] = {}
    for module in modules:
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = compile(source, module.__file__, "exec")
        missing = sorted(
            {
                name
                for name in global_loads(code)
                if name not in module.__dict__ and not hasattr(builtins, name)
            }
        )
        if missing:
            missing_by_module[module.__name__] = missing

    assert missing_by_module == {}


def test_reminders_output_localizes_utc_due():
    result = _localize_reminder_datetime_fields(
        {
            "ok": True,
            "reminder": {"due": "2026-05-06T11:00:00.000Z"},
        }
    )

    assert result["reminder"]["due"] == "2026-05-06T19:00:00+08:00"


def test_reminders_create_uses_jxa_with_app_offset(tmp_path: Path):
    bridge = _make_bridge(tmp_path)
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "reminder_id": "rem-1"}

    def fake_resolve_list_spec(**kwargs):
        return {
            "ok": True,
            "list_id": "list-1",
            "list_name": "Inbox",
            "list_path": "iCloud/Inbox",
        }

    def fake_reminders_get(*, reminder_id: str):
        return {"ok": True, "reminder": {"id": reminder_id}}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]
    bridge._resolve_list_spec = fake_resolve_list_spec  # type: ignore[method-assign]
    bridge.reminders_get = fake_reminders_get  # type: ignore[method-assign]

    result = bridge.reminders_create(
        list_id="list-1",
        list_name=None,
        list_path=None,
        title="體檢",
        notes=None,
        due=datetime(2026, 9, 1, 9, 0),
        priority=None,
        flagged=None,
    )

    assert result["reminder"]["id"] == "rem-1"
    payload = captured["payload"]
    assert payload["due"] == "2026-09-01T09:00:00+08:00"
    assert payload["list_id"] == "list-1"
    body = str(captured["body"])
    assert "const newReminder = app.Reminder(properties);" in body
    assert "list.reminders.push(newReminder);" in body
    assert "new Date(payload.due)" in body


def test_reminders_create_converts_aware_due_to_app_offset(tmp_path: Path):
    bridge = _make_bridge(tmp_path)
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["payload"] = payload
        return {"ok": True, "reminder_id": "rem-1"}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]
    bridge._resolve_list_spec = lambda **kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "list_id": "list-1",
        "list_name": "Inbox",
        "list_path": "iCloud/Inbox",
    }
    bridge.reminders_get = lambda *, reminder_id: {  # type: ignore[method-assign]
        "ok": True,
        "reminder": {"id": reminder_id},
    }

    bridge.reminders_create(
        list_id="list-1",
        list_name=None,
        list_path=None,
        title="體檢",
        notes=None,
        due=datetime.fromisoformat("2026-09-01T01:00:00+00:00"),
        priority=None,
        flagged=None,
    )

    assert captured["payload"]["due"] == "2026-09-01T09:00:00+08:00"


def test_reminders_update_uses_jxa_with_app_offset(tmp_path: Path):
    bridge = _make_bridge(tmp_path)
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["body"] = body
        captured["payload"] = payload
        return {"ok": True, "reminder_id": "rem-1"}

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]
    bridge.reminders_get = lambda *, reminder_id: {  # type: ignore[method-assign]
        "ok": True,
        "reminder": {"id": reminder_id},
    }

    result = bridge.reminders_update(
        reminder_id="rem-1",
        title=None,
        notes=None,
        due=datetime(2026, 9, 1, 9, 0),
        priority=None,
        flagged=None,
        completed=None,
    )

    assert result["reminder"]["id"] == "rem-1"
    payload = captured["payload"]
    assert payload["due"] == "2026-09-01T09:00:00+08:00"
    assert payload["has_title"] is False
    body = str(captured["body"])
    assert "reminder.dueDate.set(dueDate);" in body


def test_reminders_search_normalizes_due_range_with_app_offset(tmp_path: Path):
    bridge = _make_bridge(tmp_path)
    captured = {}

    def fake_run_jxa_json(
        body: str,
        *,
        payload: dict[str, object] | None = None,
        **kwargs,
    ):
        captured["payload"] = payload
        return {
            "ok": True,
            "results": [{"due": "2026-09-01T01:00:00.000Z"}],
            "count": 1,
        }

    bridge._run_jxa_json = fake_run_jxa_json  # type: ignore[method-assign]

    result = bridge.reminders_search(
        list_id=None,
        list_name=None,
        list_path=None,
        query=None,
        due_start="2026-09-01T00:00",
        due_end="2026-09-01T23:59",
        completed=None,
        flagged=None,
        priority_min=None,
        priority_max=None,
        sort_by=None,
        limit=None,
    )

    payload = captured["payload"]
    assert payload["due_start"] == "2026-09-01T00:00:00+08:00"
    assert payload["due_end"] == "2026-09-01T23:59:00+08:00"
    assert result["results"][0]["due"] == "2026-09-01T09:00:00+08:00"


def test_notes_tool_create_requires_explicit_folder():
    bridge = MagicMock()
    tool = create_notes_tool(bridge)

    result = tool(action="create", body="hello")

    assert result == "Error: 'folder_id' or 'folder_path' is required for create"
    bridge.notes_create.assert_not_called()


def test_notes_tool_move_requires_target_folder():
    bridge = MagicMock()
    tool = create_notes_tool(bridge)

    result = tool(action="move", note_id="note-1")

    assert result == "Error: 'target_folder_id' or 'target_folder_path' is required for move"
    bridge.notes_move.assert_not_called()


def test_notes_tool_create_accepts_template_markdown():
    bridge = MagicMock()
    bridge.notes_create.return_value = {"ok": True}
    tool = create_notes_tool(bridge)

    result = json.loads(
        tool(
            action="create",
            folder_path="iCloud/待讀",
            template_markdown="# {paper_title}\n{image_cover}\n{summary}",
            variables={"paper_title": "多目標追蹤模型", "summary": "這是一篇摘要"},
            images={"image_cover": "/tmp/cover.png"},
        )
    )

    assert result["ok"] is True
    bridge.notes_create.assert_called_once_with(
        folder_id=None,
        folder_path="iCloud/待讀",
        title=None,
        body=None,
        template_markdown="# {paper_title}\n{image_cover}\n{summary}",
        variables={"paper_title": "多目標追蹤模型", "summary": "這是一篇摘要"},
        images={"image_cover": "/tmp/cover.png"},
    )


def test_photos_tool_get_album_requires_album_id():
    bridge = MagicMock()
    tool = create_photos_tool(bridge)

    result = tool(action="get_album")

    assert result == "Error: 'album_id', 'album_name', or 'album_path' is required for get_album"


def test_photos_tool_get_media_requires_ids():
    bridge = MagicMock()
    tool = create_photos_tool(bridge)

    result = tool(action="get_media")

    assert result == "Error: 'media_ids' is required for get_media"
    bridge.photos_get_media.assert_not_called()


def test_prepare_export_dir_uses_default_root(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=1,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )

    export_dir = bridge._prepare_export_dir(None)

    assert export_dir.is_absolute()
    export_dir.relative_to(tmp_path.resolve())


def test_prepare_export_dir_rejects_path_outside_allowed_paths(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=1,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )

    with pytest.raises(ValueError, match="outside allowed paths"):
        bridge._prepare_export_dir("/etc")


def test_run_applescript_utf8_files_preserves_non_ascii(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )

    result = bridge._run_applescript(
        f"return {_applescript_utf8_file_read('NOTE_BODY')}\n",
        utf8_files={"NOTE_BODY": "中文測試abc"},
    )

    assert result == "中文測試abc"


def test_run_applescript_utf8_files_accepts_empty_text(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )

    result = bridge._run_applescript(
        f"return {_applescript_utf8_file_read('EMPTY_TEXT')}\n",
        utf8_files={"EMPTY_TEXT": ""},
    )

    assert result == ""


def test_run_applescript_utf8_files_work_inside_tell_blocks(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )

    result = bridge._run_applescript(
        (
            "script targetObject\n"
            "end script\n"
            "tell targetObject\n"
            f"  set valueText to {_applescript_utf8_file_read('NESTED_TEXT')}\n"
            "end tell\n"
            "return valueText\n"
        ),
        utf8_files={"NESTED_TEXT": "nested 中文"},
    )

    assert result == "nested 中文"


def test_app_tool_log_details_redacts_text_but_keeps_scope_fields():
    result = _format_app_tool_log_details(
        {
            "folder_path": "iCloud/備忘錄",
            "title": "這是標題",
            "body": "這是內容",
            "query": "中文查詢",
            "limit": 25,
        }
    )

    assert "folder_path='iCloud/備忘錄'" in result
    assert "limit=25" in result
    assert "title_chars=4" in result
    assert "body_chars=4" in result
    assert "query_chars=4" in result
    assert "這是標題" not in result


def test_render_note_template_html_supports_custom_variables_and_images(tmp_path: Path):
    image_path = tmp_path / "cover.png"
    image_path.write_bytes(b"png-test")

    html = _render_note_template_html(
        template_markdown=(
            "# {paper_title}\n"
            "來源：{url}\n\n"
            "## 原圖\n"
            "{image_cover}\n\n"
            "## 重點\n"
            "- {point_1}\n"
            "- {point_2}\n\n"
            "|欄位|值|\n"
            "|---|---|\n"
            "|作者|{author_name}|"
        ),
        variables={
            "paper_title": "多目標追蹤模型",
            "url": "https://x.com/example",
            "point_1": "支援任意檢測器",
            "point_2": "CLI 一行追蹤影片",
            "author_name": "Berryxia",
        },
        images={"image_cover": str(image_path)},
        allowed_paths=[str(tmp_path)],
        base_dir=tmp_path,
    )

    assert (
        '<div><h1 style="font-size: 15.0pt; font-weight: bold;">'
        "多目標追蹤模型</h1></div>"
    ) in html
    assert '<a href="https://x.com/example">https://x.com/example</a>' in html
    assert (
        '<div><h2 style="font-size: 13.5pt; font-weight: bold;">'
        "原圖</h2></div>"
    ) in html
    assert "data:image/png;base64," in html
    assert "<div>- 支援任意檢測器</div>" in html
    assert "<div>- CLI 一行追蹤影片</div>" in html
    assert "<table>" in html
    assert "Berryxia" in html


def test_render_note_template_html_supports_markdown_image_placeholder(tmp_path: Path):
    image_path = tmp_path / "cover.jpg"
    image_path.write_bytes(b"jpg-test")

    html = _render_note_template_html(
        template_markdown="圖片如下\n\n![封面](image_cover)",
        variables={},
        images={"image_cover": str(image_path)},
        allowed_paths=[str(tmp_path)],
        base_dir=tmp_path,
    )

    assert "<div>圖片如下</div>" in html
    assert 'alt="封面"' in html
    assert "data:image/jpeg;base64," in html


def test_build_note_html_linkifies_bare_urls():
    html = _build_note_html(None, "來源：https://x.com/example")

    assert '<a href="https://x.com/example">https://x.com/example</a>' in html


def test_render_note_template_html_renders_quote_and_plain_text_lists():
    html = _render_note_template_html(
        template_markdown=(
            "# 標題\n"
            "## 第二層\n"
            "### 第三層\n\n"
            "- 項目一\n"
            "- 項目二\n"
            "1. 第一個\n"
            "2. 第二個\n"
            "> 引言內容"
        ),
        variables={},
        images={},
        allowed_paths=["/tmp"],
        base_dir=Path("/tmp"),
    )

    assert (
        '<h1 style="font-size: 15.0pt; font-weight: bold;">標題</h1>'
    ) in html
    assert (
        '<h2 style="font-size: 13.5pt; font-weight: bold;">第二層</h2>'
    ) in html
    assert (
        '<h3 style="font-size: 12pt; font-weight: bold;">第三層</h3>'
    ) in html
    assert "<div><br></div>" in html
    assert "<div>- 項目一</div>" in html
    assert "<div>- 項目二</div>" in html
    assert "<div>1. 第一個</div>" in html
    assert "<div>2. 第二個</div>" in html
    assert '&gt; 引言內容' in html


def test_ensure_note_title_html_prepends_missing_title():
    html = _ensure_note_title_html(
        "<p>來源：https://x.com/example</p><h2>簡介</h2><p>摘要</p>",
        "多目標追蹤模型",
    )

    assert html.startswith(
        '<div><h1 style="font-size: 15.0pt; font-weight: bold;">多目標追蹤模型</h1></div><div><br></div>'
    )


def test_ensure_note_title_html_does_not_duplicate_existing_title():
    html = _ensure_note_title_html(
        "<h1>多目標追蹤模型</h1><p>來源：https://x.com/example</p>",
        "多目標追蹤模型",
    )

    assert html.count("多目標追蹤模型") == 1


def test_notes_create_template_keeps_title_as_first_visible_line(tmp_path: Path):
    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
    )
    bridge._resolve_note_folder = MagicMock(
        return_value={
            "ok": True,
            "folder_id": "folder-1",
            "folder_path": "iCloud/待讀",
        }
    )
    captured: dict[str, str] = {}

    def fake_run_applescript(script, *, env=None, utf8_files=None, **kwargs):
        captured["note_body"] = (utf8_files or {}).get("NOTE_BODY", "")
        return "note-1"

    bridge._run_applescript = fake_run_applescript  # type: ignore[method-assign]
    bridge.notes_get = MagicMock(return_value={"ok": True, "note": {"id": "note-1"}})

    payload = bridge.notes_create(
        folder_id=None,
        folder_path="iCloud/待讀",
        title="多目標追蹤模型",
        body=None,
        template_markdown="來源：{url}\n\n## 簡介\n{summary}",
        variables={
            "url": "https://x.com/example",
            "summary": "這是一篇摘要",
        },
        images={},
    )

    assert payload["ok"] is True
    assert captured["note_body"].startswith(
        '<div><h1 style="font-size: 15.0pt; font-weight: bold;">多目標追蹤模型</h1></div><div><br></div>'
    )


def test_html_to_markdown_restores_notes_normalized_headings():
    markdown = _html_to_markdown(
        "<div><b><span style=\"font-size: 20px\">主標題</span></b></div>"
        "<div><br></div>"
        "<div><b><span style=\"font-size: 18px\">簡介</span></b></div>"
        "<div><br></div>"
        "<div><b><span style=\"font-size: 16px\">小節</span></b></div>"
        "<div><br></div>"
        "<div>內文</div>"
    )

    assert markdown == "# 主標題\n\n## 簡介\n\n### 小節\n\n內文"


def test_notes_get_renders_markdown_and_embedded_image_summary(tmp_path: Path):
    class FakeVisionAgent:
        def describe(self, image_parts):
            assert image_parts[0].text
            assert image_parts[1].data
            return "講座海報，時間是下週三晚上七點"

    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
        vision_agent=FakeVisionAgent(),
    )
    image_data = base64.b64encode(b"fake-image-bytes").decode("ascii")
    bridge._notes_get_raw = MagicMock(
        return_value={
            "ok": True,
            "note": {
                "id": "note-1",
                "title": "講座筆記",
                "body_html": (
                    f'<div><a href="https://example.com/post">原文</a></div>'
                    f'<div><img src="data:image/png;base64,{image_data}"></div>'
                    "<div>這是一段說明</div>"
                ),
                "plaintext": "這是一段說明",
                "created_at": "2026-04-11T10:00:00Z",
                "modified_at": "2026-04-11T12:00:00Z",
                "shared": False,
                "password_protected": False,
                "account": "iCloud",
                "folder_id": "folder-1",
                "folder_path": "iCloud/待讀",
            },
        }
    )

    payload = bridge.notes_get(note_id="note-1")

    assert payload["ok"] is True
    assert "data:image/png;base64" not in payload["note"]["content_markdown"]
    assert "講座海報" in payload["note"]["content_markdown"]
    assert payload["note"]["has_images"] is True
    assert payload["note"]["source_url"] == "https://example.com/post"
    assert payload["note"]["content_kind"] == "web_clip_image"
    assert (tmp_path / "cache" / "apple_notes").is_dir()


def test_notes_search_uses_cached_markdown_summary_and_paging(tmp_path: Path):
    class FakeSummarizer:
        def chat(self, messages, response_schema=None, temperature=None):
            content = messages[1].content
            assert isinstance(content, str)
            first_line = next((line for line in content.splitlines() if line.startswith("標題：")), "")
            return f"摘要 {first_line.removeprefix('標題：')}".strip()

    bridge = MacOSAppBridge(
        base_dir=tmp_path,
        allowed_paths=[str(tmp_path)],
        timeout_seconds=5,
        max_search_results=10,
        photos_export_dir="tmp/photos-exports",
        notes_summarizer=FakeSummarizer(),
    )
    bridge._notes_list_candidates = MagicMock(
        return_value={
            "ok": True,
            "results": [
                {
                    "id": "note-1",
                    "title": "講座 A",
                    "created_at": "2026-04-11T10:00:00Z",
                    "modified_at": "2026-04-11T12:00:00Z",
                    "shared": False,
                    "password_protected": False,
                    "account": "iCloud",
                    "folder_id": "folder-1",
                    "folder_path": "iCloud/待讀",
                },
                {
                    "id": "note-2",
                    "title": "講座 B",
                    "created_at": "2026-04-11T11:00:00Z",
                    "modified_at": "2026-04-11T13:00:00Z",
                    "shared": False,
                    "password_protected": False,
                    "account": "iCloud",
                    "folder_id": "folder-1",
                    "folder_path": "iCloud/待讀",
                },
                {
                    "id": "note-3",
                    "title": "別的文章",
                    "created_at": "2026-04-11T09:00:00Z",
                    "modified_at": "2026-04-11T09:30:00Z",
                    "shared": False,
                    "password_protected": False,
                    "account": "iCloud",
                    "folder_id": "folder-1",
                    "folder_path": "iCloud/待讀",
                },
            ],
        }
    )
    raw_notes = {
        "note-1": {
            "ok": True,
            "note": {
                "id": "note-1",
                "title": "講座 A",
                "body_html": "<div>下週三講座，主講人小明</div>",
                "plaintext": "下週三講座，主講人小明",
                "created_at": "2026-04-11T10:00:00Z",
                "modified_at": "2026-04-11T12:00:00Z",
                "shared": False,
                "password_protected": False,
                "account": "iCloud",
                "folder_id": "folder-1",
                "folder_path": "iCloud/待讀",
            },
        },
        "note-2": {
            "ok": True,
            "note": {
                "id": "note-2",
                "title": "講座 B",
                "body_html": "<div>今天的講座重點整理</div>",
                "plaintext": "今天的講座重點整理",
                "created_at": "2026-04-11T11:00:00Z",
                "modified_at": "2026-04-11T13:00:00Z",
                "shared": False,
                "password_protected": False,
                "account": "iCloud",
                "folder_id": "folder-1",
                "folder_path": "iCloud/待讀",
            },
        },
        "note-3": {
            "ok": True,
            "note": {
                "id": "note-3",
                "title": "別的文章",
                "body_html": "<div>這是一篇軟體更新文章</div>",
                "plaintext": "這是一篇軟體更新文章",
                "created_at": "2026-04-11T09:00:00Z",
                "modified_at": "2026-04-11T09:30:00Z",
                "shared": False,
                "password_protected": False,
                "account": "iCloud",
                "folder_id": "folder-1",
                "folder_path": "iCloud/待讀",
            },
        },
    }
    bridge._notes_get_raw = MagicMock(side_effect=lambda note_id: raw_notes[note_id])

    payload = bridge.notes_search(
        account=None,
        folder_id=None,
        folder_path="iCloud/待讀",
        query="講座",
        created_after=None,
        created_before=None,
        modified_after=None,
        modified_before=None,
        sort_by="modified_desc",
        limit=1,
        offset=1,
    )

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["total_matches"] == 2
    assert payload["has_more"] is False
    assert payload["results"][0]["id"] == "note-1"
    assert payload["results"][0]["summary"].startswith("摘要")
