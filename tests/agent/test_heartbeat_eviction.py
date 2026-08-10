"""Tests for system turn eviction from in-memory conversation."""

from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lincy.agent.schema import InboundMessage
from lincy.agent.turn_context import TurnContext
from lincy.context.conversation import Conversation
from lincy.core.schema import HeartbeatConfig
from lincy.llm.schema import ToolCall
from lincy.timezone_utils import get_tz, now as tz_now


def _make_system_heartbeat(**overrides):
    """Create a system heartbeat InboundMessage."""
    defaults = dict(
        channel="system",
        content="[HEARTBEAT]\nTime: 2026-02-21 12:00\n\nCheck memory.",
        priority=5,
        sender="system",
        metadata={"system": True, "recurring": True, "recur_spec": "3m-5m"},
    )
    defaults.update(overrides)
    return InboundMessage(**defaults)


def _make_scheduled_message(**overrides):
    """Create a scheduled system InboundMessage."""
    defaults = dict(
        channel="system",
        content=(
            "[SCHEDULED]\n"
            "Reason: follow up\n"
            "Scheduled at: 2026-02-23 21:20\n\n"
            "Act on this reason. Use send_message to deliver messages."
        ),
        priority=0,
        sender="system",
        metadata={"scheduled_reason": "follow up"},
    )
    defaults.update(overrides)
    return InboundMessage(**defaults)


def _make_core(tmp_path, *, turn_context=None):
    """Create a minimal AgentCore for _process_inbound testing."""
    from lincy.agent.core import AgentCore
    from lincy.agent.queue import PersistentPriorityQueue

    q = PersistentPriorityQueue(tmp_path / "q")
    conv = Conversation()
    tc = turn_context if turn_context is not None else TurnContext()

    core = AgentCore.__new__(AgentCore)
    core._queue = q
    core.console = MagicMock()
    core.conversation = conv
    core.turn_context = tc
    core.builder = MagicMock()
    core.config = MagicMock(app=SimpleNamespace(timezone="UTC+8"))
    core.adapters = {}
    core.copilot_runtime = None
    core.run_turn = MagicMock(return_value="completed")
    return core, q, conv, tc


def _add_tool_round(
    conv: Conversation,
    *,
    tool_calls: list[ToolCall],
    results: dict[str, str],
    content: str | None = None,
) -> None:
    """Append assistant tool calls and matching tool results to conversation."""
    conv.add_assistant_with_tools(content, tool_calls)
    for tc in tool_calls:
        conv.add_tool_result(tc.id, tc.name, results[tc.id])


class TestSilentHeartbeatEviction:
    """Silent system heartbeats should be evicted from in-memory conversation."""

    def test_silent_heartbeat_evicted(self, tmp_path):
        """A system heartbeat that sends nothing is removed from conversation."""
        core, q, conv, tc = _make_core(tmp_path)

        # Simulate existing user conversation
        conv.add("user", "hello", channel="cli", sender="alice")
        conv.add("assistant", "hi there")
        pre_count = len(conv.get_messages())  # 2

        # run_turn adds messages during the heartbeat turn
        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            conv.add("assistant", "nothing to do")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        # Heartbeat turn should be evicted; only original messages remain
        assert len(conv.get_messages()) == pre_count

    def test_active_heartbeat_preserved(self, tmp_path):
        """A system heartbeat that calls send_message is kept in conversation."""
        core, q, conv, tc = _make_core(tmp_path)

        conv.add("user", "hello", channel="cli", sender="alice")
        pre_count = len(conv.get_messages())

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            conv.add("assistant", "sending reminder")
            # Simulate send_message tool populating sent_hashes
            tc.sent_hashes.add("fake_sent_hash")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        # Turn should be preserved (sent_hashes is non-empty)
        assert len(conv.get_messages()) > pre_count

    def test_non_system_message_never_evicted(self, tmp_path):
        """Regular user messages are never evicted even if sent_hashes is empty."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="cli", sender="alice")
            conv.add("assistant", "ok")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = InboundMessage(
            channel="cli", content="hi", priority=0, sender="alice",
        )
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        # Should have the messages from the turn
        assert len(conv.get_messages()) == 2

    def test_failed_turn_not_evicted(self, tmp_path):
        """If run_turn raises, no eviction happens (completed=False)."""
        core, q, conv, tc = _make_core(tmp_path)

        conv.add("user", "hello", channel="cli", sender="alice")
        pre_count = len(conv.get_messages())

        core.run_turn.side_effect = RuntimeError("LLM error")

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()

        with pytest.raises(RuntimeError):
            core._process_inbound(msg, receipt)

        # No eviction; conversation unchanged
        assert len(conv.get_messages()) == pre_count

    def test_failed_recurring_heartbeat_reseeds_when_retry_not_enqueued(self, tmp_path):
        """Terminal recurring heartbeat failures should keep the chain alive."""
        core, q, conv, tc = _make_core(tmp_path)

        core.run_turn.return_value = "failed"

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()

        with (
            patch.object(core, "_requeue_failed_inbound", return_value=False),
            patch.object(core, "_schedule_next_heartbeat") as mock_schedule,
        ):
            core._process_inbound(msg, receipt)

        mock_schedule.assert_called_once_with(msg)
        assert q.pending_count() == 0

    def test_failed_recurring_heartbeat_retry_does_not_duplicate_chain(self, tmp_path):
        """Retry-enqueued recurring heartbeat should not also seed a new heartbeat."""
        core, q, conv, tc = _make_core(tmp_path)

        core.run_turn.return_value = "failed"

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()

        with (
            patch.object(core, "_requeue_failed_inbound", return_value=True),
            patch.object(core, "_schedule_next_heartbeat") as mock_schedule,
        ):
            core._process_inbound(msg, receipt)

        mock_schedule.assert_not_called()

    def test_eviction_does_not_affect_queue_ack(self, tmp_path):
        """Queue ack and next heartbeat scheduling still happen after eviction."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()

        with patch.object(core, "_schedule_next_heartbeat") as mock_schedule:
            core._process_inbound(msg, receipt)

            # Turn was evicted
            assert len(conv.get_messages()) == 0
            # But next heartbeat was still scheduled
            mock_schedule.assert_called_once_with(msg)

    def test_no_turn_context_skips_eviction(self, tmp_path):
        """If turn_context is None, eviction is skipped (safety)."""
        core, q, conv, _ = _make_core(tmp_path)
        core.turn_context = None

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        # No eviction because turn_context is None
        assert len(conv.get_messages()) == 1


class TestHeartbeatReliabilityNotice:
    """Recurring heartbeat turns should carry reliability guidance."""

    def test_recurring_heartbeat_gets_reliability_notice(self, tmp_path):
        """Recurring heartbeat content includes the reliability notice."""
        core, q, _conv, _tc = _make_core(tmp_path)
        captured: dict[str, str] = {}

        def fake_turn(content, **kwargs):
            captured["content"] = content
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_system_heartbeat()
        q.put(msg)
        _, receipt = q.get()

        with patch.object(core, "_schedule_next_heartbeat"):
            core._process_inbound(msg, receipt)

        assert "[Heartbeat Reliability Notice]" in captured["content"]
        assert "future heartbeat will not wake you up" in captured["content"]

    def test_non_heartbeat_does_not_get_reliability_notice(self, tmp_path):
        """Regular user turns should not receive heartbeat-only guidance."""
        core, q, _conv, _tc = _make_core(tmp_path)
        captured: dict[str, str] = {}

        def fake_turn(content, **kwargs):
            captured["content"] = content
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = InboundMessage(
            channel="cli",
            content="hi",
            priority=0,
            sender="alice",
        )
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert "[Heartbeat Reliability Notice]" not in captured["content"]

    def test_quiet_hours_warning_when_next_heartbeat_would_be_deferred(
        self, tmp_path
    ):
        """Quiet-hours warning appears when the earliest next heartbeat is blocked."""
        core, q, _conv, _tc = _make_core(tmp_path)
        core.config = SimpleNamespace(
            app=SimpleNamespace(timezone="UTC+8"),
            heartbeat=HeartbeatConfig(quiet_hours=["23:30-07:30"]),
        )
        captured: dict[str, str] = {}

        def fake_turn(content, **kwargs):
            captured["content"] = content
            return "completed"

        core.run_turn.side_effect = fake_turn
        fixed_now = datetime(2026, 2, 21, 23, 0, tzinfo=get_tz())
        msg = _make_system_heartbeat(
            metadata={
                "system": True,
                "recurring": True,
                "recur_spec": "30m-60m",
            }
        )
        q.put(msg)
        _, receipt = q.get()

        with (
            patch("lincy.agent.core.tz_now", return_value=fixed_now),
            patch.object(core, "_schedule_next_heartbeat"),
        ):
            core._process_inbound(msg, receipt)

        assert "[Heartbeat Reliability Notice]" in captured["content"]
        assert "[Heartbeat Quiet-Hours Warning]" in captured["content"]
        assert "last heartbeat before quiet hours" in captured["content"]

    def test_quiet_hours_warning_omitted_when_next_heartbeat_is_clear(
        self, tmp_path
    ):
        """Quiet-hours warning is omitted while the earliest next heartbeat is clear."""
        core, q, _conv, _tc = _make_core(tmp_path)
        core.config = SimpleNamespace(
            app=SimpleNamespace(timezone="UTC+8"),
            heartbeat=HeartbeatConfig(quiet_hours=["23:30-07:30"]),
        )
        captured: dict[str, str] = {}

        def fake_turn(content, **kwargs):
            captured["content"] = content
            return "completed"

        core.run_turn.side_effect = fake_turn
        fixed_now = datetime(2026, 2, 21, 20, 0, tzinfo=get_tz())
        msg = _make_system_heartbeat(
            metadata={
                "system": True,
                "recurring": True,
                "recur_spec": "30m-60m",
            }
        )
        q.put(msg)
        _, receipt = q.get()

        with (
            patch("lincy.agent.core.tz_now", return_value=fixed_now),
            patch.object(core, "_schedule_next_heartbeat"),
        ):
            core._process_inbound(msg, receipt)

        assert "[Heartbeat Reliability Notice]" in captured["content"]
        assert "[Heartbeat Quiet-Hours Warning]" not in captured["content"]


class TestScheduledNoopEviction:
    """Scheduled system turns should evict only when truly no-op."""

    def test_scheduled_noop_evicted(self, tmp_path):
        """Scheduled turn with no send/tool side effects should be evicted."""
        core, q, conv, tc = _make_core(tmp_path)

        conv.add("user", "hello", channel="cli", sender="alice")
        pre_count = len(conv.get_messages())

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            conv.add("assistant", "Checked. Nothing to do.")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == pre_count

    def test_scheduled_list_only_evicted(self, tmp_path):
        """schedule_action list alone is informational and should be evicted."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_list = ToolCall(id="tc_list", name="schedule_action", arguments={"action": "list"})
            _add_tool_round(
                conv,
                tool_calls=[tc_list],
                results={"tc_list": "No pending scheduled actions."},
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == 0

    def test_scheduled_list_plus_batch_add_preserved(self, tmp_path):
        """list + successful batch_add advances schedule state, so keep the turn."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_list = ToolCall(id="tc_list", name="schedule_action", arguments={"action": "list"})
            tc_add = ToolCall(
                id="tc_add",
                name="schedule_action",
                arguments={
                    "action": "batch_add",
                    "adds": [
                        {
                            "reason": "take medicine",
                            "trigger_spec": "2026-02-23T23:00",
                        }
                    ],
                },
            )
            _add_tool_round(
                conv,
                tool_calls=[tc_list, tc_add],
                results={
                    "tc_list": "No pending scheduled actions.",
                    "tc_add": (
                        "OK: scheduled 1 action(s)\n"
                        "- 2026-02-23 23:00 (1.0h from now): take medicine"
                    ),
                },
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) > 0


class TestDiscordReviewNoopEviction:
    def test_discord_review_noop_evicted(self, tmp_path):
        """Discord guild review turns with no side effects are evicted."""
        core, q, conv, tc = _make_core(tmp_path)

        conv.add("user", "seed", channel="cli", sender="alice")
        pre_count = len(conv.get_messages())

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="discord", sender="#general @ Guild")
            conv.add("assistant", "noted")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = InboundMessage(
            channel="discord",
            content="[#general @ Guild]\nAlice <@1>: hello",
            priority=1,
            sender="#general @ Guild",
            metadata={
                "source": "guild_review",
                "review_turn": True,
                "evict_if_noop": True,
                "channel_id": "c1",
                "guild_id": "g1",
            },
        )
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == pre_count

    def test_discord_non_review_turn_not_evicted(self, tmp_path):
        """Discord DM/immediate turns are not subject to review eviction rule."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="discord", sender="Alice")
            conv.add("assistant", "ok")
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = InboundMessage(
            channel="discord",
            content="hi",
            priority=1,
            sender="Alice",
            metadata={"source": "dm_immediate", "channel_id": "c1"},
        )
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == 2

    def test_scheduled_batch_add_failure_evicted(self, tmp_path):
        """Failed schedule batch_add has no durable effect and should be evicted."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_add = ToolCall(
                id="tc_add",
                name="schedule_action",
                arguments={
                    "action": "batch_add",
                    "adds": [{"reason": "x", "trigger_spec": "bad"}],
                },
            )
            _add_tool_round(
                conv,
                tool_calls=[tc_add],
                results={"tc_add": "Error: invalid datetime format: 'bad'"},
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == 0

    def test_scheduled_memory_edit_applied_preserved(self, tmp_path):
        """Applied memory_edit changes count as durable side effects."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_mem = ToolCall(
                id="tc_mem",
                name="memory_edit",
                arguments={"as_of": "2026-02-23T12:00:00Z", "turn_id": "t1", "requests": []},
            )
            result = json.dumps(
                {
                    "status": "ok",
                    "turn_id": "t1",
                    "applied": [
                        {
                            "request_id": "r1",
                            "status": "applied",
                            "path": "memory/agent/recent.md",
                        }
                    ],
                    "errors": [],
                    "warnings": [],
                }
            )
            _add_tool_round(
                conv,
                tool_calls=[tc_mem],
                results={"tc_mem": result},
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) > 0

    def test_scheduled_memory_edit_noop_evicted(self, tmp_path):
        """memory_edit with only noop/already_applied should be treated as no-op."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_mem = ToolCall(
                id="tc_mem",
                name="memory_edit",
                arguments={"as_of": "2026-02-23T12:00:00Z", "turn_id": "t1", "requests": []},
            )
            result = json.dumps(
                {
                    "status": "failed",
                    "turn_id": "t1",
                    "applied": [
                        {
                            "request_id": "r1",
                            "status": "noop",
                            "path": "memory/agent/recent.md",
                        },
                        {
                            "request_id": "r2",
                            "status": "already_applied",
                            "path": "memory/agent/pending-thoughts.md",
                        },
                    ],
                    "errors": [
                        {"request_id": "r3", "code": "x", "detail": "failed"}
                    ],
                    "warnings": [],
                }
            )
            _add_tool_round(
                conv,
                tool_calls=[tc_mem],
                results={"tc_mem": result},
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) == 0

    def test_scheduled_no_turn_context_skips_eviction(self, tmp_path):
        """Scheduled eviction is skipped when turn_context is unavailable."""
        core, q, conv, _ = _make_core(tmp_path)
        core.turn_context = None

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            tc_list = ToolCall(id="tc_list", name="schedule_action", arguments={"action": "list"})
            _add_tool_round(
                conv,
                tool_calls=[tc_list],
                results={"tc_list": "No pending scheduled actions."},
            )
            return "completed"

        core.run_turn.side_effect = fake_turn

        msg = _make_scheduled_message()
        q.put(msg)
        _, receipt = q.get()
        core._process_inbound(msg, receipt)

        assert len(conv.get_messages()) > 0


class TestHeartbeatDeferral:
    """Non-heartbeat turns should defer pending heartbeats."""

    def test_non_heartbeat_defers_pending_heartbeat(self, tmp_path):
        """After a Discord DM, the pending heartbeat should be rescheduled."""
        from datetime import timedelta

        core, q, conv, tc = _make_core(tmp_path)

        # Seed a heartbeat that is due in 30 seconds
        old_not_before = tz_now() + timedelta(seconds=30)
        hb = _make_system_heartbeat(not_before=old_not_before)
        q.put(hb)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="discord", sender="Alice")
            conv.add("assistant", "ok")
            return "completed"

        core.run_turn.side_effect = fake_turn

        # Process a non-heartbeat message
        dm = InboundMessage(
            channel="discord", content="hi", priority=0, sender="Alice",
        )
        q.put(dm)
        _, receipt = q.get()
        core._process_inbound(dm, receipt)

        # Old heartbeat should be gone; a new one should exist with later not_before
        pending = q.scan_pending(channel="system")
        heartbeats = [
            (f, m) for f, m in pending
            if m.metadata.get("system") and m.metadata.get("recurring")
        ]
        assert len(heartbeats) == 1
        _, new_hb = heartbeats[0]
        assert new_hb.not_before is not None
        assert new_hb.not_before > old_not_before

    def test_heartbeat_turn_does_not_defer(self, tmp_path):
        """Heartbeat turns use _schedule_next_heartbeat, not defer."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="system", sender="system")
            return "completed"

        core.run_turn.side_effect = fake_turn

        with patch.object(core, "_defer_pending_heartbeat") as mock_defer:
            msg = _make_system_heartbeat()
            q.put(msg)
            _, receipt = q.get()
            core._process_inbound(msg, receipt)

            mock_defer.assert_not_called()

    def test_defer_preserves_recur_spec(self, tmp_path):
        """Deferred heartbeat should retain the original recur_spec."""
        from datetime import timedelta

        core, q, conv, tc = _make_core(tmp_path)

        hb = _make_system_heartbeat(
            not_before=tz_now() + timedelta(seconds=30),
            metadata={"system": True, "recurring": True, "recur_spec": "10m-20m"},
        )
        q.put(hb)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="cli", sender="alice")
            conv.add("assistant", "ok")
            return "completed"

        core.run_turn.side_effect = fake_turn

        dm = InboundMessage(
            channel="cli", content="hi", priority=0, sender="alice",
        )
        q.put(dm)
        _, receipt = q.get()
        core._process_inbound(dm, receipt)

        pending = q.scan_pending(channel="system")
        heartbeats = [
            (f, m) for f, m in pending
            if m.metadata.get("system") and m.metadata.get("recurring")
        ]
        assert len(heartbeats) == 1
        assert heartbeats[0][1].metadata["recur_spec"] == "10m-20m"

    def test_defer_noop_when_no_pending(self, tmp_path):
        """No error when there is no pending heartbeat to defer."""
        core, q, conv, tc = _make_core(tmp_path)

        def fake_turn(content, **kwargs):
            conv.add("user", content, channel="cli", sender="alice")
            conv.add("assistant", "ok")
            return "completed"

        core.run_turn.side_effect = fake_turn

        dm = InboundMessage(
            channel="cli", content="hi", priority=0, sender="alice",
        )
        q.put(dm)
        _, receipt = q.get()
        # Should not raise
        core._process_inbound(dm, receipt)

    def test_failed_turn_does_not_defer(self, tmp_path):
        """If run_turn raises, defer should not be called."""
        from datetime import timedelta

        core, q, conv, tc = _make_core(tmp_path)

        old_not_before = tz_now() + timedelta(seconds=30)
        hb = _make_system_heartbeat(not_before=old_not_before)
        q.put(hb)

        core.run_turn.side_effect = RuntimeError("LLM error")

        dm = InboundMessage(
            channel="discord", content="hi", priority=0, sender="Alice",
        )
        q.put(dm)
        _, receipt = q.get()

        with pytest.raises(RuntimeError):
            core._process_inbound(dm, receipt)

        # Heartbeat should still be there, unchanged
        pending = q.scan_pending(channel="system")
        heartbeats = [
            (f, m) for f, m in pending
            if m.metadata.get("system") and m.metadata.get("recurring")
        ]
        assert len(heartbeats) == 1
