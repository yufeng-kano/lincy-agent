"""Persistent priority queue backed by filesystem.

Storage layout:
    {queue_dir}/pending/   - messages waiting to be processed (one JSON file each)
    {queue_dir}/active/    - message currently being processed (moved from pending/)

``recover()`` (called once at agent start) moves files left in active/ back to
pending/ and loads pending/ into memory (crash recovery).
Processed messages are deleted (ack).

Time-locked messages (not_before) sit in pending/ on disk but are held in a
separate in-memory delayed pool until their time arrives.  A background
promotion thread moves them to the ready queue every 60 seconds.
"""

import json
import logging
import queue
import threading
from datetime import datetime

from ..timezone_utils import localise as tz_localise, now as tz_now
from pathlib import Path
from typing import Any

from .schema import (
    ClearSentinel,
    CompactSentinel,
    InboundMessage,
    MaintenanceSentinel,
    NewSessionSentinel,
    ReloadSentinel,
    ReloadSystemPromptSentinel,
    RestartSentinel,
    ShutdownSentinel,
)

logger = logging.getLogger(__name__)

_PROMOTION_CHECK_INTERVAL = 60  # seconds

QueueItem = (
    InboundMessage
    | ShutdownSentinel
    | MaintenanceSentinel
    | NewSessionSentinel
    | ReloadSentinel
    | ReloadSystemPromptSentinel
    | CompactSentinel
    | ClearSentinel
    | RestartSentinel
)

# Control sentinels handled at user-input priority: finish current work, then act promptly.
_PROMPT_SENTINELS = (
    NewSessionSentinel,
    ReloadSentinel,
    ReloadSystemPromptSentinel,
    CompactSentinel,
    ClearSentinel,
)
# Lowest priority: only popped when no ready inbound remains, i.e. the agent is idle.
_IDLE_SENTINELS = (MaintenanceSentinel, RestartSentinel)


def _serialize(msg: InboundMessage) -> dict[str, Any]:
    data = {
        "channel": msg.channel,
        "content": msg.content,
        "priority": msg.priority,
        "sender": msg.sender,
        "metadata": msg.metadata,
        "timestamp": msg.timestamp.isoformat(),
    }
    if msg.not_before is not None:
        data["not_before"] = msg.not_before.isoformat()
    return data


def _deserialize(data: dict[str, Any]) -> InboundMessage:
    not_before = None
    if "not_before" in data:
        not_before = tz_localise(datetime.fromisoformat(data["not_before"]))
    return InboundMessage(
        channel=data["channel"],
        content=data["content"],
        priority=data["priority"],
        sender=data["sender"],
        metadata=data.get("metadata", {}),
        timestamp=tz_localise(datetime.fromisoformat(data["timestamp"])),
        not_before=not_before,
    )


def _is_future(dt: datetime | None) -> bool:
    """Return True if *dt* is in the future (timezone-aware comparison)."""
    if dt is None:
        return False
    # Normalize naive datetimes to app timezone for comparison
    if dt.tzinfo is None:
        from ..timezone_utils import get_tz
        dt = dt.replace(tzinfo=get_tz())
    return dt > tz_now()


class PersistentPriorityQueue:
    """Disk-backed priority queue with delayed message support.

    Uses an in-memory ``queue.PriorityQueue`` for fast blocking ``get()``
    and the filesystem for durability across process restarts.

    Messages with ``not_before`` in the future are held in a separate
    delayed pool.  Call ``start_promotion()`` to enable the background
    thread that moves due messages into the ready queue.

    Thread-safe: multiple threads may call ``put()`` concurrently.
    Only one thread should call ``get()`` (the agent main loop).
    """

    def __init__(
        self,
        queue_dir: Path,
        *,
        discard_channels: set[str] | None = None,
    ) -> None:
        self._pending_dir = queue_dir / "pending"
        self._active_dir = queue_dir / "active"
        self._pending_dir.mkdir(parents=True, exist_ok=True)
        self._active_dir.mkdir(parents=True, exist_ok=True)
        self._mem: queue.PriorityQueue[
            tuple[int, int, QueueItem, Path | None]
        ] = queue.PriorityQueue()
        self._seq: int = 0
        self._lock = threading.Lock()
        # Delayed message pool (time-locked)
        self._delayed: list[tuple[InboundMessage, Path]] = []
        self._delayed_lock = threading.Lock()
        self._promotion_stop = threading.Event()
        self._promotion_thread: threading.Thread | None = None
        # Track recurring system messages (heartbeats) in the ready queue
        # so maintenance can ignore them when deciding whether to run.
        self._recurring_ready: int = 0
        self._discard_channels = discard_channels or set()

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------

    def recover(self) -> None:
        """Move active -> pending, then load all pending into memory queue.

        Call once at agent start, before the loop runs; construction stays
        side-effect free on queue contents so a build can be discarded.
        """
        discard_channels = self._discard_channels
        recovered = 0
        for f in sorted(self._active_dir.iterdir()):
            if f.suffix != ".json":
                continue
            f.rename(self._pending_dir / f.name)
            recovered += 1
        if recovered:
            logger.info("Recovered %d in-flight message(s) from last run", recovered)

        discarded = 0
        loaded = 0
        delayed = 0
        for f in sorted(self._pending_dir.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                msg = _deserialize(json.loads(f.read_text()))
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping corrupt queue file: %s", f.name)
                f.unlink()
                continue
            if msg.channel in discard_channels:
                f.unlink()
                discarded += 1
                continue
            # Route time-locked messages to delayed pool
            if _is_future(msg.not_before):
                self._delayed.append((msg, f))
                delayed += 1
                continue
            self._seq += 1
            self._mem.put((msg.priority, self._seq, msg, f))
            if msg.metadata.get("recurring"):
                self._recurring_ready += 1
            loaded += 1

        if loaded:
            logger.info("Loaded %d pending message(s) from disk", loaded)
        if delayed:
            logger.info("Loaded %d delayed message(s) from disk", delayed)
        if discarded:
            logger.info("Discarded %d stale message(s)", discarded)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, msg: QueueItem) -> None:
        """Enqueue a message.

        ``InboundMessage`` is persisted to disk. Control sentinels are transient.
        Time-locked messages (``not_before`` in the future) go to the delayed pool.
        """
        with self._lock:
            self._seq += 1
            if isinstance(msg, ShutdownSentinel):
                # Priority -1 so shutdown is processed before any real message
                self._mem.put((-1, self._seq, msg, None))
                return
            if isinstance(msg, _IDLE_SENTINELS):
                self._mem.put((999, self._seq, msg, None))
                return
            if isinstance(msg, _PROMPT_SENTINELS):
                self._mem.put((0, self._seq, msg, None))
                return
            filename = f"{msg.priority:04d}_{self._seq:08d}.json"
            filepath = self._pending_dir / filename
            filepath.write_text(json.dumps(_serialize(msg)))
            # Route time-locked messages to delayed pool
            if _is_future(msg.not_before):
                with self._delayed_lock:
                    self._delayed.append((msg, filepath))
                return
            self._mem.put((msg.priority, self._seq, msg, filepath))
            if msg.metadata.get("recurring"):
                self._recurring_ready += 1

    def get(self) -> tuple[QueueItem, Path | None]:
        """Block until a message is available.

        Returns ``(message, receipt)``.  Pass *receipt* to ``ack()`` after
        the message has been fully processed.
        """
        while True:
            _, _, msg, filepath = self._mem.get()  # blocks
            if isinstance(msg, InboundMessage) and msg.metadata.get("recurring"):
                self._recurring_ready = max(0, self._recurring_ready - 1)
            if filepath is not None:
                active_path = self._active_dir / filepath.name
                try:
                    filepath.rename(active_path)
                except FileNotFoundError:
                    # File was removed externally (e.g. schedule_action remove)
                    continue
                return msg, active_path
            return msg, None

    def ack(self, receipt: Path | None) -> None:
        """Mark a message as processed (delete from disk)."""
        if receipt is not None:
            receipt.unlink(missing_ok=True)

    def requeue_active(self, receipt: Path, msg: InboundMessage) -> Path:
        """Rewrite an active receipt as a new pending/delayed inbound.

        This updates the in-flight file before moving it back to ``pending/``
        so crash recovery never observes both the old active receipt and a
        separately enqueued retry copy.
        """
        if receipt.parent != self._active_dir:
            raise ValueError("receipt must point to an active queue file")

        with self._lock:
            self._seq += 1
            filename = f"{msg.priority:04d}_{self._seq:08d}.json"
            pending_path = self._pending_dir / filename
            receipt.write_text(json.dumps(_serialize(msg)))
            receipt.rename(pending_path)
            if _is_future(msg.not_before):
                with self._delayed_lock:
                    self._delayed.append((msg, pending_path))
                return pending_path
            self._mem.put((msg.priority, self._seq, msg, pending_path))
            return pending_path

    def pending_count(self) -> int:
        """Number of messages waiting (approximate, for diagnostics)."""
        return self._mem.qsize()

    def pending_inbound_count(self) -> int:
        """Ready messages excluding recurring system messages (heartbeats)."""
        return max(0, self._mem.qsize() - self._recurring_ready)

    # ------------------------------------------------------------------
    # Delayed message promotion
    # ------------------------------------------------------------------

    def start_promotion(self) -> None:
        """Start the background thread that promotes due delayed messages."""
        self._promotion_stop.clear()
        self._promotion_thread = threading.Thread(
            target=self._promotion_loop, name="queue-promote", daemon=True,
        )
        self._promotion_thread.start()

    def stop_promotion(self) -> None:
        """Stop the promotion thread."""
        self._promotion_stop.set()
        if self._promotion_thread:
            self._promotion_thread.join(timeout=5)

    def _promotion_loop(self) -> None:
        """Check delayed pool periodically, promote due messages to mem."""
        while not self._promotion_stop.wait(timeout=_PROMOTION_CHECK_INTERVAL):
            self._promote_due()

    def _promote_due(self) -> None:
        """Move messages whose not_before has passed from delayed to mem."""
        promoted = 0
        with self._delayed_lock:
            remaining = []
            for msg, filepath in self._delayed:
                if not _is_future(msg.not_before):
                    with self._lock:
                        self._seq += 1
                        self._mem.put((msg.priority, self._seq, msg, filepath))
                        if msg.metadata.get("recurring"):
                            self._recurring_ready += 1
                    promoted += 1
                else:
                    remaining.append((msg, filepath))
            self._delayed = remaining
        if promoted:
            logger.info("Promoted %d delayed message(s) to ready queue", promoted)

    # ------------------------------------------------------------------
    # Scan / remove (for schedule_action tool)
    # ------------------------------------------------------------------

    def scan_pending(
        self, *, channel: str | None = None,
    ) -> list[tuple[Path, InboundMessage]]:
        """Scan pending/ directory for messages, optionally filtered by channel."""
        results = []
        for f in sorted(self._pending_dir.iterdir()):
            if f.suffix != ".json":
                continue
            try:
                msg = _deserialize(json.loads(f.read_text()))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if channel is not None and msg.channel != channel:
                continue
            results.append((f, msg))
        return results

    def has_ready_pending_inbound_for_scope(self, scope_id: str) -> bool:
        """Return True when a ready non-system inbound exists for *scope_id*."""
        for _, msg in self.scan_pending():
            if msg.channel == "system":
                continue
            if _is_future(msg.not_before):
                continue
            if msg.metadata.get("scope_id") == scope_id:
                return True
        return False

    def has_ready_pending_inbound_for_channel(self, channel: str) -> bool:
        """Return True when a ready non-system inbound exists for *channel*.

        Used by the preempt checker to avoid cancelling side-effect tools
        due to unrelated traffic on other channels.
        """
        for _, msg in self.scan_pending(channel=channel):
            if msg.channel == "system":
                continue
            if _is_future(msg.not_before):
                continue
            return True
        return False

    def remove_pending(self, filepath: Path) -> bool:
        """Remove a specific pending message by filepath.

        Also removes from delayed pool if present.
        Returns True if file was found and removed.
        """
        if not filepath.exists():
            return False
        filepath.unlink(missing_ok=True)
        # Clean from delayed pool
        with self._delayed_lock:
            self._delayed = [
                (m, p) for m, p in self._delayed if p != filepath
            ]
        return True
