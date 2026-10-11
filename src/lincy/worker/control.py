"""Control handles for running worker tasks: cancel and append from the brain."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class WorkerControl:
    """Per-task handle shared between the brain tools and the worker loop."""

    label: str
    description: str
    cancel: threading.Event = field(default_factory=threading.Event)
    _appends: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, text: str) -> None:
        with self._lock:
            self._appends.append(text)

    def drain_appends(self) -> list[str]:
        with self._lock:
            drained = self._appends
            self._appends = []
        return drained


class WorkerControlRegistry:
    """Running worker tasks by label; entries live only while a task runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[str, WorkerControl] = {}

    def register(self, control: WorkerControl) -> None:
        with self._lock:
            self._running[control.label] = control

    def unregister(self, label: str) -> None:
        with self._lock:
            self._running.pop(label, None)

    def get(self, label: str) -> WorkerControl | None:
        with self._lock:
            return self._running.get(label)
