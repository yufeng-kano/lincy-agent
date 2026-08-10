"""Daily maintenance scheduling."""

from __future__ import annotations

from datetime import date, datetime
import logging
import threading

from ..core.schema import MaintenanceConfig
from ..timezone_utils import get_tz
from .queue import PersistentPriorityQueue
from .schema import MaintenanceSentinel

logger = logging.getLogger(__name__)


class MaintenanceScheduler:
    """Background timer that enqueues maintenance within its daily window."""

    def __init__(self, queue: PersistentPriorityQueue, config: MaintenanceConfig):
        self._queue = queue
        self._config = config
        self._timezone = get_tz()
        self._ran_today = False
        self._last_date: date | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def mark_done(self) -> None:
        self._ran_today = True

    def _loop_once(self) -> bool:
        now = datetime.now(self._timezone)
        today = now.date()
        if self._last_date != today:
            self._ran_today = False
            self._last_date = today
        if self._ran_today or now.hour < self._config.daily_hour:
            return False
        if now.hour >= self._config.latest_hour:
            self._ran_today = True
            logger.info(
                "Maintenance window passed (%02d:00-%02d:00), skipping today",
                self._config.daily_hour,
                self._config.latest_hour,
            )
            return False
        self._queue.put(MaintenanceSentinel())
        return True

    def _loop(self) -> None:
        while not self._stop.wait(timeout=60):
            if self._loop_once():
                self._stop.wait(timeout=self._config.retry_interval_minutes * 60)
