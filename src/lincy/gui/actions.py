"""Desktop action primitives retained for legacy screenshot tools."""

import base64
import io
import subprocess
import sys
import time

from ..llm.schema import ContentPart


_CLIPBOARD_SETTLE_SECONDS = 0.05
_PASTE_HOTKEY_INTERVAL_SECONDS = 0.05
_PASTE_SETTLE_SECONDS = 0.15


def take_screenshot(
    *,
    max_width: int | None = None,
    quality: int = 80,
    region: tuple[int, int, int, int] | None = None,
) -> ContentPart:
    """Take a screenshot and return as base64 JPEG ContentPart.

    Args:
        max_width: Resize proportionally if image is wider. None = no resize.
        quality: JPEG quality (1-100).
        region: Optional crop region (x, y, width, height) in logical pixels.
    """
    import pyautogui
    from PIL import Image

    img = pyautogui.screenshot(region=region)

    if max_width is not None and img.width > max_width:
        ratio = max_width / img.width
        new_h = int(img.height * ratio)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    # JPEG requires RGB (no alpha channel)
    if img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return ContentPart(
        type="image",
        media_type="image/jpeg",
        data=b64,
        width=img.width,
        height=img.height,
    )


def type_text(text: str) -> str:
    """Type text via clipboard paste. Supports Unicode."""
    import pyautogui

    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    time.sleep(_CLIPBOARD_SETTLE_SECONDS)
    pyautogui.hotkey(
        "command", "v", interval=_PASTE_HOTKEY_INTERVAL_SECONDS,
    )
    time.sleep(_PASTE_SETTLE_SECONDS)
    return f"Typed: {text!r}"


def activate_app(name: str) -> str:
    """Open or switch to an application by name.

    macOS: mdfind to locate .app bundles, then open (activates existing).
    Windows: AppActivate for running apps, Get-StartApps + explorer for launching.
    """
    if sys.platform == "darwin":
        return _activate_app_macos(name)
    if sys.platform == "win32":
        return _activate_app_windows(name)
    raise OSError(f"Unsupported platform: {sys.platform}")


def _activate_app_macos(name: str) -> str:
    safe = name.replace("'", "\\'")
    query = (
        "kMDItemContentType == com.apple.application-bundle && "
        f"kMDItemFSName == '*{safe}*'cd"
    )
    r = subprocess.run(
        ["mdfind", query],
        capture_output=True, text=True,
    )
    matches = [line for line in r.stdout.strip().splitlines() if line]
    if not matches:
        return f"No application matching '{name}' found."

    # Post-filter: prefer exact name match over substring
    name_lower = name.lower().removesuffix(".app")
    exact = [m for m in matches
             if m.rsplit("/", 1)[-1].removesuffix(".app").lower() == name_lower]
    if exact:
        matches = exact

    if len(matches) == 1:
        subprocess.run(["open", matches[0]], check=True)
        return f"Activated: {matches[0].rsplit('/', 1)[-1]}"
    names = [m.rsplit("/", 1)[-1] for m in matches]
    return f"Multiple matches: {', '.join(names)}"


def _activate_app_windows(name: str) -> str:
    import json as _json

    # Try to activate a running app by window title
    safe_name = name.replace('"', '`"')
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'(New-Object -ComObject WScript.Shell).AppActivate("{safe_name}")'],
        capture_output=True, text=True,
    )
    if r.stdout.strip() == "True":
        return f"Activated: {name}"

    # Search Start Menu apps
    r2 = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f'Get-StartApps -Name "*{safe_name}*" | '
         'Select-Object Name, AppID | ConvertTo-Json -Compress'],
        capture_output=True, text=True,
    )
    try:
        data = _json.loads(r2.stdout)
    except (ValueError, _json.JSONDecodeError):
        return f"No application matching '{name}' found."

    if isinstance(data, dict):
        data = [data]
    if not data:
        return f"No application matching '{name}' found."
    if len(data) == 1:
        subprocess.run(
            ["explorer.exe", f"shell:AppsFolder\\{data[0]['AppID']}"],
        )
        return f"Activated: {data[0]['Name']}"
    names_list = [d["Name"] for d in data]
    return f"Multiple matches: {', '.join(names_list)}"


def wait(seconds: float) -> str:
    """Sleep for a given number of seconds."""
    seconds = min(max(seconds, 0.1), 10.0)
    time.sleep(seconds)
    return f"Waited {seconds:.1f}s"


def scroll_at_pixel(
    cx: float,
    cy: float,
    direction: str = "down",
    amount: int = 3,
    *,
    invert: bool = False,
) -> str:
    """Scroll at pixel coordinates using moveTo then scroll.

    Moves the mouse to *(cx, cy)* first, then sends individual scroll
    clicks with a short delay.  More reliable than ``scroll(x=, y=)``
    for apps that ignore the coordinate parameter (e.g. Qt).

    Args:
        cx: Target X in logical screen pixels.
        cy: Target Y in logical screen pixels.
        direction: "up" or "down".
        amount: Number of scroll clicks (positive).
        invert: Flip scroll direction (for macOS natural scrolling).
    """
    import pyautogui

    pyautogui.moveTo(cx, cy)
    time.sleep(0.1)
    step = 1 if direction == "up" else -1
    if invert:
        step = -step
    for _ in range(amount):
        pyautogui.scroll(step)
        time.sleep(0.05)
    return f"Scrolled {direction} {amount} clicks at pixel ({cx:.0f}, {cy:.0f})"
