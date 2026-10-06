"""macOS permission checks for the GUI backend.

TCC grants are per executable: a rebuilt venv or upgraded Python is a new
program to TCC, so the executable that last passed is recorded and compared.
"""

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

_RECORD_FILE = "gui_permissions.json"
_TERMINAL_NOTE = (
    "When run from Terminal, TCC checks Terminal's permission instead of this "
    "process; the check under the launchd service is authoritative."
)


def accessibility_granted(*, prompt: bool = False) -> bool:
    import ApplicationServices as AS

    return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: prompt}))


def screen_recording_granted(*, request: bool = False) -> bool:
    import Quartz

    if request:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def keyboard_navigation_enabled() -> bool:
    """Full keyboard access (Tab moves through all controls)."""
    result = subprocess.run(
        ["defaults", "read", "-g", "AppleKeyboardUIMode"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() in ("2", "3")


def secure_input_enabled() -> bool:
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.IsSecureEventInputEnabled.restype = ctypes.c_ubyte
    return bool(carbon.IsSecureEventInputEnabled())


def check_gui_permissions(state_dir: Path) -> list[str]:
    """Return blocking permission problems; empty means the GUI backend can run.

    Keyboard navigation and Secure Input are hints only and not checked here.
    """
    record = state_dir / _RECORD_FILE
    # The venv python is a symlink that survives rebuilds; TCC keys on the
    # resolved binary, which is also the path to grant in System Settings.
    executable = os.path.realpath(sys.executable)
    missing = []
    if not accessibility_granted():
        missing.append(("Accessibility", "Accessibility"))
    if not screen_recording_granted():
        missing.append(("Screen Recording", "Screen & System Audio Recording"))
    if not missing:
        state_dir.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"executable": executable}) + "\n", encoding="utf-8")
        return []

    previous = ""
    if record.exists():
        previous = json.loads(record.read_text(encoding="utf-8")).get("executable", "")
    changed_note = ""
    if previous and previous != executable:
        changed_note = (
            f" Permissions were last granted to {previous}; after a venv rebuild "
            "or Python upgrade TCC treats the executable as a new program, so "
            "grant them again."
        )
    return [
        f"{name} permission is not granted to {executable}. Grant it in System "
        f"Settings > Privacy & Security > {pane}.{changed_note} {_TERMINAL_NOTE}"
        for name, pane in missing
    ]
