"""Tests for macOS personal-app tool helpers."""

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
