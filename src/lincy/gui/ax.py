"""Accessibility snapshot of the focused window with screen-point bboxes.

Filtering rules are ported from OpenComputerUse AccessibilitySnapshot.swift
(pin c8a7758): unnamed AXGroup/AXUnknown containers are flattened, Chrome
anonymous groups with a press action become buttons, plain text groups are
merged into one text line, links keep their own index, and long lists only
expose visible rows with a "showing X-Y of N items" note.
"""

import base64
import io
import re
from typing import Any

from pydantic import BaseModel

from ..llm.schema import ContentPart

_NODE_ATTRS = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue",
    "AXPlaceholderValue", "AXPosition", "AXSize", "AXEnabled", "AXFocused",
    "AXSelected", "AXChildren", "AXRows", "AXVisibleRows",
    "AXVisibleChildren", "AXContents",
]
_MAX_DEPTH = 64  # nesting of listed elements, as in OCU
_MAX_NESTING = 256  # raw AX nesting, keeps recursion within Python's limit
_MAX_VISIBLE_ROWS = 20
_ANON_TARGET_MAX_W = 240
_ANON_TARGET_MAX_H = 120
_GENERIC_ROLES = {"AXGroup", "AXUnknown"}
_ROW_CONTAINER_ROLES = {"AXOutline", "AXList", "AXTable", "AXBrowser"}
_PRIMARY_ACTIONS = {"axpress", "axconfirm", "axopen"}
# Controls whose text children only repeat their own label.
_LABELLED_CONTROL_ROLES = {
    "AXLink", "AXButton", "AXCheckBox", "AXRadioButton", "AXMenuBarItem",
    "AXDisclosureTriangle",
}
# Controls whose children are internal parts (text runs, scroll thumbs).
_INPUT_ROLES = {
    "AXTextField", "AXTextArea", "AXSlider", "AXIncrementor", "AXColorWell",
    "AXScrollBar",
}
_INTERACTIVE_ROLES = {
    "AXComboBox", "AXPopUpButton", "AXMenuButton", "AXMenuItem",
}
_ROLE_NAMES = {"AXRadioButton": "radio"}
_SUBROLE_NAMES = {
    "AXTabButton": "tab",
    "AXSearchField": "searchfield",
    "AXSecureTextField": "securetextfield",
    "AXSwitch": "switch",
}
_COUNTER_RE = re.compile(r"^\d+\s*/\s*\d+$")
_TIME_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


class ElementRecord(BaseModel):
    index: int
    role: str
    label: str
    value: str
    bbox: tuple[int, int, int, int]
    focused: bool
    enabled: bool


class Snapshot(BaseModel):
    snapshot_id: int
    app_name: str
    bundle_id: str
    pid: int
    window_title: str
    window_bbox: tuple[int, int, int, int]
    window_changed: bool
    input_source: str
    elements: list[ElementRecord]
    truncated: bool
    screenshot: ContentPart | None = None
    # Screenshot pixels per screen point after downscaling to
    # screenshot_max_width; 1.0 means the image and the bboxes line up.
    screenshot_scale: float = 1.0
    # Top-level element (menu bar, panel of another process) holding
    # keyboard focus outside the window. Its subtree is listed first (the
    # first outside_focus_count elements) so truncation never hides it.
    outside_focus: str = ""
    outside_focus_count: int = 0

    def render(self) -> str:
        # No snapshot_id here: the manager compares render() output to
        # detect actions that changed nothing.
        lines = [f"App: {self.app_name} ({self.bundle_id}, pid {self.pid})"]
        if self.window_bbox == (0, 0, 0, 0) and not self.window_title:
            lines.append("Window: none (the app has no focused window)")
        else:
            lines.append(f'Window: "{_quote(self.window_title)}" {_bbox(self.window_bbox)}')
        lines.append(f"Input source: {self.input_source}")
        if self.screenshot is not None and self.screenshot_scale != 1.0:
            lines.append(
                f"Screenshot: {self.screenshot.width}x{self.screenshot.height}, "
                f"scaled {self.screenshot_scale:.3f}x from screen points "
                "(image pixel / scale = screen coordinate); use the bboxes "
                "below for coordinates."
            )
        if self.window_changed:
            lines.append("Note: the focused window changed since the previous state.")
        lines.append('Elements (index role "label" = "value" [x,y,w,h] in screen points):')
        for i, el in enumerate(self.elements):
            if self.outside_focus and i == 0:
                lines.append(f"--- Keyboard focus is outside the window, in {self.outside_focus} ---")
            if self.outside_focus and i == self.outside_focus_count:
                lines.append("--- Window ---")
            lines.append(_element_line(el))
        if not self.elements:
            lines.append("(no accessible elements; use the screenshot and x/y coordinates)")
        if self.truncated:
            lines.append(
                f"(tree truncated at {len(self.elements)} elements; scroll or "
                "close panels to see the rest)"
            )
        focused = next((el for el in self.elements if el.focused), None)
        lines.append(
            f"Focused element: {_element_line(focused)}" if focused
            else "Focused element: none of the listed elements"
        )
        return "\n".join(lines)


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _bbox(b: tuple[int, int, int, int]) -> str:
    return f"[{b[0]},{b[1]},{b[2]},{b[3]}]"


def _element_line(el: ElementRecord) -> str:
    line = f'{el.index} {el.role} "{_quote(el.label)}"'
    if el.value:
        line += f' = "{_quote(el.value)}"'
    if el.focused:
        line += " (focused)"
    if not el.enabled:
        line += " (disabled)"
    return f"{line} {_bbox(el.bbox)}"


class AXAccess:
    """pyobjc access layer; tests pass an object with the same methods."""

    def __init__(self) -> None:
        import AppKit
        import ApplicationServices
        import CoreFoundation

        self._as = ApplicationServices
        self._appkit = AppKit
        self._cf = CoreFoundation

    def system_wide(self) -> Any:
        return self._as.AXUIElementCreateSystemWide()

    def application(self, pid: int) -> Any:
        return self._as.AXUIElementCreateApplication(pid)

    def attrs(self, element: Any, names: list[str]) -> dict[str, Any]:
        """Fetch attributes in one IPC round trip, converted to plain Python."""
        AS = self._as
        err, values = AS.AXUIElementCopyMultipleAttributeValues(element, names, 0, None)
        if err != 0 or values is None:
            return {}
        out: dict[str, Any] = {}
        for name, value in zip(names, values):
            if value is None:
                continue
            if isinstance(value, AS.AXValueRef):
                kind = AS.AXValueGetType(value)
                if kind == AS.kAXValueCGPointType:
                    _, p = AS.AXValueGetValue(value, kind, None)
                    out[name] = (p.x, p.y)
                elif kind == AS.kAXValueCGSizeType:
                    _, s = AS.AXValueGetValue(value, kind, None)
                    out[name] = (s.width, s.height)
                # Missing attributes come back as kAXValueAXErrorType.
                continue
            if isinstance(value, str):
                out[name] = str(value)
            elif isinstance(value, self._appkit.NSArray):
                out[name] = list(value)
            else:
                out[name] = value
        return out

    def actions(self, element: Any) -> list[str]:
        err, names = self._as.AXUIElementCopyActionNames(element, None)
        return [str(n) for n in names] if err == 0 and names else []

    def pid(self, element: Any) -> int:
        _, pid = self._as.AXUIElementGetPid(element, None)
        return int(pid)

    def same(self, a: Any, b: Any) -> bool:
        return bool(self._cf.CFEqual(a, b))

    def frontmost_pid(self) -> int | None:
        app = self._appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    def app_info(self, pid: int) -> tuple[str, str]:
        app = self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return "", ""
        return str(app.localizedName() or ""), str(app.bundleIdentifier() or "")

    def enable_accessibility(self, app_element: Any) -> None:
        # Chromium/Electron (AXManualAccessibility) and Firefox
        # (AXEnhancedUserInterface) withhold web content from AX until an
        # assistive client asks; same best-effort switches OCU flips.
        for name in ("AXManualAccessibility", "AXEnhancedUserInterface"):
            self._as.AXUIElementSetAttributeValue(app_element, name, True)


def _frame(a: dict[str, Any]) -> tuple[int, int, int, int] | None:
    pos, size = a.get("AXPosition"), a.get("AXSize")
    if pos is None or size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return (round(pos[0]), round(pos[1]), round(size[0]), round(size[1]))


def _intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


class _TreeBuilder:
    def __init__(self, access: Any, max_nodes: int, text_limit: int) -> None:
        self.access = access
        self.max_nodes = max_nodes
        self.text_limit = text_limit
        self.elements: list[ElementRecord] = []
        self.truncated = False
        self._cache: dict[Any, dict[str, Any]] = {}
        self.seen: set[Any] = set()

    def attrs(self, el: Any) -> dict[str, Any]:
        cached = self._cache.get(el)
        if cached is None:
            cached = self.access.attrs(el, _NODE_ATTRS)
            self._cache[el] = cached
        return cached

    def clean(self, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        text = value.replace("\r", "").replace("\n", "\\n").strip()
        if len(text) > self.text_limit:
            return text[: self.text_limit] + "..."
        return text

    def emit(self, a: dict[str, Any], role: str, label: str, value: str) -> bool:
        frame = _frame(a)
        if frame is None or self.truncated:
            # Without a frame there is no point to click.
            return False
        if len(self.elements) >= self.max_nodes:
            self.truncated = True
            return False
        self.elements.append(ElementRecord(
            index=len(self.elements) + 1,
            role=role,
            label=label,
            value="" if value == label else value,
            bbox=frame,
            focused=a.get("AXFocused") is True,
            enabled=a.get("AXEnabled") is not False,
        ))
        return True

    def walk_children(self, el: Any, depth: int, nesting: int) -> None:
        """Walk children at depth; flattened parents pass their own depth."""
        for child in self.children(self.attrs(el)):
            self.walk(child, depth, nesting + 1)

    def walk(self, el: Any, depth: int, nesting: int) -> None:
        if self.truncated or depth >= _MAX_DEPTH or nesting >= _MAX_NESTING or el in self.seen:
            return
        self.seen.add(el)
        a = self.attrs(el)
        role = a.get("AXRole") or "AXUnknown"
        subrole = a.get("AXSubrole") or ""
        role_name = _SUBROLE_NAMES.get(subrole) or _ROLE_NAMES.get(role) or (
            role[2:].lower() if role.startswith("AX") else role.lower()
        )
        label = self.clean(a.get("AXTitle")) or self.clean(a.get("AXDescription"))
        value = self.value(a, role, subrole)

        if role in _GENERIC_ROLES:
            frame = _frame(a)
            if not label and not value and frame is not None:
                summary = self.text_summary(el)
                if summary:
                    self.emit(a, "text", summary, "")
                    for image in self.images_under(el, 0):
                        self.walk(image, depth + 1, nesting + 1)
                    return
                emitted = (
                    frame[2] <= _ANON_TARGET_MAX_W
                    and frame[3] <= _ANON_TARGET_MAX_H
                    and any(n.lower() in _PRIMARY_ACTIONS for n in self.access.actions(el))
                    and self.emit(a, "button", "", "")
                )
            else:
                emitted = bool(label or value) and self.emit(a, role_name, label, value)
            # Unnamed containers are flattened: their children keep our depth.
            self.walk_children(el, depth + 1 if emitted else depth, nesting)
            return

        if role == "AXStaticText":
            text = value or label
            if text:
                self.emit(a, role_name, text, "")
            return

        if role in _LABELLED_CONTROL_ROLES:
            if not label:
                label = self.clean(" ".join(self.descendant_texts(el, 0)))
            self.emit(a, role_name, label or self.clean(a.get("AXPlaceholderValue")), value)
            # An open menu-bar menu hangs under its selected item.
            if role == "AXMenuBarItem" and a.get("AXSelected") is True:
                self.walk_children(el, depth + 1, nesting)
            elif not label and role != "AXMenuBarItem":
                self.walk_children(el, depth + 1, nesting)
            return

        if role in _INPUT_ROLES:
            self.emit(a, role_name, label or self.clean(a.get("AXPlaceholderValue")), value)
            return

        if role == "AXRow":
            texts = list(dict.fromkeys(
                t for cell in a.get("AXChildren") or [] for t in self.descendant_texts(cell, 0)
            ))
            self.emit(a, role_name, label or self.clean(" ".join(texts)), value)
            if a.get("AXSelected") is True:
                self.walk_children(el, depth + 1, nesting)
            return

        if role in _ROW_CONTAINER_ROLES:
            rows = a.get("AXRows") or []
            visible = self.visible_rows(a)
            showing = ""
            if role != "AXBrowser" and visible and len(visible) < len(rows):
                shown = set(visible)
                positions = [i for i, row in enumerate(rows) if row in shown]
                showing = f"showing {positions[0]}-{positions[-1]} of {len(rows)} items"
            emitted = bool(label or showing) and self.emit(a, role_name, label, showing)
            self.walk_children(el, depth + 1 if emitted else depth, nesting)
            return

        if role in _INTERACTIVE_ROLES:
            emitted = self.emit(a, role_name, label or self.clean(a.get("AXPlaceholderValue")), value)
            self.walk_children(el, depth + 1 if emitted else depth, nesting)
            return

        emitted = bool(label or value) and self.emit(a, role_name, label, value)
        if role != "AXImage":
            self.walk_children(el, depth + 1 if emitted else depth, nesting)

    def value(self, a: dict[str, Any], role: str, subrole: str) -> str:
        v = a.get("AXValue")
        if isinstance(v, bool):
            return "on" if v else "off"
        if isinstance(v, (int, float)):
            if role in ("AXCheckBox", "AXRadioButton") or subrole == "AXTabButton":
                return {0: "off", 1: "on", 2: "mixed"}.get(int(v), str(v))
            return f"{v:g}" if isinstance(v, float) else str(v)
        return self.clean(v)

    def visible_rows(self, a: dict[str, Any]) -> list[Any]:
        rows = a.get("AXRows") or []
        if not rows:
            return []
        visible = a.get("AXVisibleRows") or []
        parent = _frame(a)
        if not visible and parent is not None:
            visible = [r for r in rows if (f := _frame(self.attrs(r))) and _intersects(f, parent)]
        # OCU caps visible rows at 20 too, but a maximized Open panel shows
        # far more; max_tree_nodes already bounds the total. The cap only
        # guards the blind fallback.
        return visible or rows[:_MAX_VISIBLE_ROWS]

    def children(self, a: dict[str, Any]) -> list[Any]:
        role = a.get("AXRole")
        rows = a.get("AXRows") or []
        visible_children = a.get("AXVisibleChildren") or []
        out: list[Any] = []
        if not (rows and role in _ROW_CONTAINER_ROLES) and not (
            visible_children and role == "AXList"
        ):
            out.extend(a.get("AXChildren") or [])
        out.extend(self.visible_rows(a))
        out.extend(a.get("AXContents") or [])
        out.extend(visible_children)
        return out

    def is_plain_text_container(self, el: Any, depth: int) -> bool:
        # Links are deliberately not plain text: they keep their own index.
        for child in self.attrs(el).get("AXChildren") or []:
            role = self.attrs(child).get("AXRole")
            if role in ("AXStaticText", "AXImage"):
                continue
            if role in _GENERIC_ROLES and depth < 3 and self.is_plain_text_container(child, depth + 1):
                continue
            return False
        return True

    def text_summary(self, el: Any) -> str:
        if not self.attrs(el).get("AXChildren") or not self.is_plain_text_container(el, 0):
            return ""
        texts = self.descendant_texts(el, 0, max_depth=8)
        if len(texts) < 2 or len(texts) > 8 or sum(len(t) for t in texts) > 220:
            return ""
        if any(_COUNTER_RE.match(t) or _TIME_RANGE_RE.match(t) for t in texts):
            return ""
        return self.clean(" ".join(texts))

    def descendant_texts(self, el: Any, depth: int, max_depth: int = 4) -> list[str]:
        if depth >= max_depth:
            return []
        a = self.attrs(el)
        if a.get("AXRole") in ("AXStaticText", "AXTextField"):
            text = self.value(a, "", "") or self.clean(a.get("AXTitle"))
            return [text] if text else []
        return [
            t for child in a.get("AXChildren") or []
            for t in self.descendant_texts(child, depth + 1, max_depth)
        ]

    def images_under(self, el: Any, depth: int) -> list[Any]:
        if depth >= 4:
            return []
        images: list[Any] = []
        for child in self.attrs(el).get("AXChildren") or []:
            if self.attrs(child).get("AXRole") == "AXImage":
                images.append(child)
            else:
                images.extend(self.images_under(child, depth + 1))
        return images[:4]


def build_snapshot(
    *,
    snapshot_id: int,
    max_tree_nodes: int,
    text_limit: int,
    input_source: str,
    previous_window: Any = None,
    access: Any = None,
) -> tuple[Snapshot, Any]:
    """Snapshot the focused window; return it with the window AX element.

    The window element is returned separately because pydantic cannot hold
    it; the caller passes it back as previous_window to detect switches.
    """
    access = access or AXAccess()
    system = access.system_wide()
    focused_app = access.attrs(system, ["AXFocusedApplication"]).get("AXFocusedApplication")
    window = None
    pid = 0
    if focused_app is not None:
        pid = access.pid(focused_app)
        window = access.attrs(focused_app, ["AXFocusedWindow"]).get("AXFocusedWindow")
    if window is None:
        # The system-wide query fails with kAXErrorCannotComplete for some
        # apps (seen with Firefox); the frontmost app is the same target.
        pid = access.frontmost_pid() or 0
        if pid:
            app_attrs = access.attrs(access.application(pid), ["AXFocusedWindow", "AXWindows"])
            window = app_attrs.get("AXFocusedWindow")
            if window is None and app_attrs.get("AXWindows"):
                window = app_attrs["AXWindows"][0]
    if pid:
        access.enable_accessibility(access.application(pid))
    app_name, bundle_id = access.app_info(pid) if pid else ("", "")

    # Find the top-level element (direct child of an application) holding
    # keyboard focus. If it is not the window, it is a menu or a panel of
    # another process (the macOS Open dialog), which must be visible too.
    focused = access.attrs(system, ["AXFocusedUIElement"]).get("AXFocusedUIElement")
    if focused is None and pid:
        focused = access.attrs(access.application(pid), ["AXFocusedUIElement"]).get("AXFocusedUIElement")
    top = None
    node = focused
    for _ in range(_MAX_DEPTH):
        if node is None or (window is not None and access.same(node, window)):
            break
        parent = access.attrs(node, ["AXParent"]).get("AXParent")
        if parent is None:
            # Broken parent chain: containment unknown, assume the window.
            break
        if access.attrs(parent, ["AXRole"]).get("AXRole") == "AXApplication":
            top = node
            break
        node = parent

    builder = _TreeBuilder(access, max_tree_nodes, text_limit)
    outside_focus = ""
    if top is not None:
        ta = builder.attrs(top)
        top_role = ta.get("AXRole") or "AXUnknown"
        top_name, top_bundle = access.app_info(access.pid(top))
        outside_focus = (
            f'{top_role[2:].lower()} "{_quote(builder.clean(ta.get("AXTitle")))}" '
            f"of {top_name} ({top_bundle})"
        )
        builder.seen.add(top)
        builder.walk_children(top, 0, 0)
        if not builder.elements:
            outside_focus = ""
    outside_count = len(builder.elements)

    window_title = ""
    window_bbox = (0, 0, 0, 0)
    if window is not None:
        wa = builder.attrs(window)
        window_title = builder.clean(wa.get("AXTitle"))
        window_bbox = _frame(wa) or (0, 0, 0, 0)
        builder.seen.add(window)
        builder.walk_children(window, 0, 0)

    window_changed = (
        previous_window is not None and window is not None
        and not access.same(previous_window, window)
    )
    snapshot = Snapshot(
        snapshot_id=snapshot_id,
        app_name=app_name,
        bundle_id=bundle_id,
        pid=pid,
        window_title=window_title,
        window_bbox=window_bbox,
        window_changed=window_changed,
        input_source=input_source,
        elements=builder.elements,
        truncated=builder.truncated,
        outside_focus=outside_focus,
        outside_focus_count=outside_count,
    )
    return snapshot, window


def main_window(pid: int) -> Any:
    """Focused window of the app, else its first window, else None."""
    import ApplicationServices as AS

    app = AS.AXUIElementCreateApplication(pid)
    err, window = AS.AXUIElementCopyAttributeValue(app, "AXFocusedWindow", None)
    if err == 0 and window is not None:
        return window
    err, windows = AS.AXUIElementCopyAttributeValue(app, "AXWindows", None)
    return windows[0] if err == 0 and windows else None


def raise_window(pid: int) -> None:
    import AppKit
    import ApplicationServices as AS

    window = main_window(pid)
    if window is not None:
        AS.AXUIElementPerformAction(window, "AXRaise")
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)


def maximize_window(window: Any) -> None:
    """Fill the visible frame of the primary screen (no full-screen Space)."""
    import AppKit
    import ApplicationServices as AS

    # Cocoa frames are bottom-left based on the primary screen; AX wants
    # top-left global points.
    # The primary screen, not mainScreen (the one with the key window): the
    # screenshot only covers the primary display.
    primary = AppKit.NSScreen.screens()[0]
    primary_height = primary.frame().size.height
    vf = primary.visibleFrame()
    top = primary_height - (vf.origin.y + vf.size.height)
    position = AS.AXValueCreate(AS.kAXValueCGPointType, (vf.origin.x, top))
    size = AS.AXValueCreate(AS.kAXValueCGSizeType, (vf.size.width, vf.size.height))
    # With AXEnhancedUserInterface on, apps animate frame changes and can
    # end at the wrong size; switch it off around the resize.
    _, pid = AS.AXUIElementGetPid(window, None)
    app = AS.AXUIElementCreateApplication(pid)
    err, enhanced = AS.AXUIElementCopyAttributeValue(app, "AXEnhancedUserInterface", None)
    if err == 0 and enhanced:
        AS.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", False)
    AS.AXUIElementSetAttributeValue(window, "AXPosition", position)
    AS.AXUIElementSetAttributeValue(window, "AXSize", size)
    if err == 0 and enhanced:
        AS.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)


def draw_marks(
    shot: ContentPart, elements: list[ElementRecord], *,
    max_width: int | None, quality: int,
) -> ContentPart:
    """Draw element indexes on a 1 pixel = 1 point screenshot, then downscale."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(base64.b64decode(shot.data or ""))).convert("RGB")
    draw = ImageDraw.Draw(img)
    for el in elements:
        x, y, w, h = el.bbox
        draw.rectangle((x, y, x + w, y + h), outline=(255, 0, 0), width=1)
        tag = str(el.index)
        draw.rectangle((x, y, x + 7 * len(tag) + 4, y + 12), fill=(255, 0, 0))
        draw.text((x + 2, y), tag, fill=(255, 255, 255))
    if max_width is not None and img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return ContentPart(
        type="image", media_type="image/jpeg",
        data=base64.b64encode(buf.getvalue()).decode("ascii"),
        width=img.width, height=img.height,
    )
