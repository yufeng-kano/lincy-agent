from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from textual.geometry import Size
from textual.widgets import RichLog

from lincy.tui.app import ChatTextualApp
from lincy.tui.controller import TextualController, TurnCancelController
from lincy.tui.events import CtxStatusEvent, WarningEvent
from lincy.tui.state import UiLogEntry
from lincy.tui.sink import QueueUiSink


@pytest.mark.asyncio
async def test_textual_app_renders_status_and_log_from_events():
    sink = QueueUiSink()
    controller = TextualController(
        ui_sink=sink,
        cancel=TurnCancelController(ui_sink=sink),
    )
    app = ChatTextualApp(controller=controller, event_sink=sink)

    sink.set_on_emit(app.wake_ui_event_drain)

    async with app.run_test() as pilot:
        sink.emit(CtxStatusEvent(text="tok 1/10 (10.0%)"))
        sink.emit(WarningEvent(message="warn"))
        await pilot.pause()

        assert "tok 1/10 (10.0%)" in app.status_text
        assert any("warn" in line for line in app.log_lines)


@pytest.mark.asyncio
async def test_textual_app_ctrl_c_clears_input():
    app = ChatTextualApp()

    async with app.run_test() as pilot:
        input_widget = app.query_one("#input")
        input_widget.insert("hello")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert input_widget.text == ""


def test_textual_app_formats_left_timestamp_with_configured_timezone():
    captured = []
    app = ChatTextualApp()
    app._ui = SimpleNamespace(
        log=SimpleNamespace(
            write=lambda text, **_: captured.append(text),
            max_scroll_y=0,
            scroll_y=0,
        )
    )

    app._write_log_entry(
        UiLogEntry(
            kind="warning",
            text="warn",
            timestamp=datetime(2026, 3, 1, 14, 37, tzinfo=timezone.utc),
        )
    )

    assert captured
    assert captured[0].plain.startswith("22:37:00 ")


@pytest.mark.asyncio
async def test_textual_app_does_not_autofollow_when_user_scrolls_up():
    sink = QueueUiSink()
    controller = TextualController(
        ui_sink=sink,
        cancel=TurnCancelController(ui_sink=sink),
    )
    app = ChatTextualApp(controller=controller, event_sink=sink)
    sink.set_on_emit(app.wake_ui_event_drain)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(80):
            sink.emit(WarningEvent(message=f"warn {index}"))
        await pilot.pause()

        log = app.query_one("#log", RichLog)
        log.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        scroll_y = log.scroll_y
        assert log.max_scroll_y > scroll_y

        await pilot.resize_terminal(100, 20)
        await pilot.pause()
        scroll_y = log.scroll_y

        sink.emit(WarningEvent(message="tail update"))
        await pilot.pause()

        assert log.scroll_y == scroll_y
        assert not log.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_textual_app_keeps_autofollow_after_resize_when_at_tail():
    sink = QueueUiSink()
    controller = TextualController(
        ui_sink=sink,
        cancel=TurnCancelController(ui_sink=sink),
    )
    app = ChatTextualApp(controller=controller, event_sink=sink)
    sink.set_on_emit(app.wake_ui_event_drain)

    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(80):
            sink.emit(WarningEvent(message=f"warn {index}"))
        await pilot.pause()

        log = app.query_one("#log", RichLog)
        log.scroll_end(animate=False, immediate=True, x_axis=False)
        await pilot.pause()
        assert log.is_vertical_scroll_end

        await pilot.resize_terminal(100, 16)
        await pilot.pause()

        sink.emit(WarningEvent(message="tail update after resize"))
        await pilot.pause()

        assert log.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_textual_app_status_height_capped_after_narrow_resize():
    """Status widget stays bounded when terminal narrows and text would wrap."""
    sink = QueueUiSink()
    controller = TextualController(
        ui_sink=sink,
        cancel=TurnCancelController(ui_sink=sink),
    )
    app = ChatTextualApp(controller=controller, event_sink=sink)
    sink.set_on_emit(app.wake_ui_event_drain)

    async with app.run_test(size=(120, 30)) as pilot:
        long_status = "tok 47,439/96,000 (49.4%) | turn=idle | interrupt=idle"
        sink.emit(CtxStatusEvent(text=long_status))
        await pilot.pause()

        await pilot.resize_terminal(30, 15)
        await pilot.pause()

        status = app.query_one("#status")
        log = app.query_one("#log", RichLog)

        # max-height: 3 (outer, including border) caps the status widget.
        assert status.outer_size.height <= 3
        # Log must retain usable space after resize.
        assert log.size.height > 0


@pytest.mark.asyncio
async def test_textual_app_polls_terminal_size_when_resize_event_is_missed():
    app = ChatTextualApp()

    async with app.run_test(size=(100, 24)) as pilot:
        app._read_terminal_size = lambda: Size(72, 18)  # type: ignore[method-assign]

        app._poll_terminal_resize()
        await pilot.pause()

        assert app.size == Size(72, 18)
