"""Tests for gui/ax.py: tree filtering and rendering over a fake AX layer."""

import base64
import io

from PIL import Image

from lincy.gui.ax import ElementRecord, Snapshot, build_snapshot, draw_marks
from lincy.llm.schema import ContentPart


class Node:
    def __init__(self, role, *, title=None, desc=None, value=None, frame=(0, 0, 10, 10),
                 children=(), actions=(), pid=100, **extra):
        self.attrs = {"AXRole": role, **extra}
        if title is not None:
            self.attrs["AXTitle"] = title
        if desc is not None:
            self.attrs["AXDescription"] = desc
        if value is not None:
            self.attrs["AXValue"] = value
        if frame is not None:
            self.attrs["AXPosition"] = frame[:2]
            self.attrs["AXSize"] = frame[2:]
        self.attrs["AXChildren"] = list(children)
        for child in children:
            child.attrs.setdefault("AXParent", self)
        self.actions = list(actions)
        self.pid = pid


class FakeAccess:
    def __init__(self, window, *, app=None, focused=None, frontmost_pid=100, use_system_app=True):
        self.app = app or Node("AXApplication", frame=None, children=[window])
        window.attrs.setdefault("AXParent", self.app)
        self.apps = {self.app.pid: self.app}
        self.system = Node("AXSystemWide", frame=None)
        if use_system_app:
            self.app.attrs["AXFocusedWindow"] = window
            self.system.attrs["AXFocusedApplication"] = self.app
        else:
            self.app.attrs["AXWindows"] = [window]
        if focused is not None:
            self.system.attrs["AXFocusedUIElement"] = focused
        self._frontmost = frontmost_pid
        self.enabled = []

    def system_wide(self):
        return self.system

    def application(self, pid):
        return self.apps[pid]

    def attrs(self, element, names):
        return {n: element.attrs[n] for n in names if n in element.attrs}

    def actions(self, element):
        return element.actions

    def pid(self, element):
        return element.pid

    def same(self, a, b):
        return a is b

    def frontmost_pid(self):
        return self._frontmost

    def app_info(self, pid):
        return {100: ("TestApp", "com.test.app"), 200: ("Panel", "com.test.panel")}[pid]

    def enable_accessibility(self, app_element):
        self.enabled.append(app_element)


def snap(access, **kwargs):
    params = {"snapshot_id": 1, "max_tree_nodes": 800, "text_limit": 200, "input_source": "com.apple.keylayout.ABC"}
    params.update(kwargs)
    return build_snapshot(access=access, **params)


def element_lines(snapshot):
    text = snapshot.render().splitlines()
    start = text.index('Elements (index role "label" = "value" [x,y,w,h] in screen points):') + 1
    return [line for line in text[start:] if not line.startswith("Focused element:")]


class TestRender:
    def test_line_format_and_header(self):
        window = Node("AXWindow", title="Doc", frame=(0, 30, 800, 600), children=[
            Node("AXButton", title="OK", frame=(10, 40, 80, 20), AXFocused=True),
            Node("AXTextField", desc="Name", value="Bob", frame=(10, 70, 200, 22)),
            Node("AXCheckBox", title="Agree", value=1, frame=(10, 100, 20, 20), AXEnabled=False),
        ])
        snapshot, returned = snap(FakeAccess(window))
        assert returned is window
        assert snapshot.app_name == "TestApp"
        assert snapshot.bundle_id == "com.test.app"
        assert snapshot.window_title == "Doc"
        assert snapshot.window_bbox == (0, 30, 800, 600)
        text = snapshot.render()
        assert text.startswith(
            "App: TestApp (com.test.app, pid 100)\n"
            'Window: "Doc" [0,30,800,600]\n'
            "Input source: com.apple.keylayout.ABC\n"
        )
        assert element_lines(snapshot) == [
            '1 button "OK" (focused) [10,40,80,20]',
            '2 textfield "Name" = "Bob" [10,70,200,22]',
            '3 checkbox "Agree" = "on" (disabled) [10,100,20,20]',
        ]
        assert text.splitlines()[-1] == 'Focused element: 1 button "OK" (focused) [10,40,80,20]'

    def test_render_has_no_snapshot_id(self):
        window = Node("AXWindow", title="W", children=[Node("AXButton", title="A")])
        first, _ = snap(FakeAccess(window), snapshot_id=1)
        second, _ = snap(FakeAccess(window), snapshot_id=2)
        assert first.render() == second.render()

    def test_quotes_are_escaped(self):
        window = Node("AXWindow", title="W", children=[Node("AXButton", title='Say "hi"')])
        snapshot, _ = snap(FakeAccess(window))
        assert element_lines(snapshot) == ['1 button "Say \\"hi\\"" [0,0,10,10]']

    def test_window_changed_flag(self):
        window = Node("AXWindow", title="W")
        other = Node("AXWindow", title="Other")
        same, _ = snap(FakeAccess(window), previous_window=window)
        changed, _ = snap(FakeAccess(window), previous_window=other)
        first, _ = snap(FakeAccess(window))
        assert not same.window_changed
        assert changed.window_changed
        assert not first.window_changed
        assert "focused window changed" in changed.render()


class TestFiltering:
    def test_unnamed_groups_are_flattened(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", frame=(0, 0, 500, 500), children=[
                Node("AXGroup", frame=(0, 0, 400, 400), children=[Node("AXButton", title="Deep")]),
            ]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [e.role for e in snapshot.elements] == ["button"]
        assert snapshot.elements[0].index == 1

    def test_deep_unnamed_nesting_does_not_hit_depth_limit(self):
        node = Node("AXButton", title="Deep")
        for _ in range(100):
            node = Node("AXGroup", frame=(0, 0, 500, 500), children=[node])
        snapshot, _ = snap(FakeAccess(Node("AXWindow", title="W", children=[node])))
        assert [e.label for e in snapshot.elements] == ["Deep"]

    def test_named_group_is_kept(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", desc="Toolbar", children=[Node("AXButton", title="Go")]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [(e.role, e.label) for e in snapshot.elements] == [("group", "Toolbar"), ("button", "Go")]

    def test_anonymous_pressable_group_becomes_button(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", frame=(5, 5, 24, 24), actions=["AXPress"]),
            Node("AXUnknown", frame=(5, 5, 600, 400), actions=["AXPress"]),
            Node("AXGroup", frame=(5, 5, 24, 24), actions=["AXShowMenu"]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [(e.role, e.label, e.bbox) for e in snapshot.elements] == [("button", "", (5, 5, 24, 24))]

    def test_plain_text_group_is_merged(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", frame=(0, 0, 300, 20), children=[
                Node("AXStaticText", value="Hello"),
                Node("AXGroup", children=[Node("AXStaticText", value="world")]),
            ]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert element_lines(snapshot) == ['1 text "Hello world" [0,0,300,20]']

    def test_links_keep_their_own_index(self):
        link = Node("AXLink", frame=(50, 0, 40, 20), children=[Node("AXStaticText", value="here")])
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", frame=(0, 0, 300, 20), children=[
                Node("AXStaticText", value="Click"),
                link,
                Node("AXStaticText", value="now"),
            ]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [(e.role, e.label) for e in snapshot.elements] == [
            ("statictext", "Click"), ("link", "here"), ("statictext", "now"),
        ]

    def test_counter_texts_are_not_merged(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXGroup", children=[Node("AXStaticText", value="Photo"), Node("AXStaticText", value="3 / 10")]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [e.label for e in snapshot.elements] == ["Photo", "3 / 10"]

    def test_button_label_from_text_children_without_duplicates(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXButton", children=[Node("AXStaticText", value="Submit")]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [(e.role, e.label) for e in snapshot.elements] == [("button", "Submit")]

    def test_element_without_frame_is_flattened(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXScrollArea", desc="Content", frame=None, children=[Node("AXButton", title="Inside")]),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [e.label for e in snapshot.elements] == ["Inside"]

    def test_list_shows_visible_rows_only(self):
        rows = [Node("AXRow", frame=(0, 20 * i, 200, 20), children=[
            Node("AXCell", children=[Node("AXStaticText", value=f"file{i}.txt")]),
        ]) for i in range(100)]
        listing = Node("AXOutline", frame=(0, 200, 200, 400), children=rows,
                       AXRows=rows, AXVisibleRows=rows[10:30])
        window = Node("AXWindow", title="Open", children=[listing])
        snapshot, _ = snap(FakeAccess(window))
        assert snapshot.elements[0].role == "outline"
        assert snapshot.elements[0].value == "showing 10-29 of 100 items"
        assert [e.label for e in snapshot.elements[1:]] == [f"file{i}.txt" for i in range(10, 30)]

    def test_all_visible_rows_are_listed(self):
        rows = [Node("AXRow", title=f"r{i}", frame=(0, 20 * i, 200, 20)) for i in range(200)]
        listing = Node("AXOutline", frame=(0, 0, 200, 1200), children=rows,
                       AXRows=rows, AXVisibleRows=rows[:60])
        snapshot, _ = snap(FakeAccess(Node("AXWindow", title="W", children=[listing])))
        assert snapshot.elements[0].value == "showing 0-59 of 200 items"
        assert len(snapshot.elements) == 61

    def test_list_rows_by_frame_when_no_visible_rows_attribute(self):
        rows = [Node("AXRow", title=f"r{i}", frame=(0, 20 * i, 200, 20)) for i in range(10)]
        listing = Node("AXList", frame=(0, 40, 200, 60), children=rows, AXRows=rows)
        snapshot, _ = snap(FakeAccess(Node("AXWindow", title="W", children=[listing])))
        assert snapshot.elements[0].value == "showing 2-4 of 10 items"
        assert [e.label for e in snapshot.elements[1:]] == ["r2", "r3", "r4"]

    def test_tab_and_switch_roles(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXRadioButton", title="General", value=1, AXSubrole="AXTabButton"),
            Node("AXRadioButton", title="Small", value=0),
            Node("AXCheckBox", title="Wi-Fi", value=1, AXSubrole="AXSwitch"),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert [(e.role, e.value) for e in snapshot.elements] == [
            ("tab", "on"), ("radio", "off"), ("switch", "on"),
        ]

    def test_placeholder_is_label_fallback(self):
        window = Node("AXWindow", title="W", children=[
            Node("AXTextField", value="", AXPlaceholderValue="Your answer"),
        ])
        snapshot, _ = snap(FakeAccess(window))
        assert element_lines(snapshot) == ['1 textfield "Your answer" [0,0,10,10]']


class TestLimits:
    def test_max_tree_nodes_truncates(self):
        window = Node("AXWindow", title="W", children=[Node("AXButton", title=f"b{i}") for i in range(10)])
        snapshot, _ = snap(FakeAccess(window), max_tree_nodes=4)
        assert len(snapshot.elements) == 4
        assert snapshot.truncated
        assert "tree truncated at 4 elements" in snapshot.render()

    def test_exact_cap_is_not_truncated(self):
        window = Node("AXWindow", title="W", children=[Node("AXButton", title=f"b{i}") for i in range(4)])
        snapshot, _ = snap(FakeAccess(window), max_tree_nodes=4)
        assert not snapshot.truncated

    def test_text_limit_and_newlines(self):
        window = Node("AXWindow", title="W", children=[Node("AXStaticText", value="line1\nline2 " + "x" * 50)])
        snapshot, _ = snap(FakeAccess(window), text_limit=20)
        assert snapshot.elements[0].label == "line1\\nline2 " + "x" * 7 + "..."


class TestRootSelection:
    def test_falls_back_to_frontmost_app(self):
        window = Node("AXWindow", title="Front", children=[Node("AXButton", title="A")])
        access = FakeAccess(window, use_system_app=False)
        snapshot, returned = snap(access)
        assert returned is window
        assert snapshot.window_title == "Front"
        assert access.enabled == [access.app]

    def test_no_window(self):
        app = Node("AXApplication", frame=None)
        access = FakeAccess(Node("AXWindow", title="unused"), app=app, use_system_app=False)
        app.attrs.pop("AXWindows")
        snapshot, returned = snap(access)
        assert returned is None
        assert snapshot.elements == []
        assert "Window: none" in snapshot.render()

    def test_focus_in_other_process_panel_is_listed_first(self):
        name_field = Node("AXTextField", desc="Go to", value="~/Desktop", pid=200, AXFocused=True)
        panel = Node("AXWindow", title="Open", pid=200, frame=(100, 100, 600, 400), children=[
            Node("AXButton", title="Cancel", pid=200),
            name_field,
        ])
        panel_app = Node("AXApplication", frame=None, pid=200, children=[panel])
        panel.attrs["AXParent"] = panel_app
        window = Node("AXWindow", title="Form", children=[Node("AXButton", title="Browse")])
        access = FakeAccess(window, focused=name_field)
        snapshot, returned = snap(access)
        assert returned is window
        assert snapshot.outside_focus == 'window "Open" of Panel (com.test.panel)'
        assert snapshot.outside_focus_count == 2
        lines = element_lines(snapshot)
        assert lines == [
            '--- Keyboard focus is outside the window, in window "Open" of Panel (com.test.panel) ---',
            '1 button "Cancel" [0,0,10,10]',
            '2 textfield "Go to" = "~/Desktop" (focused) [0,0,10,10]',
            "--- Window ---",
            '3 button "Browse" [0,0,10,10]',
        ]

    def test_outside_panel_survives_truncation(self):
        field = Node("AXTextField", desc="Name", pid=200)
        panel = Node("AXWindow", title="Save", pid=200, children=[field])
        panel_app = Node("AXApplication", frame=None, pid=200, children=[panel])
        panel.attrs["AXParent"] = panel_app
        window = Node("AXWindow", title="W", children=[Node("AXButton", title=f"b{i}") for i in range(10)])
        snapshot, _ = snap(FakeAccess(window, focused=field), max_tree_nodes=3)
        assert snapshot.elements[0].label == "Name"
        assert snapshot.truncated

    def test_focus_inside_window_adds_nothing(self):
        button = Node("AXButton", title="A", AXFocused=True)
        window = Node("AXWindow", title="W", children=[Node("AXGroup", children=[button])])
        snapshot, _ = snap(FakeAccess(window, focused=button))
        assert snapshot.outside_focus == ""
        assert [e.label for e in snapshot.elements] == ["A"]


def test_draw_marks_scales_to_max_width():
    img = Image.new("RGB", (400, 200), color=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    shot = ContentPart(type="image", media_type="image/jpeg",
                       data=base64.b64encode(buf.getvalue()).decode("ascii"), width=400, height=200)
    elements = [ElementRecord(index=1, role="button", label="A", value="", bbox=(10, 10, 50, 20),
                              focused=False, enabled=True)]
    marked = draw_marks(shot, elements, max_width=200, quality=80)
    assert (marked.width, marked.height) == (200, 100)
    decoded = Image.open(io.BytesIO(base64.b64decode(marked.data)))
    assert decoded.size == (200, 100)


def test_snapshot_model_defaults():
    snapshot = Snapshot(
        snapshot_id=1, app_name="A", bundle_id="b", pid=1, window_title="T",
        window_bbox=(0, 0, 1, 1), window_changed=False, input_source="x",
        elements=[], truncated=False,
    )
    assert snapshot.screenshot is None
    assert snapshot.outside_focus == ""
