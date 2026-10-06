"""Real mouse and keyboard input posted to the HID event tap.

Events go through kCGHIDEventTap, so they land wherever the real cursor and
keyboard focus are, including menus and panels owned by other processes.
"""

import ctypes
import time
from typing import NamedTuple

import Quartz

_EVENT_GAP_SECONDS = 0.03
_DRAG_STEPS = 10
_TYPE_CHUNK_UTF16_UNITS = 20
_RETURN_KEYCODE = 0x24

# Only non-printable keys; printable characters come from the active layout.
_NAMED_KEYS = {
    "return": 0x24, "enter": 0x24, "tab": 0x30, "space": 0x31,
    "escape": 0x35, "esc": 0x35, "delete": 0x33, "backspace": 0x33,
    "forwarddelete": 0x75, "up": 0x7E, "down": 0x7D, "left": 0x7B,
    "right": 0x7C, "home": 0x73, "end": 0x77, "pageup": 0x74,
    "pagedown": 0x79, "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
    "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65,
    "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
}
_MODIFIER_ALIASES = {
    "command": "command", "cmd": "command", "shift": "shift",
    "option": "option", "alt": "option", "control": "control", "ctrl": "control",
}
_MODIFIER_KEYS = {
    "command": (0x37, Quartz.kCGEventFlagMaskCommand),
    "shift": (0x38, Quartz.kCGEventFlagMaskShift),
    "option": (0x3A, Quartz.kCGEventFlagMaskAlternate),
    "control": (0x3B, Quartz.kCGEventFlagMaskControl),
}
# Keypad keys also produce digits and operators; the main block is what a
# person means by "+" or "1".
_KEYPAD_KEYCODES = frozenset({
    0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51, 0x52, 0x53, 0x54,
    0x55, 0x56, 0x57, 0x58, 0x59, 0x5B, 0x5C,
})
_UC_KEY_ACTION_DISPLAY = 3
_UC_SHIFT_STATE = 2  # (shiftKey >> 8) & 0xFF
_UC_NO_DEAD_KEYS = 1


class KeyPress(NamedTuple):
    keycode: int
    modifiers: tuple[str, ...]  # canonical: command, shift, option, control


def layout_keymap() -> dict[str, tuple[int, bool]]:
    """Map each printable character of the current layout to (keycode, shift)."""
    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    carbon.LMGetKbdType.restype = ctypes.c_uint8
    carbon.UCKeyTranslate.restype = ctypes.c_int32
    carbon.UCKeyTranslate.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16),
    ]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    # The ASCII-capable layout, not the current one: with an input method
    # such as Zhuyin active, the current layout maps unshifted keys to
    # bopomofo, while shortcuts and symbols still go through the ABC layout.
    source = carbon.TISCopyCurrentASCIICapableKeyboardLayoutInputSource()
    try:
        prop = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        data = carbon.TISGetInputSourceProperty(source, prop)
        if not data:
            raise RuntimeError("current keyboard layout has no Unicode key layout data")
        layout = cf.CFDataGetBytePtr(data)
        kbd_type = carbon.LMGetKbdType()
        dead_state = ctypes.c_uint32(0)
        length = ctypes.c_ulong(0)
        chars = (ctypes.c_uint16 * 4)()
        keymap: dict[str, tuple[int, bool]] = {}
        # Unshifted pass first so "1" maps to the 1 key, not Shift+something.
        for shift in (False, True):
            for keycode in range(128):
                if keycode in _KEYPAD_KEYCODES:
                    continue
                status = carbon.UCKeyTranslate(
                    layout, keycode, _UC_KEY_ACTION_DISPLAY,
                    _UC_SHIFT_STATE if shift else 0, kbd_type, _UC_NO_DEAD_KEYS,
                    ctypes.byref(dead_state), 4, ctypes.byref(length), chars,
                )
                if status == 0 and length.value == 1:
                    keymap.setdefault(chr(chars[0]), (keycode, shift))
        return keymap
    finally:
        cf.CFRelease(source)


def parse_key(spec: str, *, keymap: dict[str, tuple[int, bool]] | None = None) -> KeyPress:
    """Parse "Command+Shift+G", "Return", "/" or "A" into a key press.

    A bare uppercase letter means Shift+letter. With other modifiers the
    letter case is ignored, so "Command+A" is Select All, not Cmd+Shift+A.
    """
    if spec.endswith("++"):
        parts = [*spec[:-2].split("+"), "+"]
    elif spec == "+":
        parts = ["+"]
    else:
        parts = spec.split("+")
    key = parts[-1].strip()
    if not key:
        raise ValueError(f"unsupported key {spec!r}: empty key")
    modifiers: list[str] = []
    for token in parts[:-1]:
        name = _MODIFIER_ALIASES.get(token.strip().lower())
        if name is None:
            raise ValueError(f"unsupported modifier {token.strip()!r} in key {spec!r}")
        if name not in modifiers:
            modifiers.append(name)

    named = _NAMED_KEYS.get(key.lower())
    if named is not None:
        return KeyPress(named, tuple(modifiers))
    if len(key) != 1:
        raise ValueError(f"unsupported key {spec!r}")
    if key.isalpha() and key.isupper():
        key = key.lower()
        if not modifiers:
            modifiers.append("shift")
    keymap = layout_keymap() if keymap is None else keymap
    found = keymap.get(key)
    if found is None:
        raise ValueError(f"unsupported key {spec!r}: not on the current keyboard layout")
    keycode, needs_shift = found
    if needs_shift and "shift" not in modifiers:
        modifiers.append("shift")
    return KeyPress(keycode, tuple(modifiers))


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(_EVENT_GAP_SECONDS)


def _source():
    return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _post_key(source, keycode: int, down: bool, flags: int) -> None:
    event = Quartz.CGEventCreateKeyboardEvent(source, keycode, down)
    Quartz.CGEventSetFlags(event, flags)
    _post(event)


def move(x: int, y: int) -> None:
    _post(Quartz.CGEventCreateMouseEvent(
        _source(), Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft,
    ))


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    if button == "left":
        down_type, up_type, cg_button = (
            Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft,
        )
    elif button == "right":
        down_type, up_type, cg_button = (
            Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight,
        )
    else:
        raise ValueError(f"unsupported mouse button {button!r}; use left or right")
    if count not in (1, 2, 3):
        raise ValueError(f"unsupported click count {count}; use 1, 2 or 3")
    source = _source()
    move(x, y)
    # macOS recognises double clicks by the click state rising 1, 2, ...
    for state in range(1, count + 1):
        for event_type in (down_type, up_type):
            event = Quartz.CGEventCreateMouseEvent(source, event_type, (x, y), cg_button)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, state)
            _post(event)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    source = _source()
    move(x1, y1)
    _post(Quartz.CGEventCreateMouseEvent(
        source, Quartz.kCGEventLeftMouseDown, (x1, y1), Quartz.kCGMouseButtonLeft,
    ))
    for step in range(1, _DRAG_STEPS + 1):
        point = (x1 + (x2 - x1) * step / _DRAG_STEPS, y1 + (y2 - y1) * step / _DRAG_STEPS)
        _post(Quartz.CGEventCreateMouseEvent(
            source, Quartz.kCGEventLeftMouseDragged, point, Quartz.kCGMouseButtonLeft,
        ))
    _post(Quartz.CGEventCreateMouseEvent(
        source, Quartz.kCGEventLeftMouseUp, (x2, y2), Quartz.kCGMouseButtonLeft,
    ))


def scroll(x: int, y: int, direction: str, amount: int = 3) -> None:
    # Positive wheel1 scrolls content up, positive wheel2 scrolls left.
    deltas = {"up": (amount, 0), "down": (-amount, 0), "left": (0, amount), "right": (0, -amount)}
    if direction not in deltas:
        raise ValueError(f"unsupported scroll direction {direction!r}; use up, down, left or right")
    if amount < 1:
        raise ValueError("scroll amount must be at least 1")
    move(x, y)
    vertical, horizontal = deltas[direction]
    event = Quartz.CGEventCreateScrollWheelEvent(
        _source(), Quartz.kCGScrollEventUnitLine, 2, vertical, horizontal,
    )
    Quartz.CGEventSetLocation(event, (x, y))
    _post(event)


def text_chunks(text: str) -> list[str]:
    """Split text into "\\n" items and runs of at most 20 UTF-16 units."""
    chunks: list[str] = []
    current = ""
    units = 0
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            if current:
                chunks.append(current)
            chunks.append("\n")
            current, units = "", 0
            continue
        size = len(ch.encode("utf-16-le")) // 2
        if current and units + size > _TYPE_CHUNK_UTF16_UNITS:
            chunks.append(current)
            current, units = "", 0
        current += ch
        units += size
    if current:
        chunks.append(current)
    return chunks


def type_text(text: str) -> None:
    source = _source()
    for chunk in text_chunks(text):
        if chunk == "\n":
            _post_key(source, _RETURN_KEYCODE, True, 0)
            _post_key(source, _RETURN_KEYCODE, False, 0)
            continue
        units = len(chunk.encode("utf-16-le")) // 2
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(source, 0, down)
            Quartz.CGEventSetFlags(event, 0)
            Quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
            _post(event)


def press_key(spec: str, *, keymap: dict[str, tuple[int, bool]] | None = None) -> None:
    key = parse_key(spec, keymap=keymap)
    source = _source()
    flags = 0
    # Real modifier key events, not just flags: some apps (Chrome) track
    # modifier state from the key events themselves.
    for name in key.modifiers:
        keycode, flag = _MODIFIER_KEYS[name]
        flags |= flag
        _post_key(source, keycode, True, flags)
    _post_key(source, key.keycode, True, flags)
    _post_key(source, key.keycode, False, flags)
    for name in reversed(key.modifiers):
        keycode, flag = _MODIFIER_KEYS[name]
        flags &= ~flag
        _post_key(source, keycode, False, flags)
