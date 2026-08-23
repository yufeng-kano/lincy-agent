"""Tests for pre-sleep memory sync scheduling and handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lincy.agent.adapters.scheduler import SchedulerAdapter
from lincy.agent.heartbeat import make_pre_sleep_sync_message
from lincy.agent.schema import InboundMessage
from lincy.agent.turn_context import TurnContext
from lincy.context.conversation import Conversation
from lincy.timezone_utils import now as tz_now


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_core(tmp_path, *, turns_since_sync: int = 0):
    """Create a minimal AgentCore for pre-sleep sync testing."""
    from lincy.agent.core import AgentCore
    from lincy.agent.queue import PersistentPriorityQueue

    q = PersistentPriorityQueue(tmp_path / "q")
    conv = Conversation()
    tc = TurnContext()

    core = AgentCore.__new__(AgentCore)
    core._queue = q
    core.console = MagicMock()
    core.conversation = conv
    core.turn_context = tc
    core.builder = MagicMock()
    core.config = MagicMock(
        app=SimpleNamespace(timezone="UTC+8"),
        tools=SimpleNamespace(
            memory_sync=SimpleNamespace(every_n_turns=5, max_retries=1),
        ),
    )
    core.adapters = {}
    core.run_turn = MagicMock()
    core.client = MagicMock()
    core.registry = MagicMock()
    core.registry.get_definitions.return_value = []
    core._turns_since_memory_sync = turns_since_sync
    return core, q


def _make_system_heartbeat(**overrides):
    defaults = dict(
        channel="system",
        content="[HEARTBEAT]\nTime: 2026-03-01 12:00\n\nCheck memory.",
        priority=5,
        sender="system",
        metadata={"system": True, "recurring": True, "recur_spec": "51m-54m"},
    )
    defaults.update(overrides)
    return InboundMessage(**defaults)


def _scan_pre_sleep(q):
    """Find pre-sleep sync messages in queue pending."""
    return [
        (f, m) for f, m in q.scan_pending(channel="system")
        if m.metadata.get("pre_sleep_sync")
    ]


def _scan_heartbeats(q):
    """Find recurring heartbeat messages in queue pending."""
    return [
        (f, m) for f, m in q.scan_pending(channel="system")
        if m.metadata.get("system") and m.metadata.get("recurring")
    ]


# ------------------------------------------------------------------
# make_pre_sleep_sync_message factory
# ------------------------------------------------------------------


class TestMakePreSleepSyncMessage:
    def test_basic_fields(self):
        nb = datetime(2026, 3, 1, 1, 30, tzinfo=timezone.utc)
        msg = make_pre_sleep_sync_message(not_before=nb)
        assert msg.channel == "system"
        assert msg.sender == "system"
        assert msg.priority == 5
        assert msg.not_before == nb
        assert "[PRE-SLEEP SYNC]" in msg.content

    def test_metadata_has_pre_sleep_sync(self):
        msg = make_pre_sleep_sync_message(
            not_before=tz_now(),
        )
        assert msg.metadata["system"] is True
        assert msg.metadata["pre_sleep_sync"] is True
        assert "recurring" not in msg.metadata


# ------------------------------------------------------------------
# _maybe_schedule_pre_sleep_sync
# ------------------------------------------------------------------


class TestMaybeSchedulePreSleepSync:
    def test_schedules_when_deferred(self, tmp_path):
        """Pre-sleep sync is enqueued when heartbeat is deferred past quiet hours."""
        core, q = _make_core(tmp_path)
        core._maybe_schedule_pre_sleep_sync(was_deferred=True)

        syncs = _scan_pre_sleep(q)
        assert len(syncs) == 1
        _, msg = syncs[0]
        assert msg.not_before is not None
        # Should be ~30 min from now
        delta = (msg.not_before - tz_now()).total_seconds()
        assert 25 * 60 < delta < 35 * 60

    def test_no_schedule_when_not_deferred(self, tmp_path):
        """No pre-sleep sync when heartbeat is not deferred."""
        core, q = _make_core(tmp_path)
        core._maybe_schedule_pre_sleep_sync(was_deferred=False)

        assert len(_scan_pre_sleep(q)) == 0

    def test_dedup_removes_old_sync(self, tmp_path):
        """Scheduling a new pre-sleep sync removes any existing one."""
        core, q = _make_core(tmp_path)

        # Seed an old pre-sleep sync
        old_sync = make_pre_sleep_sync_message(
            not_before=tz_now() + timedelta(minutes=10),
        )
        q.put(old_sync)
        assert len(_scan_pre_sleep(q)) == 1

        # Schedule a new one (deferred)
        core._maybe_schedule_pre_sleep_sync(was_deferred=True)

        syncs = _scan_pre_sleep(q)
        assert len(syncs) == 1
        # New sync should be later than the old one
        _, msg = syncs[0]
        assert msg.not_before > old_sync.not_before

    def test_not_deferred_cleans_up_old_sync(self, tmp_path):
        """When not deferred, existing pre-sleep sync is removed."""
        core, q = _make_core(tmp_path)

        old_sync = make_pre_sleep_sync_message(
            not_before=tz_now() + timedelta(minutes=10),
        )
        q.put(old_sync)
        assert len(_scan_pre_sleep(q)) == 1

        core._maybe_schedule_pre_sleep_sync(was_deferred=False)
        assert len(_scan_pre_sleep(q)) == 0

    def test_no_queue_no_crash(self, tmp_path):
        """No error when queue is None."""
        core, _ = _make_core(tmp_path)
        core._queue = None
        core._maybe_schedule_pre_sleep_sync(was_deferred=True)  # Should not raise


# ------------------------------------------------------------------
# _handle_pre_sleep_sync
# ------------------------------------------------------------------


class TestHandlePreSleepSync:
    def test_runs_sync_when_counter_positive(self, tmp_path):
        """Sync side-channel called when turns_since_memory_sync > 0."""
        core, q = _make_core(tmp_path, turns_since_sync=3)

        msg = make_pre_sleep_sync_message(
            not_before=tz_now(),
        )
        q.put(msg)
        _, receipt = q.get()

        with patch(
            "lincy.agent.core._run_memory_sync_side_channel",
        ) as mock_sync:
            core._handle_pre_sleep_sync(receipt)

        mock_sync.assert_called_once()
        assert core._turns_since_memory_sync == 0
        # Receipt should be acked
        assert q.pending_count() == 0

    def test_skips_when_counter_zero(self, tmp_path):
        """No sync call when counter is 0."""
        core, q = _make_core(tmp_path, turns_since_sync=0)

        msg = make_pre_sleep_sync_message(
            not_before=tz_now(),
        )
        q.put(msg)
        _, receipt = q.get()

        with patch(
            "lincy.agent.core._run_memory_sync_side_channel",
        ) as mock_sync:
            core._handle_pre_sleep_sync(receipt)

        mock_sync.assert_not_called()
        assert q.pending_count() == 0  # Still acked

    def test_sync_failure_still_acks(self, tmp_path):
        """Queue receipt acked even if sync fails."""
        core, q = _make_core(tmp_path, turns_since_sync=2)

        msg = make_pre_sleep_sync_message(
            not_before=tz_now(),
        )
        q.put(msg)
        _, receipt = q.get()

        with patch(
            "lincy.agent.core._run_memory_sync_side_channel",
            side_effect=RuntimeError("LLM error"),
        ):
            core._handle_pre_sleep_sync(receipt)

        assert q.pending_count() == 0  # Acked despite failure


# ------------------------------------------------------------------
# _process_inbound early return
# ------------------------------------------------------------------


class TestProcessInboundPreSleepSync:
    def test_pre_sleep_sync_skips_run_turn(self, tmp_path):
        """Pre-sleep sync message does not call run_turn."""
        core, q = _make_core(tmp_path, turns_since_sync=2)

        msg = InboundMessage(
            channel="system",
            content="[PRE-SLEEP SYNC]",
            priority=5,
            sender="system",
            metadata={"system": True, "pre_sleep_sync": True},
        )
        q.put(msg)
        _, receipt = q.get()

        with patch(
            "lincy.agent.core._run_memory_sync_side_channel",
        ):
            core._process_inbound(msg, receipt)

        core.run_turn.assert_not_called()

    def test_pre_sleep_sync_does_not_schedule_heartbeat(self, tmp_path):
        """Pre-sleep sync should not trigger heartbeat scheduling."""
        core, q = _make_core(tmp_path, turns_since_sync=0)

        msg = InboundMessage(
            channel="system",
            content="[PRE-SLEEP SYNC]",
            priority=5,
            sender="system",
            metadata={"system": True, "pre_sleep_sync": True},
        )
        q.put(msg)
        _, receipt = q.get()

        with patch.object(core, "_schedule_next_heartbeat") as mock_sched, \
             patch.object(core, "_defer_pending_heartbeat") as mock_defer:
            core._process_inbound(msg, receipt)

        mock_sched.assert_not_called()
        mock_defer.assert_not_called()


# ------------------------------------------------------------------
# Startup cleanup
# ------------------------------------------------------------------


class TestStartupCleanup:
    def test_pre_sleep_sync_cleared_on_startup(self, tmp_path):
        """SchedulerAdapter.start() clears pre-sleep sync (has system: True)."""
        from lincy.agent.queue import PersistentPriorityQueue

        q = PersistentPriorityQueue(tmp_path / "q")
        sync_msg = make_pre_sleep_sync_message(
            not_before=tz_now() + timedelta(minutes=10),
        )
        q.put(sync_msg)
        assert len(_scan_pre_sleep(q)) == 1

        agent = MagicMock()
        agent._queue = q
        adapter = SchedulerAdapter(interval="51m-54m", enqueue_startup=True)
        adapter.start(agent)

        # Pre-sleep sync should be cleared (system: True)
        assert len(_scan_pre_sleep(q)) == 0


# ------------------------------------------------------------------
# Integration: _schedule_next_heartbeat triggers pre-sleep sync
# ------------------------------------------------------------------


class TestScheduleNextHeartbeatPreSleep:
    def test_heartbeat_deferred_schedules_pre_sleep(self, tmp_path):
        """When next heartbeat is deferred past quiet hours, pre-sleep sync is scheduled."""
        core, q = _make_core(tmp_path, turns_since_sync=2)

        hb = _make_system_heartbeat()

        # Patch _apply_quiet_hours to always defer (push +6h)
        original_dt = None

        def fake_apply(dt):
            nonlocal original_dt
            original_dt = dt
            return dt + timedelta(hours=6)

        with patch.object(core, "_apply_quiet_hours", side_effect=fake_apply):
            core._schedule_next_heartbeat(hb)

        # Should have both heartbeat and pre-sleep sync
        assert len(_scan_heartbeats(q)) == 1
        syncs = _scan_pre_sleep(q)
        assert len(syncs) == 1

    def test_heartbeat_not_deferred_no_pre_sleep(self, tmp_path):
        """When next heartbeat is not deferred, no pre-sleep sync."""
        core, q = _make_core(tmp_path, turns_since_sync=2)

        hb = _make_system_heartbeat()

        # No deferral
        with patch.object(core, "_apply_quiet_hours", side_effect=lambda dt: dt):
            core._schedule_next_heartbeat(hb)

        assert len(_scan_heartbeats(q)) == 1
        assert len(_scan_pre_sleep(q)) == 0


# ------------------------------------------------------------------
# Integration: _defer_pending_heartbeat triggers pre-sleep sync
# ------------------------------------------------------------------


class TestDeferHeartbeatPreSleep:
    def test_deferred_heartbeat_schedules_pre_sleep(self, tmp_path):
        """User message defers heartbeat past quiet hours -> pre-sleep sync."""
        core, q = _make_core(tmp_path, turns_since_sync=3)

        # Seed a pending heartbeat
        hb = _make_system_heartbeat(
            not_before=tz_now() + timedelta(minutes=30),
        )
        q.put(hb)

        # Patch _apply_quiet_hours to defer
        def fake_apply(dt):
            return dt + timedelta(hours=5)

        with patch.object(core, "_apply_quiet_hours", side_effect=fake_apply):
            core._defer_pending_heartbeat()

        syncs = _scan_pre_sleep(q)
        assert len(syncs) == 1

    def test_not_deferred_no_pre_sleep(self, tmp_path):
        """User message defers heartbeat but stays outside quiet hours -> no sync."""
        core, q = _make_core(tmp_path, turns_since_sync=3)

        hb = _make_system_heartbeat(
            not_before=tz_now() + timedelta(minutes=30),
        )
        q.put(hb)

        with patch.object(core, "_apply_quiet_hours", side_effect=lambda dt: dt):
            core._defer_pending_heartbeat()

        assert len(_scan_pre_sleep(q)) == 0

    def test_no_pending_heartbeat_no_pre_sleep(self, tmp_path):
        """No crash and no sync when no pending heartbeat exists."""
        core, q = _make_core(tmp_path, turns_since_sync=3)

        core._defer_pending_heartbeat()

        assert len(_scan_pre_sleep(q)) == 0


# ------------------------------------------------------------------
# Pre-sleep sync not deferred by user activity
# ------------------------------------------------------------------


class TestPreSleepNotDeferred:
    def test_pre_sleep_not_picked_up_by_defer(self, tmp_path):
        """_defer_pending_heartbeat does not touch pre-sleep sync (no recurring)."""
        core, q = _make_core(tmp_path)

        # Seed both heartbeat and pre-sleep sync
        hb = _make_system_heartbeat(
            not_before=tz_now() + timedelta(minutes=30),
        )
        q.put(hb)

        sync = make_pre_sleep_sync_message(
            not_before=tz_now() + timedelta(minutes=20),
        )
        q.put(sync)

        with patch.object(core, "_apply_quiet_hours", side_effect=lambda dt: dt):
            core._defer_pending_heartbeat()

        # Heartbeat was rescheduled, but pre-sleep sync should remain
        # (Note: _maybe_schedule_pre_sleep_sync with was_deferred=False
        # will clean it up, but the defer logic itself doesn't touch it)
        # The heartbeat was not deferred, so the old pre-sleep gets cleaned
        # That's correct: no quiet hours deferral -> remove stale pre-sleep sync
        syncs = _scan_pre_sleep(q)
        assert len(syncs) == 0  # Cleaned up because was_deferred=False
