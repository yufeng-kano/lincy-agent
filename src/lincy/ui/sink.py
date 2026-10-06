"""UI sink abstraction used by runtime code to emit typed UI events."""

from __future__ import annotations

import logging
from typing import Protocol

from .events import UiEvent


logger = logging.getLogger(__name__)


class UiSink(Protocol):
    """A write-only sink for typed UI events."""

    def emit(self, event: UiEvent) -> None:
        """Emit one UI event."""
        ...


class FanoutUiSink:
    """Forward every event to several sinks, isolating failures per sink."""

    def __init__(self, sinks: tuple[UiSink, ...]) -> None:
        self._sinks = sinks

    def emit(self, event: UiEvent) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                logger.warning("UI sink %r failed to emit", sink, exc_info=True)
