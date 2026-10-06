"""Tests for ConsoleAdapter submit and turn bookkeeping."""

from unittest.mock import MagicMock

import pytest

from lincy.agent.adapters.console import ConsoleAdapter
from lincy.agent.handle import AgentBusy
from lincy.agent.turn_cancel import TurnCancelController
from lincy.ui.events import ProcessingFinishedEvent


class _ListSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _adapter(cancel: TurnCancelController | None = None) -> tuple[ConsoleAdapter, MagicMock, _ListSink]:
    sink = _ListSink()
    adapter = ConsoleAdapter(ui_sink=sink, user_id="u", cancel_controller=cancel)
    agent = MagicMock()
    adapter.start(agent)
    return adapter, agent, sink


def test_submit_enqueues_cli_message():
    adapter, agent, _ = _adapter()

    adapter.submit("hello")

    msg = agent.enqueue.call_args.args[0]
    assert msg.channel == "cli"
    assert msg.content == "hello"
    assert msg.sender == "u"
    assert msg.priority == 0


def test_submit_raises_busy_while_turn_in_flight():
    adapter, agent, _ = _adapter()
    adapter.submit("first")

    with pytest.raises(AgentBusy, match="Still processing"):
        adapter.submit("second")

    assert agent.enqueue.call_count == 1


def test_submit_accepted_again_after_turn_complete():
    adapter, agent, sink = _adapter()
    adapter.submit("first")

    adapter.on_turn_complete()
    adapter.submit("second")

    assert agent.enqueue.call_count == 2
    assert isinstance(sink.events[-1], ProcessingFinishedEvent)


def test_submit_before_start_raises_busy():
    adapter = ConsoleAdapter(ui_sink=_ListSink(), user_id="u")

    with pytest.raises(AgentBusy, match="starting"):
        adapter.submit("hello")


def test_turn_complete_reports_interrupt_when_cancel_requested():
    cancel = TurnCancelController()
    adapter, _, sink = _adapter(cancel)
    adapter.on_turn_start("cli")
    cancel.request()

    adapter.on_turn_complete()

    finished = sink.events[-1]
    assert isinstance(finished, ProcessingFinishedEvent)
    assert finished.interrupted is True
    assert cancel.phase == "completed"
