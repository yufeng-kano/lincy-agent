"""Tests for AgentCore queue-based methods."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from lincy.agent.schema import (
    InboundMessage,
    NewSessionSentinel,
    ReloadSentinel,
    ReloadSystemPromptSentinel,
    ShutdownSentinel,
)
from lincy.agent.turn_context import ProactiveTurnYield



class TestEnqueueAndShutdown:
    """Test enqueue / request_shutdown."""

    def test_enqueue_without_queue_raises(self):
        from lincy.agent.core import AgentCore

        core = AgentCore.__new__(AgentCore)
        core._queue = None
        with pytest.raises(RuntimeError, match="No queue configured"):
            core.enqueue(InboundMessage(channel="cli", content="x", priority=0, sender="u"))

    def test_request_shutdown_pushes_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core.request_shutdown(graceful=True)

        msg, receipt = q.get()
        assert isinstance(msg, ShutdownSentinel)
        assert msg.graceful is True
        assert receipt is None  # sentinel not persisted

    def test_request_new_session_pushes_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}

        core.request_new_session()

        msg, receipt = q.get()
        assert isinstance(msg, NewSessionSentinel)
        assert receipt is None

    def test_request_reload_pushes_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}

        core.request_reload()

        msg, receipt = q.get()
        assert isinstance(msg, ReloadSentinel)
        assert receipt is None

    def test_request_reload_system_prompt_pushes_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}

        core.request_reload_system_prompt()

        msg, receipt = q.get()
        assert isinstance(msg, ReloadSystemPromptSentinel)
        assert receipt is None

    def test_enqueue_stamps_scope_and_anchor_when_shared_state_available(self, tmp_path):
        from lincy.agent.core import AgentCore
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.scope import DEFAULT_SCOPE_RESOLVER
        from lincy.agent.shared_state import SharedStateStore

        q = PersistentPriorityQueue(tmp_path / "q")
        store = SharedStateStore(tmp_path / "shared_state.json")
        store.record_shared_outbound(
            scope_id="discord:dm:123",
            channel="discord",
            recipient="friend",
            body="hi",
        )

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.shared_state_store = store
        core.scope_resolver = DEFAULT_SCOPE_RESOLVER

        core.enqueue(
            InboundMessage(
                channel="discord",
                content="x",
                priority=1,
                sender="friend",
                metadata={"is_dm": True, "author_id": "123", "channel_id": "dm1"},
            )
        )

        msg, _ = q.get()
        assert msg.metadata["scope_id"] == "discord:dm:123"
        assert msg.metadata["anchor_shared_rev"] == 1


class TestTurnMetadata:
    def test_ensure_turn_runtime_metadata_backfills_processing_started_at(self, monkeypatch):
        from lincy.agent.core import _ensure_turn_runtime_metadata

        fixed_now = datetime(2026, 3, 12, 1, 11, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "lincy.agent.core.tz_now",
            lambda: fixed_now,
            raising=False,
        )

        metadata = _ensure_turn_runtime_metadata(
            channel="cli",
            timestamp=None,
            metadata=None,
        )

        assert metadata["turn_processing_started_at"] == fixed_now.isoformat()
        assert metadata["turn_processing_delay_seconds"] == 0
        assert "turn_processing_delay_reason" not in metadata


class TestTurnFailureClassification:
    def test_classify_http_529_as_transport(self):
        from lincy.agent.requeue import classify_turn_failure

        request = httpx.Request("POST", "http://localhost:4142/v1/messages")
        error = httpx.HTTPStatusError(
            "HTTP 529",
            request=request,
            response=httpx.Response(
                529,
                request=request,
                text='{"error":{"type":"overloaded_error","message":"Overloaded"}}',
            ),
        )

        assert classify_turn_failure(error) == "transport"


class TestRun:
    """Test AgentCore.run() loop."""

    def test_run_stops_on_graceful_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel(graceful=True))

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        core.run()
        core.graceful_exit.assert_called_once()

    def test_run_stops_on_non_graceful_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel(graceful=False))

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        core.run()
        core.graceful_exit.assert_not_called()

    def test_run_processes_message_then_stops(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        msg = InboundMessage(channel="cli", content="test", priority=0, sender="u")
        q.put(msg)

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        processed = []

        def fake_process(m, receipt):
            processed.append(m.content)
            # After processing, signal shutdown
            q.put(ShutdownSentinel(graceful=False))

        core._process_inbound = fake_process

        core.run()
        assert processed == ["test"]

    def test_run_handles_new_session_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(NewSessionSentinel())

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        def fake_perform_new_session():
            q.put(ShutdownSentinel(graceful=False))

        core._perform_new_session = MagicMock(side_effect=fake_perform_new_session)

        core.run()

        core._perform_new_session.assert_called_once()

    def test_run_handles_reload_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ReloadSentinel())

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        def fake_reload_resources():
            q.put(ShutdownSentinel(graceful=False))

        core._perform_reload_resources = MagicMock(
            side_effect=fake_reload_resources
        )

        core.run()

        core._perform_reload_resources.assert_called_once()

    def test_run_handles_reload_system_prompt_sentinel(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ReloadSystemPromptSentinel())

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        def fake_reload_system_prompt():
            q.put(ShutdownSentinel(graceful=False))

        core._perform_reload_system_prompt = MagicMock(
            side_effect=fake_reload_system_prompt
        )

        core.run()

        core._perform_reload_system_prompt.assert_called_once()

    def test_run_starts_and_stops_adapters(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore

        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel(graceful=False))

        adapter = MagicMock()
        adapter.channel_name = "cli"

        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.adapters = {"cli": adapter}
        core._maintenance_scheduler = None
        core.config = None
        core.graceful_exit = MagicMock()

        core.run()
        adapter.start.assert_called_once_with(core)
        adapter.stop.assert_called_once()


class TestProcessInboundLifecycle:
    """Test that _process_inbound notifies all adapters."""

    def _make_core(self, tmp_path):
        from lincy.agent.queue import PersistentPriorityQueue
        from lincy.agent.core import AgentCore
        from lincy.context.conversation import Conversation

        q = PersistentPriorityQueue(tmp_path / "q")
        core = AgentCore.__new__(AgentCore)
        core._queue = q
        core.console = MagicMock()
        core.adapters = {}
        core.turn_context = None
        core.copilot_runtime = None
        core.conversation = Conversation()
        core.run_turn = MagicMock(return_value="completed")
        core._last_proactive_yield = None
        core.config = SimpleNamespace(
            app=SimpleNamespace(
                turn_failure_requeue_limit=1,
                turn_failure_requeue_delay_seconds=60,
                requeue_non_retryable_turn_failures=False,
            )
        )
        return core, q

    def test_on_turn_start_called_on_all_adapters(self, tmp_path):
        core, q = self._make_core(tmp_path)
        cli_adapter = MagicMock()
        cli_adapter.channel_name = "cli"
        gmail_adapter = MagicMock()
        gmail_adapter.channel_name = "gmail"
        core.adapters = {"cli": cli_adapter, "gmail": gmail_adapter}

        msg = InboundMessage(channel="gmail", content="hi", priority=1, sender="x")
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        cli_adapter.on_turn_start.assert_called_once_with("gmail")
        gmail_adapter.on_turn_start.assert_called_once_with("gmail")

    def test_on_turn_complete_called_on_all_adapters(self, tmp_path):
        core, q = self._make_core(tmp_path)
        cli_adapter = MagicMock()
        cli_adapter.channel_name = "cli"
        gmail_adapter = MagicMock()
        gmail_adapter.channel_name = "gmail"
        core.adapters = {"cli": cli_adapter, "gmail": gmail_adapter}

        msg = InboundMessage(channel="gmail", content="hi", priority=1, sender="x")
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        cli_adapter.on_turn_complete.assert_called_once()
        gmail_adapter.on_turn_complete.assert_called_once()

    def test_on_turn_complete_called_even_on_error(self, tmp_path):
        core, q = self._make_core(tmp_path)
        adapter = MagicMock()
        adapter.channel_name = "cli"
        core.adapters = {"cli": adapter}
        core.run_turn.side_effect = RuntimeError("boom")

        msg = InboundMessage(channel="cli", content="x", priority=0, sender="u")
        q.put(msg)
        _, receipt = q.get()

        with pytest.raises(RuntimeError):
            core._process_inbound(msg, receipt)

        adapter.on_turn_complete.assert_called_once()

    def test_on_turn_start_before_console_output(self, tmp_path):
        """on_turn_start must be called before any console output."""
        core, q = self._make_core(tmp_path)
        order = []
        adapter = MagicMock()
        adapter.channel_name = "cli"
        adapter.on_turn_start.side_effect = lambda ch: order.append("turn_start")
        core.console.print_inbound.side_effect = lambda *a, **k: order.append("print_inbound")
        core.adapters = {"cli": adapter}

        msg = InboundMessage(channel="cli", content="x", priority=0, sender="u")
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert order.index("turn_start") < order.index("print_inbound")

    def test_turn_context_set_before_on_turn_start(self, tmp_path):
        """Adapters can inspect inbound metadata during on_turn_start."""
        core, q = self._make_core(tmp_path)
        order = []

        class _FakeTurnContext:
            def __init__(self):
                self.metadata = {}
                self.sent_hashes = set()
                self.pending_outbound = []

            def set_inbound(self, channel, sender, metadata):
                del channel, sender
                self.metadata = dict(metadata)
                order.append("set_inbound")

            def clear(self):
                self.sent_hashes = set()
                self.pending_outbound = []

        tc = _FakeTurnContext()
        core.turn_context = tc
        adapter = MagicMock()
        adapter.channel_name = "discord"

        def _on_turn_start(_channel):
            assert tc.metadata.get("channel_id") == "c1"
            order.append("turn_start")

        adapter.on_turn_start.side_effect = _on_turn_start
        core.adapters = {"discord": adapter}

        msg = InboundMessage(
            channel="discord",
            content="x",
            priority=1,
            sender="chan",
            metadata={"channel_id": "c1"},
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert order[:2] == ["set_inbound", "turn_start"]

    def test_print_inbound_uses_message_timestamp(self, tmp_path):
        core, q = self._make_core(tmp_path)
        ts = datetime(2026, 3, 1, 14, 37, tzinfo=timezone.utc)
        msg = InboundMessage(
            channel="cli",
            content="x",
            priority=0,
            sender="u",
            timestamp=ts,
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert core.console.print_inbound.call_args.kwargs["ts"] == ts

    def test_failed_turn_requeues_inbound_with_delay(self, tmp_path):
        core, q = self._make_core(tmp_path)
        core.run_turn.return_value = "failed"

        msg = InboundMessage(
            channel="discord",
            content="retry me",
            priority=1,
            sender="friend",
            metadata={"anchor_shared_rev": 7, "scope_id": "discord:dm:friend"},
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        pending = q.scan_pending(channel="discord")
        assert len(pending) == 1
        _, retried = pending[0]
        assert retried.content == "retry me"
        assert retried.sender == "friend"
        assert retried.timestamp == msg.timestamp
        assert retried.metadata["turn_failure_requeue_count"] == 1
        assert retried.metadata["anchor_shared_rev"] == 7
        assert retried.metadata["scope_id"] == "discord:dm:friend"
        assert retried.not_before is not None
        assert retried.not_before > msg.timestamp

    def test_failed_turn_acks_when_requeue_budget_exhausted(self, tmp_path):
        core, q = self._make_core(tmp_path)
        core.run_turn.return_value = "failed"

        msg = InboundMessage(
            channel="discord",
            content="drop after retry",
            priority=1,
            sender="friend",
            metadata={"turn_failure_requeue_count": 1},
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert q.scan_pending(channel="discord") == []

    def test_request_format_failure_is_acked_without_requeue(self, tmp_path):
        core, q = self._make_core(tmp_path)

        def _run_turn(*args, **kwargs):
            del args, kwargs
            core._last_turn_failure_category = "request-format"
            return "failed"

        core.run_turn.side_effect = _run_turn

        msg = InboundMessage(
            channel="discord",
            content="do not replay me",
            priority=1,
            sender="friend",
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert q.scan_pending(channel="discord") == []

    def test_request_format_failure_can_be_requeued_by_config(self, tmp_path):
        core, q = self._make_core(tmp_path)
        core.config.app.requeue_non_retryable_turn_failures = True

        def _run_turn(*args, **kwargs):
            del args, kwargs
            core._last_turn_failure_category = "request-format"
            return "failed"

        core.run_turn.side_effect = _run_turn

        msg = InboundMessage(
            channel="discord",
            content="retry deterministic failure",
            priority=1,
            sender="friend",
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        pending = q.scan_pending(channel="discord")
        assert len(pending) == 1
        _, retried = pending[0]
        assert retried.content == "retry deterministic failure"
        assert retried.metadata["turn_failure_requeue_count"] == 1

    def test_interrupted_turn_is_acked_without_requeue(self, tmp_path):
        core, q = self._make_core(tmp_path)
        core.run_turn.return_value = "interrupted"

        msg = InboundMessage(channel="cli", content="cancel me", priority=0, sender="u")
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert q.scan_pending(channel="cli") == []

    def test_yielded_scheduled_turn_is_requeued_for_reevaluation(self, tmp_path):
        core, q = self._make_core(tmp_path)

        def _run_turn(*args, **kwargs):
            del args, kwargs
            core._last_proactive_yield = ProactiveTurnYield("discord:dm:123")
            return "completed"

        core.run_turn.side_effect = _run_turn

        msg = InboundMessage(
            channel="system",
            content=(
                "[SCHEDULED]\n"
                "Reason: meds\n"
                "Scheduled at: 2026-03-08 22:30\n\n"
                "Act on this reason. Use send_message to deliver messages."
            ),
            priority=2,
            sender="system",
            metadata={"scheduled_reason": "meds"},
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        pending = q.scan_pending(channel="system")
        assert len(pending) == 1
        _, requeued = pending[0]
        assert requeued.priority == 2
        assert requeued.metadata["scheduled_reason"] == "meds"
        assert requeued.metadata["yielded_scope_id"] == "discord:dm:123"
        assert requeued.metadata["yield_reschedule_count"] == 1
        assert requeued.not_before is not None
        assert requeued.not_before > msg.timestamp
        assert "Reevaluate whether action is still needed." in requeued.content

    def test_retry_turn_sets_processing_timing_metadata_before_run_turn(self, tmp_path):
        from lincy.agent.turn_context import TurnContext

        core, q = self._make_core(tmp_path)
        core.turn_context = TurnContext()

        seen_metadata = {}

        def _run_turn(*args, **kwargs):
            del args, kwargs
            seen_metadata.update(core.turn_context.metadata)
            return "interrupted"

        core.run_turn.side_effect = _run_turn

        msg = InboundMessage(
            channel="discord",
            content="retry me later",
            priority=1,
            sender="friend",
            metadata={"turn_failure_requeue_count": 1},
            timestamp=datetime(2026, 3, 11, 16, 27, tzinfo=timezone.utc),
        )
        q.put(msg)
        _, receipt = q.get()

        core._process_inbound(msg, receipt)

        assert "turn_processing_started_at" in seen_metadata
        assert seen_metadata["turn_processing_delay_reason"] == "failed_retry"
        assert seen_metadata["turn_processing_delay_seconds"] >= 0
