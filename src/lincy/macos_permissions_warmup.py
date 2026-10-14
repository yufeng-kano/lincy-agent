"""Warm up macOS permission prompts for Apple app tools."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import subprocess
import sys
import threading
from typing import Sequence

import EventKit
import Photos

from .tools.builtin.macos_apps.eventkit import eventkit_permission_problems
from .tools.builtin.macos_apps.mail_store import mail_permission_problems
from .tools.builtin.macos_apps.notes_store import notes_permission_problems
from .tools.builtin.macos_apps.photos import photos_permission_problems


@dataclass(frozen=True)
class WarmupTask:
    """One safe permission probe."""

    name: str
    app_name: str
    script_body: str


_EVENTKIT_ENTITIES = {
    EventKit.EKEntityTypeEvent: ("calendar", "Calendar"),
    EventKit.EKEntityTypeReminder: ("reminders", "Reminders"),
}


@dataclass(frozen=True)
class WarmupResult:
    """Permission probe result."""

    name: str
    app_name: str
    ok: bool
    detail: str


def _wrap_jxa(body: str) -> str:
    return f"""
function main() {{
{body}
}}
JSON.stringify(main());
"""


def _build_tasks() -> list[WarmupTask]:
    # Calendar and Photos are read through EventKit / PhotoKit only, so they
    # need no Apple Events grant; Reminders still writes `flagged` through JXA.
    return [
        WarmupTask(
            name="reminders",
            app_name="Reminders",
            script_body="""
const app = Application("Reminders");
return { ok: true, count: app.lists().length };
""",
        ),
        WarmupTask(
            name="notes",
            app_name="Notes",
            script_body="""
const app = Application("Notes");
return { ok: true, count: app.accounts().length };
""",
        ),
        WarmupTask(
            name="mail",
            app_name="Mail",
            script_body="""
const app = Application("Mail");
return {
  ok: true,
  account_count: app.accounts().length,
  inbox_exists: app.inbox.exists(),
};
""",
        ),
    ]


def _request_eventkit_access(entity_type: int, *, timeout: float) -> WarmupResult:
    name, app_name = _EVENTKIT_ENTITIES[entity_type]
    done = threading.Event()
    outcome: dict[str, object] = {}

    def completion(granted: bool, error: object) -> None:
        outcome["granted"] = bool(granted)
        outcome["error"] = error
        done.set()

    store = EventKit.EKEventStore.alloc().init()
    if entity_type == EventKit.EKEntityTypeEvent:
        store.requestFullAccessToEventsWithCompletion_(completion)
    else:
        store.requestFullAccessToRemindersWithCompletion_(completion)
    if not done.wait(timeout):
        return WarmupResult(name, f"{app_name} full access", False, f"timed out after {timeout:.1f}s")
    if outcome["granted"]:
        return WarmupResult(name, f"{app_name} full access", True, "granted")
    return WarmupResult(name, f"{app_name} full access", False, f"not granted ({outcome['error']})")


def _request_photos_access(*, timeout: float) -> WarmupResult:
    done = threading.Event()
    outcome: dict[str, int] = {}

    def handler(status: int) -> None:
        outcome["status"] = status
        done.set()

    Photos.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(
        Photos.PHAccessLevelReadWrite, handler,
    )
    if not done.wait(timeout):
        return WarmupResult("photos", "Photos library access", False, f"timed out after {timeout:.1f}s")
    if outcome["status"] == Photos.PHAuthorizationStatusAuthorized:
        return WarmupResult("photos", "Photos library access", True, "granted")
    return WarmupResult("photos", "Photos library access", False, f"status={outcome['status']}")


def _run_task(task: WarmupTask, *, timeout: float) -> WarmupResult:
    try:
        completed = subprocess.run(
            ["osascript", "-l", "JavaScript"],
            input=_wrap_jxa(task.script_body),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WarmupResult(
            name=task.name,
            app_name=task.app_name,
            ok=False,
            detail=f"timed out after {timeout:.1f}s",
        )

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return WarmupResult(
            name=task.name,
            app_name=task.app_name,
            ok=False,
            detail=detail or "osascript failed",
        )

    output = completed.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {"raw": output}
    detail = ", ".join(f"{key}={value}" for key, value in sorted(payload.items()))
    return WarmupResult(
        name=task.name,
        app_name=task.app_name,
        ok=True,
        detail=detail or "ok",
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="permissions-warmup",
        description="Trigger safe macOS permission prompts for Lincy Apple app tools.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for each app permission probe. Default: 60.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List permission probes without running them.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    tasks = _build_tasks()

    if args.list:
        for name, app_name in _EVENTKIT_ENTITIES.values():
            print(f"{name}: {app_name} full access (EventKit)")
        print("photos: Photos library access (PhotoKit)")
        for task in tasks:
            print(f"{task.name}: {task.app_name} automation (Apple Events)")
        return 0

    if sys.platform != "darwin":
        print("Error: permissions-warmup is only supported on macOS.", file=sys.stderr)
        return 2

    print("macOS permissions warmup")
    print("Approve the permission dialogs for the apps you want Lincy to use.")
    print("This command only reads small metadata; it does not write, send, or delete.")
    print("Full Disk Access cannot be requested; grant it manually if reported below.")
    print()

    results = [
        _request_eventkit_access(entity_type, timeout=args.timeout)
        for entity_type in _EVENTKIT_ENTITIES
    ]
    results.append(_request_photos_access(timeout=args.timeout))
    results.extend(_run_task(task, timeout=args.timeout) for task in tasks)
    for result in results:
        status = "ok" if result.ok else "failed"
        print(f"{result.app_name}: {status} ({result.detail})")

    # Full Disk Access (Notes, Mail, Photos and Reminders databases) has no
    # prompt API, so the same checks the validate stage runs are printed here.
    problems = [
        *eventkit_permission_problems(),
        *photos_permission_problems(),
        *notes_permission_problems(),
        *mail_permission_problems(),
    ]
    failed = [result for result in results if not result.ok]
    if failed or problems:
        print()
        for problem in problems:
            print(f"- {problem}")
        print("Some permissions are missing.")
        print("Open System Settings > Privacy & Security and allow the app running this command.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
