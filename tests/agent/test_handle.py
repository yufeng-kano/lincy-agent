"""The AgentHandle implementation built by build_agent."""

from unittest.mock import MagicMock

import pytest

from lincy.agent.adapters.console import ConsoleAdapter
from lincy.agent.build import _Handle
from lincy.agent.handle import (
    AgentBusy,
    AgentState,
    InvalidRequest,
    ShellSessionNotFound,
    UnsupportedChannel,
)
from lincy.agent.turn_cancel import TurnCancelController
from lincy.tools.builtin.shell_task import ShellTaskManager


class _NullSink:
    def emit(self, event) -> None:
        pass


def _handle(shell=None):
    core = MagicMock()
    core.user_id = "yufeng"
    core.is_busy.return_value = False
    console = ConsoleAdapter(ui_sink=_NullSink(), user_id="yufeng")
    core.adapters = {
        "system": object(),
        "gmail": object(),
        "cli": console,
        "discord": object(),
    }
    console.start(core)
    cancel = TurnCancelController()
    handle = _Handle(
        core=core,
        console_adapter=console,
        cancel_controller=cancel,
        shell_task_manager=shell or ShellTaskManager(),
    )
    return handle, core, cancel


def test_channels_put_cli_first_and_hide_system():
    handle, _, _ = _handle()

    assert handle.channels() == ["cli", "discord", "gmail"]


def test_submit_defaults_to_cli_through_console_adapter():
    handle, core, _ = _handle()
    handle.mark_ready()

    handle.submit("  hello  ")

    msg = core.enqueue.call_args.args[0]
    assert (msg.channel, msg.content, msg.sender) == ("cli", "hello", "yufeng")
    assert msg.metadata == {}


def test_submit_cli_while_turn_in_flight_raises_busy():
    handle, core, _ = _handle()
    handle.mark_ready()
    handle.submit("first")

    with pytest.raises(AgentBusy) as exc:
        handle.submit("second")

    assert exc.value.status_code == 409
    assert core.enqueue.call_count == 1


def test_submit_any_channel_before_ready_raises_busy():
    handle, core, _ = _handle()

    with pytest.raises(AgentBusy, match="still starting"):
        handle.submit("ping", "discord")
    core.enqueue.assert_not_called()


def test_submit_other_channel_enqueues_with_web_console_source():
    handle, core, _ = _handle()
    handle.mark_ready()

    handle.submit("ping", "discord")

    msg = core.enqueue.call_args.args[0]
    assert (msg.channel, msg.content, msg.priority, msg.sender) == ("discord", "ping", 0, "yufeng")
    assert msg.metadata == {"source": "web_console"}


@pytest.mark.parametrize("channel", ["system", "web", "line"])
def test_submit_rejects_unregistered_or_system_channel(channel):
    handle, core, _ = _handle()

    with pytest.raises(UnsupportedChannel, match="choose one of: cli, discord, gmail") as exc:
        handle.submit("hi", channel)

    assert exc.value.status_code == 400
    core.enqueue.assert_not_called()


def test_submit_rejects_blank_content():
    handle, core, _ = _handle()

    with pytest.raises(InvalidRequest, match="content is required") as exc:
        handle.submit("   ")

    assert exc.value.status_code == 400
    core.enqueue.assert_not_called()


def test_request_reload_maps_targets():
    handle, core, _ = _handle()

    handle.request_reload("all")
    handle.request_reload("system-prompt")

    core.request_reload.assert_called_once_with()
    core.request_reload_system_prompt.assert_called_once_with()
    with pytest.raises(InvalidRequest):
        handle.request_reload("boot-files")


def test_state_transitions():
    handle, core, _ = _handle()
    assert handle.state() is AgentState.STARTING

    handle.mark_ready()
    assert handle.state() is AgentState.READY

    core.is_busy.return_value = True
    assert handle.state() is AgentState.BUSY

    handle.request_restart()
    assert handle.state() is AgentState.STOPPING
    core.request_restart.assert_called_once_with()


def test_request_shutdown_marks_stopping():
    handle, core, _ = _handle()
    handle.mark_ready()

    handle.request_shutdown(graceful=False)

    assert handle.state() is AgentState.STOPPING
    core.request_shutdown.assert_called_once_with(graceful=False)


def test_queue_requests_forward_to_core():
    handle, core, cancel = _handle()

    handle.request_new_session()
    handle.request_compact()
    handle.request_clear()
    handle.cancel_turn()

    core.request_new_session.assert_called_once_with()
    core.request_compact.assert_called_once_with()
    core.request_clear.assert_called_once_with()
    assert cancel.is_requested() is True


def test_shell_unknown_session_raises_not_found():
    handle, _, _ = _handle()

    with pytest.raises(ShellSessionNotFound) as exc:
        handle.shell_send("sh_0001", text="y", key=None)
    assert exc.value.status_code == 404
    with pytest.raises(ShellSessionNotFound):
        handle.shell_cancel("sh_0001")
    assert handle.shell_sessions() == []


@pytest.mark.parametrize(
    ("text", "key"),
    [(None, None), ("y", "enter"), (None, "f1")],
)
def test_shell_send_requires_exactly_one_valid_input(text, key):
    handle, _, _ = _handle()

    with pytest.raises(InvalidRequest):
        handle.shell_send("sh_0001", text=text, key=key)


def test_shell_send_routes_text_and_keys():
    shell = MagicMock()
    shell.has_session.return_value = True
    shell.send_input.return_value = "Sent input"
    shell.send_key.return_value = "Sent Enter"
    handle, _, _ = _handle(shell)

    assert handle.shell_send("sh_0001", text="yes", key=None) == "Sent input"
    assert handle.shell_send("sh_0001", text=None, key="enter") == "Sent Enter"
    shell.send_input.assert_called_once_with("yes", session_id="sh_0001")
    shell.send_key.assert_called_once_with("enter", session_id="sh_0001")
