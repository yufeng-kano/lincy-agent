"""Manual GUI backend check for the lincy host; tests never run this.

    uv run python -m lincy.gui.smoke [--app Calculator] [--click INDEX] [--type TEXT]

Without --click/--type no input events are sent. --app brings the app
forward, maximizes it and switches to the ABC input source, as a task would.
"""

import argparse

from . import input_source, permissions
from .capture import save_screenshot
from .desktop import DesktopBackend

_SCREENSHOT_PATH = "/tmp/lincy_gui_smoke.jpg"
_PREVIEW_LINES = 40


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m lincy.gui.smoke")
    parser.add_argument("--app", help="English app name or bundle id to prepare")
    parser.add_argument("--click", type=int, metavar="INDEX", help="click this element index")
    parser.add_argument("--type", metavar="TEXT", help="type this text after the optional click")
    args = parser.parse_args()

    print(f"Accessibility: {permissions.accessibility_granted()}")
    print(f"Screen Recording: {permissions.screen_recording_granted()}")
    print(f"Keyboard navigation: {permissions.keyboard_navigation_enabled()}")
    print(f"Secure Input active: {permissions.secure_input_enabled()}")
    print(f"Input source: {input_source.current_input_source()}")
    print(f"Enabled input sources: {', '.join(input_source.list_input_sources())}")

    backend = DesktopBackend(
        screenshot_max_width=None,
        screenshot_quality=80,
        max_tree_nodes=800,
        text_limit=200,
        set_marks=False,
        default_input_source="com.apple.keylayout.ABC",
        settle_seconds=0.4,
    )
    snapshot = backend.prepare(args.app) if args.app else backend.get_state()
    if args.click is not None:
        snapshot = backend.click(index=args.click, state=snapshot.snapshot_id)
    if args.type is not None:
        snapshot = backend.type_text(args.type)

    lines = snapshot.render().splitlines()
    print()
    print("\n".join(lines[:_PREVIEW_LINES]))
    if len(lines) > _PREVIEW_LINES:
        print(f"... ({len(lines) - _PREVIEW_LINES} more lines)")
    if snapshot.screenshot is not None:
        save_screenshot(snapshot.screenshot, _SCREENSHOT_PATH)
        print(f"Screenshot: {_SCREENSHOT_PATH} ({snapshot.screenshot.width}x{snapshot.screenshot.height})")


if __name__ == "__main__":
    main()
