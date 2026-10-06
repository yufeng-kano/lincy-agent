"""Tests for gui/input.py: key parsing, text chunking and event sequences.

No event is ever posted: CGEventPost is replaced with a function that fails
the test, and _post records the created events instead.
"""

import pytest
import Quartz

from lincy.gui import input as hid
from lincy.gui.input import KeyPress, parse_key, text_chunks

# US/ABC layout subset: char -> (keycode, needs shift)
US_KEYMAP = {
    "a": (0x00, False), "A": (0x00, True), "g": (0x05, False), "G": (0x05, True),
    "v": (0x09, False), "1": (0x12, False), "!": (0x12, True), "=": (0x18, False),
    "+": (0x18, True), "/": (0x2C, False), "?": (0x2C, True), ".": (0x2F, False),
    ",": (0x2B, False), " ": (0x31, False),
}
CMD = Quartz.kCGEventFlagMaskCommand
SHIFT = Quartz.kCGEventFlagMaskShift


@pytest.fixture(autouse=True)
def no_real_events(monkeypatch):
    def forbidden(*args):
        raise AssertionError("CGEventPost must not be called in tests")

    monkeypatch.setattr(Quartz, "CGEventPost", forbidden)
    events = []
    monkeypatch.setattr(hid, "_post", events.append)
    monkeypatch.setattr(hid, "layout_keymap", lambda: US_KEYMAP)
    return events


def describe(event):
    kind = Quartz.CGEventGetType(event)
    names = {Quartz.kCGEventKeyDown: "down", Quartz.kCGEventKeyUp: "up", Quartz.kCGEventFlagsChanged: "flags"}
    if kind in names:
        _, text = Quartz.CGEventKeyboardGetUnicodeString(event, 32, None, None)
        return (
            names[kind],
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode),
            Quartz.CGEventGetFlags(event),
            text,
        )
    loc = Quartz.CGEventGetLocation(event)
    return (kind, round(loc.x), round(loc.y),
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGMouseEventClickState))


class TestParseKey:
    @pytest.mark.parametrize(("spec", "expected"), [
        ("Return", KeyPress(0x24, ())),
        ("enter", KeyPress(0x24, ())),
        ("Escape", KeyPress(0x35, ())),
        ("esc", KeyPress(0x35, ())),
        ("Tab", KeyPress(0x30, ())),
        ("space", KeyPress(0x31, ())),
        ("Backspace", KeyPress(0x33, ())),
        ("ForwardDelete", KeyPress(0x75, ())),
        ("Up", KeyPress(0x7E, ())),
        ("PageDown", KeyPress(0x79, ())),
        ("F12", KeyPress(0x6F, ())),
        ("ctrl+Tab", KeyPress(0x30, ("control",))),
    ])
    def test_named_keys(self, spec, expected):
        assert parse_key(spec, keymap=US_KEYMAP) == expected

    def test_modifiers_and_aliases(self):
        assert parse_key("Command+Shift+G", keymap=US_KEYMAP) == KeyPress(0x05, ("command", "shift"))
        assert parse_key("cmd+alt+ctrl+v", keymap=US_KEYMAP) == KeyPress(0x09, ("command", "option", "control"))
        assert parse_key(" Option + v ", keymap=US_KEYMAP) == KeyPress(0x09, ("option",))

    def test_printable_symbols_come_from_layout(self):
        assert parse_key("/", keymap=US_KEYMAP) == KeyPress(0x2C, ())
        assert parse_key("Command+.", keymap=US_KEYMAP) == KeyPress(0x2F, ("command",))
        assert parse_key("Command+,", keymap=US_KEYMAP) == KeyPress(0x2B, ("command",))

    def test_shifted_symbol_adds_shift(self):
        assert parse_key("?", keymap=US_KEYMAP) == KeyPress(0x2C, ("shift",))
        assert parse_key("Command+?", keymap=US_KEYMAP) == KeyPress(0x2C, ("command", "shift"))

    def test_plus_key(self):
        assert parse_key("Command++", keymap=US_KEYMAP) == KeyPress(0x18, ("command", "shift"))
        assert parse_key("+", keymap=US_KEYMAP) == KeyPress(0x18, ("shift",))

    def test_bare_uppercase_letter_adds_shift(self):
        assert parse_key("A", keymap=US_KEYMAP) == KeyPress(0x00, ("shift",))
        assert parse_key("Shift+A", keymap=US_KEYMAP) == KeyPress(0x00, ("shift",))
        assert parse_key("a", keymap=US_KEYMAP) == KeyPress(0x00, ())

    def test_uppercase_with_modifier_ignores_case(self):
        assert parse_key("Command+A", keymap=US_KEYMAP) == KeyPress(0x00, ("command",))

    @pytest.mark.parametrize("spec", ["Hyper+a", "super+a"])
    def test_unknown_modifier(self, spec):
        with pytest.raises(ValueError, match="unsupported modifier"):
            parse_key(spec, keymap=US_KEYMAP)

    @pytest.mark.parametrize("spec", ["Insert", "kp_1", "Command+", "", "\u00e9"])
    def test_unknown_key(self, spec):
        with pytest.raises(ValueError, match="unsupported key"):
            parse_key(spec, keymap=US_KEYMAP)

    def test_default_keymap_comes_from_layout_keymap(self):
        assert parse_key("1") == KeyPress(0x12, ())


class TestTextChunks:
    def test_short_text(self):
        assert text_chunks("hello") == ["hello"]

    def test_splits_at_20_utf16_units(self):
        assert text_chunks("x" * 45) == ["x" * 20, "x" * 20, "x" * 5]

    def test_surrogate_pairs_are_not_split(self):
        text = "x" * 19 + "\U0001F600" + "y"
        assert text_chunks(text) == ["x" * 19, "\U0001F600y"]

    def test_newlines_become_their_own_chunks(self):
        assert text_chunks("a\nb\r\nc\rd") == ["a", "\n", "b", "\n", "c", "\n", "d"]

    def test_cjk(self):
        assert text_chunks("\u4e2d\u6587" * 15) == ["\u4e2d\u6587" * 10, "\u4e2d\u6587" * 5]


class TestEventSequences:
    def test_click_double(self, no_real_events):
        hid.click(100, 200, button="left", count=2)
        assert [describe(e) for e in no_real_events] == [
            (Quartz.kCGEventMouseMoved, 100, 200, 0),
            (Quartz.kCGEventLeftMouseDown, 100, 200, 1),
            (Quartz.kCGEventLeftMouseUp, 100, 200, 1),
            (Quartz.kCGEventLeftMouseDown, 100, 200, 2),
            (Quartz.kCGEventLeftMouseUp, 100, 200, 2),
        ]

    def test_right_click(self, no_real_events):
        hid.click(5, 6, button="right")
        kinds = [Quartz.CGEventGetType(e) for e in no_real_events]
        assert kinds == [Quartz.kCGEventMouseMoved, Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp]

    def test_click_rejects_bad_args(self, no_real_events):
        with pytest.raises(ValueError, match="mouse button"):
            hid.click(1, 1, button="middle")
        with pytest.raises(ValueError, match="click count"):
            hid.click(1, 1, count=5)
        assert no_real_events == []

    def test_drag(self, no_real_events):
        hid.drag(0, 0, 100, 50)
        described = [describe(e) for e in no_real_events]
        assert described[0][0] == Quartz.kCGEventMouseMoved
        assert described[1][:3] == (Quartz.kCGEventLeftMouseDown, 0, 0)
        assert [d[0] for d in described[2:-1]] == [Quartz.kCGEventLeftMouseDragged] * 10
        assert described[-2][1:3] == (100, 50)
        assert described[-1][:3] == (Quartz.kCGEventLeftMouseUp, 100, 50)

    @pytest.mark.parametrize(("direction", "axis1", "axis2"), [
        ("up", 3, 0), ("down", -3, 0), ("left", 0, 3), ("right", 0, -3),
    ])
    def test_scroll(self, no_real_events, direction, axis1, axis2):
        hid.scroll(300, 400, direction, 3)
        wheel = no_real_events[-1]
        assert Quartz.CGEventGetType(wheel) == Quartz.kCGEventScrollWheel
        assert Quartz.CGEventGetIntegerValueField(wheel, Quartz.kCGScrollWheelEventDeltaAxis1) == axis1
        assert Quartz.CGEventGetIntegerValueField(wheel, Quartz.kCGScrollWheelEventDeltaAxis2) == axis2
        loc = Quartz.CGEventGetLocation(wheel)
        assert (loc.x, loc.y) == (300, 400)

    def test_scroll_rejects_bad_direction(self, no_real_events):
        with pytest.raises(ValueError, match="scroll direction"):
            hid.scroll(1, 1, "sideways")
        assert no_real_events == []

    def test_type_text_with_newline(self, no_real_events):
        hid.type_text("ab\n\u4e2d")
        assert [describe(e) for e in no_real_events] == [
            ("down", 0, 0, "ab"), ("up", 0, 0, "ab"),
            ("down", 0x24, 0, "\r"), ("up", 0x24, 0, "\r"),
            ("down", 0, 0, "\u4e2d"), ("up", 0, 0, "\u4e2d"),
        ]

    def test_press_key_with_modifiers(self, no_real_events):
        hid.press_key("Command+Shift+G")
        assert [describe(e)[:3] for e in no_real_events] == [
            # Modifier key events are flags-changed events, as from hardware.
            ("flags", 0x37, CMD),
            ("flags", 0x38, CMD | SHIFT),
            ("down", 0x05, CMD | SHIFT),
            ("up", 0x05, CMD | SHIFT),
            ("flags", 0x38, CMD),
            ("flags", 0x37, 0),
        ]

    def test_press_key_unknown_posts_nothing(self, no_real_events):
        with pytest.raises(ValueError, match="unsupported key"):
            hid.press_key("Command+Insert")
        assert no_real_events == []
