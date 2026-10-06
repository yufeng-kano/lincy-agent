"""Console channel adapter: operator input submitted through the AgentHandle.

The channel keeps the persisted name ``cli`` because brain prompts and
session files already reference it; renaming is a separate migration.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..handle import AgentBusy
from ..schema import InboundMessage, OutboundMessage
from ...ui.events import ProcessingFinishedEvent

if TYPE_CHECKING:
    from ...ui.sink import UiSink
    from ..core import AgentCore
    from ..turn_cancel import TurnCancelController


class ConsoleAdapter:
    """Operator channel: one in-flight turn at a time, output via UI events."""

    channel_name = "cli"
    priority = 0

    def __init__(
        self,
        *,
        ui_sink: UiSink,
        user_id: str,
        cancel_controller: TurnCancelController | None = None,
    ) -> None:
        self._ui_sink = ui_sink
        self._user_id = user_id
        self._cancel = cancel_controller

        self._agent: AgentCore | None = None
        self._turn_done = threading.Event()
        self._turn_done.set()

    # ------------------------------------------------------------------
    # ChannelAdapter protocol
    # ------------------------------------------------------------------

    def start(self, agent: AgentCore) -> None:
        self._agent = agent
        self._turn_done.set()

    def send(self, message: OutboundMessage) -> None:
        # Display is handled by UI events; the console has no delivery target.
        pass

    def on_turn_start(self, channel: str) -> None:
        if channel == self.channel_name and self._cancel is not None:
            self._cancel.begin_turn()

    def on_turn_complete(self) -> None:
        interrupted = False
        if self._cancel is not None:
            interrupted = self._cancel.phase in {"requested", "pending", "acknowledged"}
            if interrupted:
                self._cancel.acknowledge()
                self._cancel.complete()
            else:
                self._cancel.reset()
        self._ui_sink.emit(ProcessingFinishedEvent(channel=self.channel_name, interrupted=interrupted))
        self._turn_done.set()

    def stop(self) -> None:
        self._turn_done.set()

    # ------------------------------------------------------------------
    # Operator input
    # ------------------------------------------------------------------

    def submit(self, content: str) -> None:
        """Enqueue one operator message; reject while a console turn is in flight."""
        # The HTTP server is up before the agent loop starts the adapters.
        if self._agent is None:
            raise AgentBusy("Agent is still starting.")
        if not self._turn_done.is_set():
            raise AgentBusy("Still processing the previous turn.")
        self._turn_done.clear()
        self._agent.enqueue(
            InboundMessage(
                channel=self.channel_name,
                content=content,
                priority=self.priority,
                sender=self._user_id,
            )
        )
