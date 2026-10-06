"""Tests for gui/desktop.py with fake ax/capture/input/input_source/AppKit."""

from types import SimpleNamespace

import pytest

from lincy.gui import desktop
from lincy.gui.ax import ElementRecord, Snapshot
from lincy.gui.desktop import DesktopBackend, StaleIndexError
from lincy.llm.schema import ContentPart

SHOT = ContentPart(type="image", media_type="image/jpeg", data="eA==", width=10, height=10)


def element(index, bbox):
    return ElementRecord(index=index, role="button", label=f"b{index}", value="",
                         bbox=bbox, focused=False, enabled=True)


class FakeAx:
    def __init__(self):
        self.trees = [[element(1, (10, 20, 100, 40)), element(2, (200, 300, 50, 50))]]
        self.window = "window-1"
        self.calls = []

    def build_snapshot(self, *, snapshot_id, max_tree_nodes, text_limit, input_source, previous_window):
        self.calls.append(("build", snapshot_id, max_tree_nodes, text_limit, input_source, previous_window))
        elements = self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]
        snapshot = Snapshot(
            snapshot_id=snapshot_id, app_name="App", bundle_id="com.app", pid=42,
            window_title="W", window_bbox=(0, 0, 800, 600),
            window_changed=previous_window is not None and previous_window != self.window,
            input_source=input_source, elements=elements, truncated=False,
        )
        return snapshot, self.window

    def draw_marks(self, shot, elements, *, max_width, quality):
        self.calls.append(("marks", shot.width, len(elements), max_width, quality))
        return ContentPart(type="image", media_type="image/jpeg", data="bWFyaw==", width=5, height=5)

    def raise_window(self, pid):
        self.calls.append(("raise", pid))

    def main_window(self, pid):
        return f"main-{pid}"

    def maximize_window(self, window):
        self.calls.append(("maximize", window))


class FakeInputSource:
    def __init__(self, current="com.apple.keylayout.ABC"):
        self.current = current
        self.selected = []

    def current_input_source(self):
        return self.current

    def list_input_sources(self):
        return [self.current]

    def select_input_source(self, source_id):
        if source_id == "missing":
            raise ValueError("input source 'missing' is not enabled")
        self.selected.append(source_id)
        self.current = source_id


class FakeHid:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args, kwargs))


@pytest.fixture()
def fakes(monkeypatch):
    ax = FakeAx()
    source = FakeInputSource()
    hid = FakeHid()
    shots = []

    def take_screenshot(*, max_width=None, quality=80, region=None):
        shots.append((max_width, quality, region))
        return SHOT

    monkeypatch.setattr(desktop, "ax", ax)
    monkeypatch.setattr(desktop, "input_source", source)
    monkeypatch.setattr(desktop, "hid", hid)
    monkeypatch.setattr(desktop, "capture", SimpleNamespace(take_screenshot=take_screenshot))
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    monkeypatch.setattr(desktop.DesktopBackend, "_screen_size", lambda self: (1440.0, 900.0))
    return SimpleNamespace(ax=ax, source=source, hid=hid, shots=shots)


def make_backend(**overrides):
    params = dict(
        screenshot_max_width=1280, screenshot_quality=70, max_tree_nodes=800,
        text_limit=200, set_marks=False,
        default_input_source="com.apple.keylayout.ABC", settle_seconds=0.0,
    )
    params.update(overrides)
    return DesktopBackend(**params)


class TestGetState:
    def test_snapshot_ids_are_monotonic(self, fakes):
        backend = make_backend()
        first = backend.get_state()
        second = backend.get_state()
        assert (first.snapshot_id, second.snapshot_id) == (1, 2)
        assert second.screenshot == SHOT
        assert fakes.shots == [(1280, 70, None), (1280, 70, None)]

    def test_previous_window_is_passed_back(self, fakes):
        backend = make_backend()
        backend.get_state()
        backend.get_state()
        assert [c[5] for c in fakes.ax.calls] == [None, "window-1"]

    def test_tree_settings_and_input_source(self, fakes):
        fakes.source.current = "com.apple.inputmethod.TCIM.Zhuyin"
        snapshot = make_backend(max_tree_nodes=50, text_limit=20).get_state()
        assert fakes.ax.calls[0][2:5] == (50, 20, "com.apple.inputmethod.TCIM.Zhuyin")
        assert snapshot.input_source == "com.apple.inputmethod.TCIM.Zhuyin"

    def test_set_marks_draws_on_full_resolution(self, fakes):
        snapshot = make_backend(set_marks=True).get_state()
        assert fakes.shots == [(None, 70, None)]
        assert fakes.ax.calls[-1] == ("marks", 10, 2, 1280, 70)
        assert snapshot.screenshot.data == "bWFyaw=="


class TestActions:
    def test_click_index_uses_bbox_centre(self, fakes):
        backend = make_backend()
        backend.get_state()
        snapshot = backend.click(index=1, state=1)
        assert fakes.hid.calls == [("click", (60, 40), {"button": "left", "count": 1})]
        assert snapshot.snapshot_id == 2

    def test_click_coordinates(self, fakes):
        backend = make_backend()
        backend.click(x=5, y=6, button="right", count=2)
        assert fakes.hid.calls == [("click", (5, 6), {"button": "right", "count": 2})]

    def test_index_only_valid_in_latest_snapshot(self, fakes):
        fakes.ax.trees = [[element(1, (0, 0, 10, 10)), element(2, (0, 0, 10, 10))], [element(1, (0, 0, 10, 10))]]
        backend = make_backend()
        backend.get_state()
        backend.get_state()
        with pytest.raises(StaleIndexError, match="index 2"):
            backend.click(index=2, state=2)
        assert fakes.hid.calls == []

    def test_index_from_older_state_is_rejected_even_if_in_range(self, fakes):
        # Index 1 exists in both states but names different elements.
        fakes.ax.trees = [[element(1, (0, 0, 10, 10))], [element(1, (100, 100, 10, 10))]]
        backend = make_backend()
        backend.get_state()
        backend.get_state()
        with pytest.raises(StaleIndexError, match="latest state is 2"):
            backend.click(index=1, state=1)
        with pytest.raises(StaleIndexError):
            backend.click(index=1)
        assert fakes.hid.calls == []

    def test_off_screen_point_is_rejected(self, fakes):
        fakes.ax.trees = [[element(1, (200, 2400, 80, 20))]]
        backend = make_backend()
        backend.get_state()
        with pytest.raises(ValueError, match="off screen"):
            backend.click(index=1, state=1)
        with pytest.raises(ValueError, match="off screen"):
            backend.click(x=10, y=-5)
        assert fakes.hid.calls == []

    def test_state_read_failure_after_action_is_distinct(self, fakes, monkeypatch):
        backend = make_backend()

        def broken_state():
            raise RuntimeError("AX timeout")

        monkeypatch.setattr(backend, "get_state", broken_state)
        with pytest.raises(desktop.StateReadError, match="AX timeout"):
            backend.click(x=5, y=6)
        assert fakes.hid.calls == [("click", (5, 6), {"button": "left", "count": 1})]

    def test_index_before_any_state_is_stale(self, fakes):
        with pytest.raises(StaleIndexError):
            make_backend().click(index=1, state=1)

    def test_click_argument_errors(self, fakes):
        backend = make_backend()
        backend.get_state()
        with pytest.raises(ValueError, match="both x and y"):
            backend.click(x=1)
        with pytest.raises(ValueError, match="not both"):
            backend.click(index=1, state=1, x=1, y=2)
        assert fakes.hid.calls == []

    def test_scroll_by_index(self, fakes):
        backend = make_backend()
        backend.get_state()
        backend.scroll(index=2, state=1, direction="up", amount=5)
        assert fakes.hid.calls == [("scroll", (225, 325, "up", 5), {})]

    def test_drag_type_and_key(self, fakes):
        backend = make_backend()
        backend.drag(1, 2, 3, 4)
        backend.type_text("hello")
        last = backend.press_key("Command+A")
        assert fakes.hid.calls == [
            ("drag", (1, 2, 3, 4), {}),
            ("type_text", ("hello",), {}),
            ("press_key", ("Command+A",), {}),
        ]
        assert last.snapshot_id == 3

    def test_set_input_source(self, fakes):
        backend = make_backend()
        snapshot = backend.set_input_source("com.apple.inputmethod.TCIM.Zhuyin")
        assert fakes.source.selected == ["com.apple.inputmethod.TCIM.Zhuyin"]
        assert snapshot.input_source == "com.apple.inputmethod.TCIM.Zhuyin"
        with pytest.raises(ValueError, match="not enabled"):
            backend.set_input_source("missing")

    def test_settle_delay(self, fakes, monkeypatch):
        sleeps = []
        monkeypatch.setattr(desktop.time, "sleep", sleeps.append)
        make_backend(settle_seconds=0.4).type_text("x")
        assert sleeps == [0.4]


class FakeRunningApp:
    def __init__(self, pid):
        self.pid = pid

    def processIdentifier(self):
        return self.pid

    def isFinishedLaunching(self):
        return True


def fake_appkit(apps, *, frontmost=None):
    """apps: (English name, bundle id, pid) of installed and running apps."""
    opened = []
    url_bundles = {}
    for name, bundle_id, _ in apps:
        url_bundles[f"bundle:{bundle_id}"] = bundle_id
        url_bundles[f"file:/Applications/{name}.app"] = bundle_id
    pids = {bundle_id: pid for _, bundle_id, pid in apps}
    paths = {name: f"/Applications/{name}.app" for name, _, _ in apps}

    class Workspace:
        def frontmostApplication(self):
            return frontmost

        def URLForApplicationWithBundleIdentifier_(self, name):
            return f"bundle:{name}" if name in pids else None

        def fullPathForApplication_(self, name):
            return paths.get(name)

        def openApplicationAtURL_configuration_completionHandler_(self, url, config, handler):
            opened.append((url, config.activates))

    class Config:
        activates = False

        def setActivates_(self, value):
            self.activates = value

    appkit = SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=Workspace),
        NSURL=SimpleNamespace(fileURLWithPath_=lambda path: f"file:{path}"),
        NSBundle=SimpleNamespace(
            bundleWithURL_=lambda url: SimpleNamespace(bundleIdentifier=lambda: url_bundles[url]),
        ),
        NSWorkspaceOpenConfiguration=SimpleNamespace(configuration=Config),
        NSRunningApplication=SimpleNamespace(
            runningApplicationsWithBundleIdentifier_=lambda bid: [FakeRunningApp(pids[bid])],
        ),
    )
    return appkit, opened


class TestAppHandling:
    def test_open_app_by_name(self, fakes, monkeypatch):
        appkit, opened = fake_appkit([("Calculator", "com.apple.calculator", 77)])
        monkeypatch.setattr(desktop, "AppKit", appkit)
        snapshot = make_backend().open_app("Calculator")
        assert opened == [("file:/Applications/Calculator.app", True)]
        assert ("raise", 77) in fakes.ax.calls
        assert ("maximize", "main-77") in fakes.ax.calls
        assert snapshot.snapshot_id == 1

    def test_open_app_by_bundle_id(self, fakes, monkeypatch):
        appkit, opened = fake_appkit([("Google Chrome", "com.google.Chrome", 9)])
        monkeypatch.setattr(desktop, "AppKit", appkit)
        make_backend().open_app("com.google.Chrome")
        assert opened == [("bundle:com.google.Chrome", True)]
        assert ("raise", 9) in fakes.ax.calls

    def test_open_unknown_app(self, fakes, monkeypatch):
        appkit, _ = fake_appkit([])
        monkeypatch.setattr(desktop, "AppKit", appkit)
        with pytest.raises(ValueError, match="not found"):
            make_backend().open_app("Nope")

    def test_prepare_frontmost_switches_input_source(self, fakes, monkeypatch):
        appkit, _ = fake_appkit([], frontmost=FakeRunningApp(5))
        monkeypatch.setattr(desktop, "AppKit", appkit)
        fakes.source.current = "com.apple.inputmethod.TCIM.Zhuyin"
        snapshot = make_backend().prepare(None)
        assert fakes.ax.calls[:2] == [("raise", 5), ("maximize", "main-5")]
        assert fakes.source.selected == ["com.apple.keylayout.ABC"]
        assert snapshot.input_source == "com.apple.keylayout.ABC"

    def test_prepare_keeps_matching_input_source(self, fakes, monkeypatch):
        appkit, _ = fake_appkit([], frontmost=FakeRunningApp(5))
        monkeypatch.setattr(desktop, "AppKit", appkit)
        make_backend().prepare(None)
        assert fakes.source.selected == []

    def test_prepare_resets_window_change_tracking(self, fakes, monkeypatch):
        appkit, _ = fake_appkit([], frontmost=FakeRunningApp(5))
        monkeypatch.setattr(desktop, "AppKit", appkit)
        backend = make_backend()
        backend.get_state()
        fakes.ax.window = "window-2"
        snapshot = backend.prepare(None)
        assert fakes.ax.calls[-1][5] is None
        assert not snapshot.window_changed

    def test_prepare_with_app(self, fakes, monkeypatch):
        appkit, opened = fake_appkit([("TextEdit", "com.apple.TextEdit", 3)])
        monkeypatch.setattr(desktop, "AppKit", appkit)
        make_backend().prepare("TextEdit")
        assert opened == [("file:/Applications/TextEdit.app", True)]
        assert ("maximize", "main-3") in fakes.ax.calls


class TestScreenshotScale:
    def test_scale_reported_when_downscaled(self, fakes):
        snapshot = make_backend().get_state()
        assert snapshot.screenshot is not None
        expected = round(snapshot.screenshot.width / 1440.0, 3)
        assert snapshot.screenshot_scale == expected
        if expected != 1.0:
            assert "Screenshot:" in snapshot.render()


class TestSmoke:
    def test_click_uses_the_snapshot_it_just_read(self, fakes, monkeypatch):
        from lincy.gui import smoke

        monkeypatch.setattr(smoke, "DesktopBackend", lambda **kwargs: make_backend())
        monkeypatch.setattr(smoke.permissions, "accessibility_granted", lambda: True)
        monkeypatch.setattr(smoke.permissions, "screen_recording_granted", lambda: True)
        monkeypatch.setattr(smoke, "input_source", fakes.source)
        monkeypatch.setattr(smoke, "save_screenshot", lambda part, path: None)
        monkeypatch.setattr("sys.argv", ["smoke", "--click", "1"])
        smoke.main()
        assert fakes.hid.calls == [("click", (60, 40), {"button": "left", "count": 1})]
