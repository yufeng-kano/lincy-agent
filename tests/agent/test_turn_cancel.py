from lincy.agent.turn_cancel import TurnCancelController
from lincy.ui.events import InterruptStateEvent


class _ListSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_turn_cancel_controller_emits_state_transitions():
    sink = _ListSink()
    cancel = TurnCancelController(ui_sink=sink)

    cancel.begin_turn()
    cancel.request()
    cancel.mark_pending()
    cancel.acknowledge()
    cancel.complete()

    phases = [
        event.phase
        for event in sink.events
        if isinstance(event, InterruptStateEvent)
    ]
    assert phases == ["idle", "requested", "pending", "acknowledged", "completed"]
    assert cancel.is_requested() is False
