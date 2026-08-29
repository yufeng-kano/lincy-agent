"""Controller and cancellation state for the Textual chat CLI."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from typing import Callable, Literal

from .events import CtxStatusEvent, InterruptStateEvent
from .sink import UiSink


InterruptPhase = Literal["idle", "requested", "pending", "acknowledged", "completed"]


class TurnCancelController:
    """Thread-safe turn cancellation state machine for UI-triggered interrupts."""

    def __init__(self, ui_sink: UiSink | None = None) -> None:
        self._requested = Event()
        self._lock = Lock()
        self._phase: InterruptPhase = "idle"
        self._ui_sink = ui_sink

    @property
    def phase(self) -> InterruptPhase:
        with self._lock:
            return self._phase

    def is_requested(self) -> bool:
        return self._requested.is_set()

    def begin_turn(self) -> None:
        self._requested.clear()
        self._set_phase("idle", "")

    def request(self) -> None:
        self._requested.set()
        self._set_phase("requested", "Interrupt requested")

    def mark_pending(self) -> None:
        if self._requested.is_set():
            self._set_phase("pending", "Cancel pending at safe boundary")

    def acknowledge(self) -> None:
        if self._requested.is_set():
            self._set_phase("acknowledged", "Interrupt acknowledged")

    def complete(self) -> None:
        self._requested.clear()
        self._set_phase("completed", "Interrupted")

    def reset(self) -> None:
        self._requested.clear()
        self._set_phase("idle", "")

    def _set_phase(self, phase: InterruptPhase, message: str) -> None:
        with self._lock:
            self._phase = phase
        if self._ui_sink is not None:
            self._ui_sink.emit(InterruptStateEvent(phase=phase, message=message))


@dataclass(slots=True)
class TextualController:
    """Thin controller that routes UI actions to runtime callbacks."""

    ui_sink: UiSink
    on_submit: Callable[[str], bool] | None = None
    on_exit_request: Callable[[], None] | None = None
    cancel: TurnCancelController | None = None
    ctx_provider: Callable[[], str | None] | None = None

    def submit_input(self, text: str) -> bool:
        """Forward submitted input to the runtime."""
        if self.on_submit is None:
            return False
        result = self.on_submit(text)
        return bool(result)

    def request_interrupt(self) -> None:
        """Request interruption via the turn cancel controller."""
        if self.cancel is None:
            self.ui_sink.emit(
                InterruptStateEvent(
                    phase="requested",
                    message="Interrupt requested (no runtime handler attached)",
                )
            )
            return
        self.cancel.request()
        self.cancel.mark_pending()

    def request_exit(self) -> None:
        """Trigger application exit."""
        if self.on_exit_request is not None:
            self.on_exit_request()

    def refresh_ctx_status(self) -> None:
        """Publish context status text if a provider is configured."""
        if self.ctx_provider is None:
            return
        text = self.ctx_provider() or ""
        self.ui_sink.emit(CtxStatusEvent(text=text))
