"""Writes through the private ReminderKit framework for fields EventKit lacks.

EventKit has no API for the early reminder, urgent state, tags, the URL field,
image attachments, or subtasks. Reminders.app writes them through ReminderKit,
so this module calls the same classes. The API is undocumented and can change
with any macOS update; reminderkit_problems() checks every class and selector
used here so a change stops lincy at startup, not in the middle of a turn.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from AppKit import NSImageRep
from Foundation import NSURL, NSUUID
import objc

from .reminders_db import EARLY_REMINDER_UNITS

_FRAMEWORK_PATH = "/System/Library/PrivateFrameworks/ReminderKit.framework"
_EARLY_REMINDER_RE = re.compile(r"^([1-9]\d*)(mo|m|h|d|w)$")
_REQUIRED_CLASS_SELECTORS = {"REMObjectID": [b"objectIDWithURL:"]}
_REQUIRED_SELECTORS = {
    "REMStore": [b"fetchReminderWithObjectID:error:"],
    "REMSaveRequest": [
        b"initWithStore:",
        b"updateReminder:",
        b"addReminderWithTitle:toReminderSubtaskContextChangeItem:",
        b"saveSynchronouslyWithError:",
    ],
    "REMReminderChangeItem": [
        b"urgentAlarmContext",
        b"hashtagContext",
        b"attachmentContext",
        b"dueDateDeltaAlertContext",
        b"subtaskContext",
    ],
    "REMReminderUrgentAlarmContextChangeItem": [b"setIsUrgentStateEnabledForCurrentUser:"],
    "REMReminderHashtagContextChangeItem": [b"removeAllHashtags", b"addHashtagWithType:name:"],
    "REMReminderAttachmentContextChangeItem": [
        b"setURLAttachmentWithURL:",
        b"removeURLAttachments",
        b"addImageAttachmentWithURL:width:height:error:",
    ],
    "REMReminderDueDateDeltaAlertContextChangeItem": [
        b"removeDueDateDeltaAlertsWithIdentifiers:",
        b"addDueDateDeltaAlertWithDueDateDelta:",
    ],
    "REMDueDateDeltaInterval": [b"initWithUnit:count:"],
}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    objc.loadBundle("ReminderKit", {}, bundle_path=_FRAMEWORK_PATH)
    # Without bridge metadata pyobjc does not know these NSError** arguments are outputs.
    out = {"type_modifier": b"o"}
    objc.registerMetaDataForSelector(b"REMStore", b"fetchReminderWithObjectID:error:", {"arguments": {3: out}})
    objc.registerMetaDataForSelector(b"REMSaveRequest", b"saveSynchronouslyWithError:", {"arguments": {2: out}})
    objc.registerMetaDataForSelector(
        b"REMReminderAttachmentContextChangeItem",
        b"addImageAttachmentWithURL:width:height:error:",
        {"arguments": {5: out}},
    )
    _loaded = True


def reminderkit_problems() -> list[str]:
    """Return the ReminderKit classes and selectors this module needs but macOS lacks."""
    try:
        _load()
    except ImportError as exc:
        return [f"ReminderKit private framework could not be loaded ({exc}); reminders_tool cannot write its fields."]
    missing = []
    for sign, required in (("+", _REQUIRED_CLASS_SELECTORS), ("-", _REQUIRED_SELECTORS)):
        for class_name, selectors in required.items():
            try:
                cls = objc.lookUpClass(class_name)
            except objc.nosuchclass_error:
                missing.append(class_name)
                continue
            responds = cls.respondsToSelector_ if sign == "+" else cls.instancesRespondToSelector_
            missing.extend(f"{sign}[{class_name} {selector.decode()}]" for selector in selectors if not responds(selector))
    if not missing:
        return []
    return [
        "ReminderKit private API changed on this macOS; missing: "
        + ", ".join(missing)
        + ". Update src/lincy/tools/builtin/macos_apps/reminderkit.py before running lincy."
    ]


def parse_early_reminder(value: str) -> tuple[int, str]:
    """Split an early reminder like '2d' into (amount, unit); raise ValueError otherwise."""
    match = _EARLY_REMINDER_RE.match(value)
    if match is None:
        raise ValueError(
            f"invalid early_reminder {value!r}: use a positive amount and a unit, "
            "e.g. '30m', '2h', '1d', '1w', '1mo'; use clear=['early_reminder'] to remove it"
        )
    return int(match.group(1)), match.group(2)


def image_size(path: Path) -> tuple[int, int]:
    """Return the pixel size of an image file; raise ValueError if it cannot be decoded."""
    rep = NSImageRep.imageRepWithContentsOfFile_(str(path))
    if rep is None or rep.pixelsWide() <= 0 or rep.pixelsHigh() <= 0:
        raise ValueError(f"not a readable image: {path}")
    return rep.pixelsWide(), rep.pixelsHigh()


def write_private_fields(
    reminder_id: str,
    *,
    urgent: bool | None,
    tags: list[str] | None,
    url: str | None,
    early_reminder: str | None,
    early_reminder_ids: list[str],
    subtasks: list[str] | None,
    images: list[Path] | None,
) -> None:
    """Apply the given fields to a saved reminder in one ReminderKit save.

    None leaves a field alone. tags replaces the whole tag list, an empty url
    removes the URL, early_reminder 'none' removes the early reminder, and
    subtasks and images are appended. early_reminder_ids are the reminder's
    current early reminder alerts as read from the database.
    """
    _load()
    # The CloudKit identifier in the object URL equals the EventKit
    # calendarItemIdentifier (checked on every reminder of the lincy account).
    object_url = NSURL.URLWithString_(f"x-apple-reminderkit://REMCDReminder/{reminder_id}")
    store = objc.lookUpClass("REMStore").alloc().init()
    reminder, error = store.fetchReminderWithObjectID_error_(
        objc.lookUpClass("REMObjectID").objectIDWithURL_(object_url), None
    )
    if reminder is None:
        raise RuntimeError(f"ReminderKit could not fetch the reminder: {_describe(error)}")
    save = objc.lookUpClass("REMSaveRequest").alloc().initWithStore_(store)
    change = save.updateReminder_(reminder)

    if urgent is not None:
        change.urgentAlarmContext().setIsUrgentStateEnabledForCurrentUser_(urgent)
    if tags is not None:
        hashtags = change.hashtagContext()
        hashtags.removeAllHashtags()
        for tag in tags:
            hashtags.addHashtagWithType_name_(0, tag)
    if url is not None:
        if url:
            change.attachmentContext().setURLAttachmentWithURL_(NSURL.URLWithString_(url))
        else:
            change.attachmentContext().removeURLAttachments()
    if early_reminder is not None:
        alerts = change.dueDateDeltaAlertContext()
        # removeAllFetchedDueDateDeltaAlerts does nothing after a plain fetch
        # (measured: the old alert stayed and a second one was added), so the
        # existing alerts are removed by identifier.
        if early_reminder_ids:
            alerts.removeDueDateDeltaAlertsWithIdentifiers_(
                [NSUUID.alloc().initWithUUIDString_(identifier) for identifier in early_reminder_ids]
            )
        if early_reminder != "none":
            amount, unit = parse_early_reminder(early_reminder)
            # The count is signed and added to the due date: negative is before
            # it. A positive count shows in Reminders.app as "Custom / None".
            alerts.addDueDateDeltaAlertWithDueDateDelta_(
                objc.lookUpClass("REMDueDateDeltaInterval")
                .alloc()
                .initWithUnit_count_(EARLY_REMINDER_UNITS.index(unit), -amount)
            )
    for title in subtasks or []:
        save.addReminderWithTitle_toReminderSubtaskContextChangeItem_(title, change.subtaskContext())
    for path in images or []:
        width, height = image_size(path)
        attachment, error = change.attachmentContext().addImageAttachmentWithURL_width_height_error_(
            NSURL.fileURLWithPath_(str(path)), width, height, None
        )
        if attachment is None:
            raise RuntimeError(f"ReminderKit could not attach {path.name}: {_describe(error)}")

    saved, error = save.saveSynchronouslyWithError_(None)
    if not saved:
        raise RuntimeError(f"ReminderKit save failed: {_describe(error)}")


def _describe(error: Any) -> str:
    return str(error.localizedDescription()) if error is not None else "no error detail"
