"""Tests for SchedulerAdapter."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lincy.agent.adapters.scheduler import (
    SchedulerAdapter,
    make_heartbeat_message,
    make_upgrade_notice_message,
    random_delay,
)
from lincy.agent.heartbeat import parse_interval
from lincy.agent.schema import InboundMessage


# ------------------------------------------------------------------
# Interval parsing
# ------------------------------------------------------------------


class TestParseInterval:
    # Returns (lo_minutes, hi_minutes)

    def test_hours(self):
        assert parse_interval("2h-5h") == (120, 300)

    def test_single_hour(self):
        assert parse_interval("1h-1h") == (60, 60)

    def test_minutes(self):
        assert parse_interval("30m-90m") == (30, 90)

    def test_mixed_units(self):
        assert parse_interval("1h-30m") == (30, 60)

    def test_swapped_order(self):
        assert parse_interval("5h-2h") == (120, 300)

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid interval"):
            parse_interval("bad")

    def test_invalid_no_unit(self):
        with pytest.raises(ValueError):
            parse_interval("2-5")

    def test_invalid_empty(self):
        with pytest.raises(ValueError):
            parse_interval("")


class TestRandomDelay:
    def test_hours_within_range(self):
        for _ in range(20):
            d = random_delay("2h-5h")
            assert timedelta(hours=2) <= d <= timedelta(hours=5)

    def test_minutes_within_range(self):
        for _ in range(20):
            d = random_delay("30m-90m")
            assert timedelta(minutes=30) <= d <= timedelta(minutes=90)

    def test_same_bounds(self):
        d = random_delay("3h-3h")
        assert d == timedelta(hours=3)


# ------------------------------------------------------------------
# Heartbeat message creation
# ------------------------------------------------------------------


class TestMakeHeartbeatMessage:
    def test_startup_content(self):
        msg = make_heartbeat_message(is_startup=True)
        assert "[STARTUP]" in msg.content
        assert msg.channel == "system"
        assert msg.priority == 5
        assert msg.sender == "system"
        assert msg.not_before is None

    def test_startup_metadata(self):
        msg = make_heartbeat_message(is_startup=True, interval_spec="3h-6h")
        assert msg.metadata["system"] is True
        assert msg.metadata["recurring"] is True
        assert msg.metadata["recur_spec"] == "3h-6h"

    def test_regular_heartbeat_content(self):
        from datetime import datetime, timezone

        nb = datetime(2026, 3, 1, 14, 37, tzinfo=timezone.utc)
        msg = make_heartbeat_message(not_before=nb)
        assert "[HEARTBEAT]" in msg.content
        assert "2026-03-01 22:37" in msg.content
        assert msg.not_before == nb

    def test_regular_heartbeat_content_uses_app_timezone(self):
        from datetime import datetime, timezone

        nb = datetime(2026, 3, 1, 14, 37, tzinfo=timezone.utc)
        msg = make_heartbeat_message(not_before=nb)
        # App timezone is UTC+8, so 14:37 UTC -> 22:37 UTC+8
        assert "2026-03-01 22:37" in msg.content

    def test_regular_heartbeat_metadata(self):
        msg = make_heartbeat_message(interval_spec="1h-2h")
        assert msg.metadata["recurring"] is True
        assert msg.metadata["recur_spec"] == "1h-2h"


class TestMakeUpgradeNoticeMessage:
    def test_upgrade_notice_metadata(self):
        msg = make_upgrade_notice_message(content="[STARTUP after upgrade]\nversion: 0.1.0 -> 0.2.0")
        assert msg.channel == "system"
        assert msg.priority == 5
        assert msg.sender == "system"
        assert msg.metadata["system"] is True
        assert msg.metadata["upgrade_notice"] is True
        assert "recurring" not in msg.metadata


# ------------------------------------------------------------------
# Adapter start
# ------------------------------------------------------------------


class TestSchedulerAdapterStart:
    def _make_agent(self, pending_items=None):
        agent = MagicMock()
        agent._queue = MagicMock()
        agent._queue.scan_pending.return_value = pending_items or []
        return agent

    def test_clears_old_system_heartbeats(self):
        old_hb = make_heartbeat_message()
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), old_hb),
            ]
        )
        adapter = SchedulerAdapter(interval="2h-5h", enqueue_startup=True)
        adapter.start(agent)

        agent._queue.remove_pending.assert_called_once_with(
            Path("/fake/pending/0005_00000001.json")
        )

    def test_preserves_non_system_messages(self):
        non_system = InboundMessage(
            channel="system",
            content="[SCHEDULED]",
            priority=0,
            sender="system",
            metadata={},  # No "system" key
        )
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0000_00000001.json"), non_system),
            ]
        )
        adapter = SchedulerAdapter(interval="2h-5h", enqueue_startup=True)
        adapter.start(agent)

        agent._queue.remove_pending.assert_not_called()

    def test_enqueues_startup_heartbeat(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(interval="3h-6h", enqueue_startup=True)
        adapter.start(agent)

        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert isinstance(enqueued, InboundMessage)
        assert "[STARTUP]" in enqueued.content
        assert enqueued.metadata["recur_spec"] == "3h-6h"
        assert enqueued.not_before is None

    def test_no_queue_no_crash(self):
        agent = MagicMock()
        agent._queue = None
        adapter = SchedulerAdapter()
        adapter.start(agent)  # Should not raise

    def test_default_seeds_delayed_heartbeat(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(interval="2h-5h")
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert isinstance(enqueued, InboundMessage)
        assert "[HEARTBEAT]" in enqueued.content
        assert "[STARTUP]" not in enqueued.content
        assert enqueued.not_before is not None
        assert enqueued.metadata["system"] is True
        assert enqueued.metadata["recurring"] is True

    def test_upgrade_notice_enqueued_even_when_startup_disabled(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(
            interval="2h-5h",
            enqueue_startup=False,
            enqueue_upgrade_notice=True,
            upgrade_message="[STARTUP after upgrade]\nversion: 0.63.8 -> 0.63.9",
        )
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        assert agent.enqueue.call_count == 2
        upgrade_msg = agent.enqueue.call_args_list[0].args[0]
        heartbeat_msg = agent.enqueue.call_args_list[1].args[0]

        assert isinstance(upgrade_msg, InboundMessage)
        assert upgrade_msg.metadata["upgrade_notice"] is True
        assert "[STARTUP after upgrade]" in upgrade_msg.content
        assert "recurring" not in upgrade_msg.metadata

        assert isinstance(heartbeat_msg, InboundMessage)
        assert "[HEARTBEAT]" in heartbeat_msg.content
        assert heartbeat_msg.metadata["recurring"] is True

    def test_upgrade_notice_can_be_disabled(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(
            interval="2h-5h",
            enqueue_startup=False,
            enqueue_upgrade_notice=False,
            upgrade_message="[STARTUP after upgrade]\nversion: 0.63.8 -> 0.63.9",
        )
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        agent.enqueue.assert_called_once()
        heartbeat_msg = agent.enqueue.call_args.args[0]
        assert isinstance(heartbeat_msg, InboundMessage)
        assert "[HEARTBEAT]" in heartbeat_msg.content
        assert "upgrade_notice" not in heartbeat_msg.metadata

    def test_upgrade_message_replaces_startup_content_when_startup_enabled(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(
            interval="3h-6h",
            enqueue_startup=True,
            enqueue_upgrade_notice=True,
            upgrade_message="[STARTUP after upgrade]\nversion: 0.63.8 -> 0.63.9",
        )
        adapter.start(agent)

        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args.args[0]
        assert isinstance(enqueued, InboundMessage)
        assert "[STARTUP after upgrade]" in enqueued.content
        assert enqueued.metadata["recurring"] is True
        assert "upgrade_notice" not in enqueued.metadata

    def test_startup_ignores_upgrade_message_when_upgrade_notice_disabled(self):
        agent = self._make_agent()
        adapter = SchedulerAdapter(
            interval="3h-6h",
            enqueue_startup=True,
            enqueue_upgrade_notice=False,
            upgrade_message="[STARTUP after upgrade]\nversion: 0.63.8 -> 0.63.9",
        )
        adapter.start(agent)

        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args.args[0]
        assert isinstance(enqueued, InboundMessage)
        assert "[STARTUP]" in enqueued.content
        assert "[STARTUP after upgrade]" not in enqueued.content
        assert enqueued.metadata["recurring"] is True


# ------------------------------------------------------------------
# Heartbeat preservation across restart
# ------------------------------------------------------------------


class TestHeartbeatPreservation:
    def _make_agent(self, pending_items=None):
        agent = MagicMock()
        agent._queue = MagicMock()
        agent._queue.scan_pending.return_value = pending_items or []
        return agent

    def test_preserves_future_heartbeat(self):
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        hb = make_heartbeat_message(not_before=future, interval_spec="40m-55m")
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        adapter = SchedulerAdapter(interval="40m-55m", enqueue_startup=False)
        adapter.start(agent)

        agent._queue.remove_pending.assert_not_called()
        agent.enqueue.assert_not_called()

    def test_preserves_future_heartbeat_with_upgrade_notice(self):
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        hb = make_heartbeat_message(not_before=future, interval_spec="40m-55m")
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        adapter = SchedulerAdapter(
            interval="40m-55m",
            enqueue_startup=False,
            enqueue_upgrade_notice=True,
            upgrade_message="[STARTUP after upgrade]\nv1 -> v2",
        )
        adapter.start(agent)

        agent._queue.remove_pending.assert_not_called()
        # Only upgrade notice enqueued, no new heartbeat
        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert enqueued.metadata.get("upgrade_notice") is True

    def test_clears_past_heartbeat_and_reseeds(self):
        from datetime import datetime, timezone

        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        hb = make_heartbeat_message(not_before=past, interval_spec="40m-55m")
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        adapter = SchedulerAdapter(interval="40m-55m", enqueue_startup=False)
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        agent._queue.remove_pending.assert_called_once()
        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert enqueued.metadata.get("recurring") is True

    def test_clears_none_not_before_and_reseeds(self):
        hb = make_heartbeat_message()  # not_before=None
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        adapter = SchedulerAdapter(interval="40m-55m", enqueue_startup=False)
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        agent._queue.remove_pending.assert_called_once()
        agent.enqueue.assert_called_once()

    def test_clears_when_interval_changed(self):
        """Future heartbeat with different recur_spec is cleared and reseeded."""
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        hb = make_heartbeat_message(not_before=future, interval_spec="2h-5h")
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        # Config now uses a different interval
        adapter = SchedulerAdapter(interval="40m-55m", enqueue_startup=False)
        with patch("lincy.agent.adapters.scheduler.random_delay", return_value=timedelta(minutes=10)):
            adapter.start(agent)

        agent._queue.remove_pending.assert_called_once()
        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert enqueued.metadata["recur_spec"] == "40m-55m"

    def test_enqueue_startup_always_clears(self):
        """enqueue_startup=True always clears, even with future heartbeat."""
        from datetime import datetime, timezone

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        hb = make_heartbeat_message(not_before=future, interval_spec="40m-55m")
        agent = self._make_agent(
            pending_items=[
                (Path("/fake/pending/0005_00000001.json"), hb),
            ]
        )
        adapter = SchedulerAdapter(interval="40m-55m", enqueue_startup=True)
        adapter.start(agent)

        agent._queue.remove_pending.assert_called_once()
        agent.enqueue.assert_called_once()
        enqueued = agent.enqueue.call_args[0][0]
        assert "[STARTUP]" in enqueued.content


# ------------------------------------------------------------------
# Protocol methods
# ------------------------------------------------------------------


class TestSchedulerAdapterProtocol:
    def test_channel_name(self):
        assert SchedulerAdapter().channel_name == "system"

    def test_priority(self):
        assert SchedulerAdapter().priority == 5

    def test_send_is_noop(self):
        adapter = SchedulerAdapter()
        adapter.send(MagicMock())  # No-op, no error

    def test_turn_callbacks_are_noop(self):
        adapter = SchedulerAdapter()
        adapter.on_turn_start("cli")
        adapter.on_turn_complete()
        adapter.stop()
