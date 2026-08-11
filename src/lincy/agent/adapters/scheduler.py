"""Scheduler channel adapter: heartbeat and scheduled wake-up messages.

On startup, clears old system heartbeats from pending/. It can optionally
enqueue an immediate startup heartbeat. After each heartbeat turn completes,
AgentCore._process_inbound auto-creates the next one with a random delay.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..heartbeat import apply_quiet_hours, make_heartbeat_message, random_delay
from ..schema import InboundMessage, OutboundMessage
from ...timezone_utils import now as tz_now

if TYPE_CHECKING:
    from ..core import AgentCore

logger = logging.getLogger(__name__)

_STARTUP_CONTENT = (
    "[STARTUP]\n"
    "You just woke up. Check your memory for anything important.\n"
    "Greet the user if appropriate, or stay silent."
)


def make_upgrade_notice_message(
    *,
    content: str,
    not_before=None,
) -> InboundMessage:
    """Create a one-shot system message carrying kernel upgrade notes."""
    return InboundMessage(
        channel="system",
        content=content,
        priority=5,
        sender="system",
        metadata={
            "system": True,
            "upgrade_notice": True,
        },
        not_before=not_before,
    )


_PRE_SLEEP_SYNC_CONTENT = (
    "[PRE-SLEEP SYNC]\n"
    "Memory sync before quiet hours dormancy."
)


def make_pre_sleep_sync_message(
    *,
    not_before,
) -> InboundMessage:
    """Create a pre-sleep sync InboundMessage (no ``recurring`` flag)."""
    return InboundMessage(
        channel="system",
        content=_PRE_SLEEP_SYNC_CONTENT,
        priority=5,
        sender="system",
        metadata={"system": True, "pre_sleep_sync": True},
        not_before=not_before,
    )


class SchedulerAdapter:
    """System channel adapter for heartbeat and scheduled actions.

    Thin adapter: ``start()`` optionally seeds the queue with a startup
    heartbeat. The recurring logic lives in ``AgentCore._process_inbound``.
    """

    channel_name = "system"
    priority = 5

    def __init__(
        self,
        *,
        interval: str = "2h-5h",
        enqueue_startup: bool = False,
        enqueue_upgrade_notice: bool = True,
        upgrade_message: str = "",
        quiet_windows: list[tuple] | None = None,
    ) -> None:
        self.interval = interval
        self._enqueue_startup = enqueue_startup
        self._enqueue_upgrade_notice = enqueue_upgrade_notice
        self._upgrade_message = upgrade_message
        self._quiet_windows = quiet_windows or []

    def start(self, agent: AgentCore) -> None:
        """Seed the recurring heartbeat chain.

        Preserves future pending heartbeats across restart to avoid
        resetting the prompt-cache warming timer.
        """
        q = agent._queue
        if q is None:
            return

        # Scan pending system messages from previous run.
        system_pending = [
            (fp, msg) for fp, msg in q.scan_pending(channel="system")
            if msg.metadata.get("system")
        ]

        # When no immediate startup turn is requested, preserve a
        # still-future recurring heartbeat instead of clearing and
        # reseeding.  This avoids a gap that could exceed the prompt-
        # cache TTL.
        if not self._enqueue_startup and system_pending:
            now = tz_now()
            has_future_heartbeat = any(
                msg.metadata.get("recurring")
                and msg.not_before
                and msg.not_before > now
                and msg.metadata.get("recur_spec") == self.interval
                for _, msg in system_pending
            )
            if has_future_heartbeat:
                logger.info(
                    "Preserved %d pending system message(s) from previous run",
                    len(system_pending),
                )
                if self._upgrade_message and self._enqueue_upgrade_notice:
                    upgrade_at = self._apply_quiet_hours(now)
                    agent.enqueue(
                        make_upgrade_notice_message(
                            content=self._upgrade_message,
                            not_before=upgrade_at if upgrade_at > now else None,
                        )
                    )
                    logger.info(
                        "Upgrade notice enqueued alongside preserved heartbeat"
                    )
                return

        # Clear stale system messages from previous run.
        cleared = 0
        for filepath, _ in system_pending:
            q.remove_pending(filepath)
            cleared += 1
        if cleared:
            logger.info("Cleared %d old system heartbeat(s)", cleared)

        if not self._enqueue_startup:
            if self._upgrade_message and self._enqueue_upgrade_notice:
                now = tz_now()
                upgrade_at = self._apply_quiet_hours(now)
                agent.enqueue(
                    make_upgrade_notice_message(
                        content=self._upgrade_message,
                        not_before=upgrade_at if upgrade_at > now else None,
                    )
                )
                if upgrade_at > now:
                    logger.info(
                        "Upgrade notice deferred to %s",
                        upgrade_at.isoformat(),
                    )
                else:
                    logger.info("Upgrade notice enqueued")
            delay = random_delay(self.interval)
            next_time = self._apply_quiet_hours(tz_now() + delay)
            delayed_msg = make_heartbeat_message(
                not_before=next_time,
                interval_spec=self.interval,
            )
            agent.enqueue(delayed_msg)
            logger.info("Startup heartbeat disabled; seeded delayed heartbeat")
            return

        # Enqueue startup heartbeat (with upgrade info if available).
        # If startup lands in quiet hours, defer it to quiet-end boundary.
        if self._upgrade_message and self._enqueue_upgrade_notice:
            content = self._upgrade_message
        else:
            content = _STARTUP_CONTENT

        now = tz_now()
        startup_at = self._apply_quiet_hours(now)
        startup_msg = InboundMessage(
            channel="system",
            content=content,
            priority=5,
            sender="system",
            metadata={
                "system": True,
                "recurring": True,
                "recur_spec": self.interval,
            },
            not_before=startup_at if startup_at > now else None,
        )
        agent.enqueue(startup_msg)
        if startup_at > now:
            logger.info("Startup heartbeat deferred to %s", startup_at.isoformat())
        else:
            logger.info("Startup heartbeat enqueued")

    def _apply_quiet_hours(self, dt):
        return apply_quiet_hours(dt, self._quiet_windows)

    def send(self, message: OutboundMessage) -> None:
        """No-op: system channel does not send outbound messages."""

    def on_turn_start(self, channel: str) -> None:
        pass

    def on_turn_complete(self) -> None:
        pass

    def stop(self) -> None:
        pass
