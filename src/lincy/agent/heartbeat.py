"""Shared heartbeat scheduling helpers."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta
from typing import Callable

from ..core.schema import is_in_quiet_hours, next_quiet_end
from ..timezone_utils import get_tz, localise as tz_localise, now as tz_now
from .schema import InboundMessage

logger = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^(\d+)([hm])-(\d+)([hm])$")
_STARTUP_CONTENT = (
    "[STARTUP]\n"
    "You just woke up. Check your memory for anything important.\n"
    "Greet the user if appropriate, or stay silent."
)
_HEARTBEAT_TEMPLATE = (
    "[HEARTBEAT]\n"
    "Time: {time}\n\n"
    "You have woken up spontaneously.\n"
    "Check your memory for pending tasks, reminders, or anything\n"
    "you want to tell the user. If nothing to do, do nothing."
)
_PRE_SLEEP_SYNC_CONTENT = "[PRE-SLEEP SYNC]\nMemory sync before quiet hours dormancy."


def parse_interval(spec: str) -> tuple[int, int]:
    """Parse an interval specification into its minute bounds."""
    match = _INTERVAL_RE.match(spec)
    if not match:
        raise ValueError(f"Invalid interval spec: {spec!r}")
    lo = int(match.group(1)) * (60 if match.group(2) == "h" else 1)
    hi = int(match.group(3)) * (60 if match.group(4) == "h" else 1)
    return (hi, lo) if lo > hi else (lo, hi)


def random_delay(spec: str) -> timedelta:
    """Return a randomized delay within an interval specification."""
    lo, hi = parse_interval(spec)
    return timedelta(minutes=random.uniform(lo, hi))


def apply_quiet_hours(dt: datetime, quiet_windows: list[tuple]) -> datetime:
    """Defer a heartbeat that would land within a configured quiet window."""
    if not quiet_windows:
        return dt
    timezone = get_tz()
    if is_in_quiet_hours(dt, quiet_windows, timezone):
        end = next_quiet_end(dt, quiet_windows, timezone)
        logger.info("Heartbeat deferred past quiet hours to %s", end.astimezone(timezone))
        return end
    return dt


def make_heartbeat_message(
    *,
    not_before: datetime | None = None,
    interval_spec: str = "2h-5h",
    is_startup: bool = False,
) -> InboundMessage:
    """Create one recurring heartbeat inbound message."""
    if is_startup:
        content = _STARTUP_CONTENT
    else:
        heartbeat_time = tz_localise(not_before) if not_before else tz_now()
        content = _HEARTBEAT_TEMPLATE.format(time=heartbeat_time.strftime("%Y-%m-%d %H:%M"))
    return InboundMessage(
        channel="system",
        content=content,
        priority=5,
        sender="system",
        metadata={"system": True, "recurring": True, "recur_spec": interval_spec},
        not_before=not_before,
    )


def make_pre_sleep_sync_message(*, not_before: datetime) -> InboundMessage:
    """Create the non-recurring pre-sleep memory sync message."""
    return InboundMessage(
        channel="system",
        content=_PRE_SLEEP_SYNC_CONTENT,
        priority=5,
        sender="system",
        metadata={"system": True, "pre_sleep_sync": True},
        not_before=not_before,
    )


def schedule_pre_sleep_sync(
    *,
    queue,
    was_deferred: bool,
    now: Callable[[], datetime] = tz_now,
) -> None:
    """Replace the pending pre-sleep sync only when quiet-hours deferral occurred."""
    for filepath, message in queue.scan_pending(channel="system"):
        if message.metadata.get("pre_sleep_sync"):
            queue.remove_pending(filepath)
            break
    if not was_deferred:
        return
    sync_time = now() + timedelta(minutes=30)
    queue.put(make_pre_sleep_sync_message(not_before=sync_time))
    logger.info("Scheduled pre-sleep sync at %s", sync_time.isoformat())
