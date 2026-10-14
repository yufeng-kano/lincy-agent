"""Check Automation (Apple Events) grants without ever showing a prompt.

Reminders `flagged` writes, Notes reads/writes and Mail trash still go
through JXA/AppleScript, so a missing Automation grant would fail those calls
at runtime. AEDeterminePermissionToAutomateTarget answers from TCC directly.
"""

from __future__ import annotations

import ctypes

_CORE_SERVICES = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


_CORE_SERVICES.AECreateDesc.argtypes = [
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_AEDesc),
]
_CORE_SERVICES.AECreateDesc.restype = ctypes.c_int16
_CORE_SERVICES.AEDeterminePermissionToAutomateTarget.argtypes = [
    ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool,
]
_CORE_SERVICES.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
_CORE_SERVICES.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
_CORE_SERVICES.AEDisposeDesc.restype = ctypes.c_int16

_TYPE_APPLICATION_BUNDLE_ID = int.from_bytes(b"bund", "big")
_TYPE_WILDCARD = int.from_bytes(b"****", "big")

_PROC_NOT_FOUND = -600
_CODE_MEANINGS = {
    -1743: "denied",
    -1744: "never asked",
}


def _permission_status(bundle_id: str) -> int:
    data = bundle_id.encode()
    target = _AEDesc()
    error = _CORE_SERVICES.AECreateDesc(_TYPE_APPLICATION_BUNDLE_ID, data, len(data), ctypes.byref(target))
    if error:
        return error
    try:
        # askUserIfNeeded=False: a prompt raised from a background agent would
        # be attributed to whatever app launched it, so only read the state.
        return _CORE_SERVICES.AEDeterminePermissionToAutomateTarget(
            ctypes.byref(target), _TYPE_WILDCARD, _TYPE_WILDCARD, False
        )
    finally:
        _CORE_SERVICES.AEDisposeDesc(ctypes.byref(target))


def automation_problem(bundle_id: str, app_name: str) -> str | None:
    """Return a human-readable problem when lincy may not send Apple Events to the app."""
    code = _permission_status(bundle_id)
    # procNotFound: macOS only answers for a running target, and launching the
    # app at startup is not acceptable. The first JXA call will surface a
    # missing grant (and prompt) instead.
    if code in (0, _PROC_NOT_FOUND):
        return None
    meaning = _CODE_MEANINGS.get(code, "unexpected result")
    return (
        f"Automation access to {app_name} is not granted ({meaning}, code {code}). "
        "Grant it in System Settings > Privacy & Security > Automation, "
        f"allowing the app that runs lincy (e.g. Terminal) to control {app_name}, "
        "or run `uv run permissions-warmup`."
    )
