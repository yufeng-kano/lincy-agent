"""Shared queue-backed background task dispatch."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


def dispatch_background_task(
    queue: Any,
    slots: threading.BoundedSemaphore | threading.Lock | None,
    label: str,
    run_fn: Callable[[], Any],
    format_fn: Callable[[Any | None, Exception | None], Any],
) -> str | None:
    """Dispatch work and inject its formatted result into the inbound queue.

    Returns a busy response when a concurrency slot cannot be reserved, otherwise
    ``None`` once the background thread has started.
    """
    if slots is not None and not slots.acquire(blocking=False):
        return f"[{label} BUSY] Another background task is already running."

    def run() -> None:
        try:
            queue.put(format_fn(run_fn(), None))
        except Exception as exc:
            queue.put(format_fn(None, exc))
        finally:
            if slots is not None:
                slots.release()

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
    except Exception:
        if slots is not None:
            slots.release()
        raise
    return None
