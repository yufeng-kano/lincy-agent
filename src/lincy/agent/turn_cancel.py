"""Thread-safe turn cancellation state shared by the console adapter and tools."""

from __future__ import annotations

from threading import Event, Lock

from ..ui.events import InterruptPhase, InterruptStateEvent
from ..ui.sink import UiSink


class TurnCancelController:
    """Thread-safe turn cancellation state machine for externally requested interrupts."""

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
