"""macOS personal-app tools for Calendar, Reminders, Notes, Photos, and Mail."""

from .bridge import _BridgeBase
from .calendar import CALENDAR_TOOL_DEFINITION, MacOSAppBridge as _CalendarBridge, create_calendar_tool
from .reminders import REMINDERS_TOOL_DEFINITION, MacOSAppBridge as _RemindersBridge, create_reminders_tool
from .notes import NOTES_TOOL_DEFINITION, MacOSAppBridge as _NotesBridge, create_notes_tool
from .photos import PHOTOS_TOOL_DEFINITION, MacOSAppBridge as _PhotosBridge, create_photos_tool
from .mail import MAIL_TOOL_DEFINITION, MacOSAppBridge as _MailBridge, create_mail_tool
from .runtime import (
    _datetime_to_app_iso as _datetime_to_app_iso,
    _format_app_tool_log_details as _format_app_tool_log_details,
    _localize_calendar_datetime_fields as _localize_calendar_datetime_fields,
    _localize_mail_datetime_fields as _localize_mail_datetime_fields,
    _localize_reminder_datetime_fields as _localize_reminder_datetime_fields,
)
from .notes_template import (
    _applescript_utf8_file_read as _applescript_utf8_file_read,
    _build_note_html as _build_note_html,
    _ensure_note_title_html as _ensure_note_title_html,
    _html_to_markdown as _html_to_markdown,
    _render_note_template_html as _render_note_template_html,
)

class MacOSAppBridge(
    _BridgeBase, _CalendarBridge, _RemindersBridge, _NotesBridge, _PhotosBridge, _MailBridge,
):
    """Bridge for macOS personal apps using JXA/AppleScript."""

    pass

__all__ = [
    "CALENDAR_TOOL_DEFINITION", "REMINDERS_TOOL_DEFINITION", "NOTES_TOOL_DEFINITION",
    "PHOTOS_TOOL_DEFINITION", "MAIL_TOOL_DEFINITION", "MacOSAppBridge",
    "create_calendar_tool", "create_reminders_tool", "create_notes_tool",
    "create_photos_tool", "create_mail_tool",
]
