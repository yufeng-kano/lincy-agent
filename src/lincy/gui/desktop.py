"""DesktopBackend: the action surface the GUI manager drives.

Every action is real input (no AXPress, no AX value setting), followed by a
settle delay and a fresh snapshot, so what the model sees is what happened.
"""

import time

import AppKit

from . import ax, capture, input_source
from . import input as hid
from .ax import Snapshot

_LAUNCH_TIMEOUT_SECONDS = 10.0
_LAUNCH_POLL_SECONDS = 0.2


class StaleIndexError(Exception):
    """The element index does not belong to the latest snapshot."""


class StateReadError(Exception):
    """The action was delivered, but reading the new state failed.

    Kept apart from action errors so the model does not retry an action
    (a click on Submit, typed text) that already happened.
    """


class DesktopBackend:
    def __init__(
        self,
        *,
        screenshot_max_width: int | None,
        screenshot_quality: int,
        max_tree_nodes: int,
        text_limit: int,
        set_marks: bool,
        default_input_source: str,
        settle_seconds: float,
    ) -> None:
        self._screenshot_max_width = screenshot_max_width
        self._screenshot_quality = screenshot_quality
        self._max_tree_nodes = max_tree_nodes
        self._text_limit = text_limit
        self._set_marks = set_marks
        self.default_input_source = default_input_source
        self._settle_seconds = settle_seconds
        self._next_snapshot_id = 1
        self._latest: Snapshot | None = None
        self._window = None

    def get_state(self) -> Snapshot:
        snapshot, self._window = ax.build_snapshot(
            snapshot_id=self._next_snapshot_id,
            max_tree_nodes=self._max_tree_nodes,
            text_limit=self._text_limit,
            input_source=input_source.current_input_source(),
            previous_window=self._window,
        )
        self._next_snapshot_id += 1
        if self._set_marks:
            # Marks are drawn at 1 pixel = 1 point, then downscaled.
            shot = capture.take_screenshot(quality=self._screenshot_quality)
            shot = ax.draw_marks(
                shot, snapshot.elements,
                max_width=self._screenshot_max_width, quality=self._screenshot_quality,
            )
        else:
            shot = capture.take_screenshot(
                max_width=self._screenshot_max_width, quality=self._screenshot_quality,
            )
        snapshot.screenshot = shot
        if shot.width:
            snapshot.screenshot_scale = round(shot.width / self._screen_size()[0], 3)
        self._latest = snapshot
        return snapshot

    def prepare(self, app: str | None) -> Snapshot:
        """Task start: bring the target app forward, maximize it, reset input source."""
        # A new task should not report a window change against the last task.
        self._window = None
        if app:
            self._activate(app)
        else:
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None:
                self._focus(int(front.processIdentifier()))
        if input_source.current_input_source() != self.default_input_source:
            input_source.select_input_source(self.default_input_source)
        return self._settle()

    def open_app(self, name: str) -> Snapshot:
        self._activate(name)
        return self._settle()

    def click(
        self, *, index: int | None = None, state: int | None = None,
        x: int | None = None, y: int | None = None,
        button: str = "left", count: int = 1,
    ) -> Snapshot:
        px, py = self._point(index, state, x, y)
        hid.click(px, py, button=button, count=count)
        return self._settle()

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> Snapshot:
        hid.drag(x1, y1, x2, y2)
        return self._settle()

    def scroll(
        self, *, index: int | None = None, state: int | None = None,
        x: int | None = None, y: int | None = None,
        direction: str = "down", amount: int = 3,
    ) -> Snapshot:
        px, py = self._point(index, state, x, y)
        hid.scroll(px, py, direction, amount)
        return self._settle()

    def type_text(self, text: str) -> Snapshot:
        hid.type_text(text)
        return self._settle()

    def press_key(self, key: str) -> Snapshot:
        hid.press_key(key)
        return self._settle()

    def set_input_source(self, source_id: str) -> Snapshot:
        input_source.select_input_source(source_id)
        return self._settle()

    def _settle(self) -> Snapshot:
        time.sleep(self._settle_seconds)
        try:
            return self.get_state()
        except Exception as e:
            raise StateReadError(str(e)) from e

    def _screen_size(self) -> tuple[float, float]:
        # The primary display: the screenshot, the scale and maximize all use
        # it, so the model never acts on a screen it cannot see.
        size = AppKit.NSScreen.screens()[0].frame().size
        return size.width, size.height

    def _point(
        self, index: int | None, state: int | None, x: int | None, y: int | None,
    ) -> tuple[int, int]:
        if index is not None:
            if x is not None or y is not None:
                raise ValueError("give either index or x and y, not both")
            latest = self._latest
            # Every snapshot numbers elements from 1 again, so an index is
            # only meaningful together with the state it was read from.
            if latest is None or state != latest.snapshot_id:
                current = latest.snapshot_id if latest is not None else "none"
                raise StaleIndexError(
                    f"index {index} was given with state {state}, but the latest "
                    f"state is {current}"
                )
            element = next((el for el in latest.elements if el.index == index), None)
            if element is None:
                raise StaleIndexError(f"index {index} is not in state {state}")
            bx, by, bw, bh = element.bbox
            px, py = bx + bw // 2, by + bh // 2
        elif x is None or y is None:
            raise ValueError("give either index (with state) or both x and y")
        else:
            px, py = x, y
        width, height = self._screen_size()
        if not (0 <= px < width and 0 <= py < height):
            # The real cursor clamps to the screen edge, so an off-screen
            # point would click the Dock or the menu bar instead.
            raise ValueError(
                f"point ({px}, {py}) is off screen ({int(width)}x{int(height)}); "
                "scroll the target into view first"
            )
        return px, py

    def _activate(self, name: str) -> None:
        """Launch or activate an app by English name or bundle id, then focus it."""
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        url = workspace.URLForApplicationWithBundleIdentifier_(name)
        if url is None:
            path = workspace.fullPathForApplication_(name)
            url = AppKit.NSURL.fileURLWithPath_(path) if path else None
        if url is None:
            raise ValueError(f"application {name!r} not found; use its English name or bundle id")
        bundle_id = AppKit.NSBundle.bundleWithURL_(url).bundleIdentifier()
        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)
        workspace.openApplicationAtURL_configuration_completionHandler_(url, config, None)

        deadline = time.monotonic() + _LAUNCH_TIMEOUT_SECONDS
        while True:
            running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
            app = running[0] if running else None
            if app is not None and app.isFinishedLaunching() and ax.main_window(int(app.processIdentifier())) is not None:
                break
            if time.monotonic() >= deadline:
                if app is None:
                    raise RuntimeError(f"{name} did not start within {_LAUNCH_TIMEOUT_SECONDS:.0f}s")
                # Running without a window (e.g. a menu-bar app); still focus it.
                break
            time.sleep(_LAUNCH_POLL_SECONDS)
        self._focus(int(app.processIdentifier()))

    def _focus(self, pid: int) -> None:
        ax.raise_window(pid)
        window = ax.main_window(pid)
        if window is not None:
            ax.maximize_window(window)
