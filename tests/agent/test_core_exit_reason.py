"""AgentCore.run() reports why it stopped so the host can exit or exec."""

import threading
from unittest.mock import MagicMock

from lincy.agent.core import AgentCore
from lincy.agent.handle import ExitReason
from lincy.agent.queue import PersistentPriorityQueue
from lincy.agent.schema import (
    ClearSentinel,
    InboundMessage,
    RestartSentinel,
    ShutdownSentinel,
)


def _core(tmp_path) -> AgentCore:
    core = AgentCore.__new__(AgentCore)
    core._queue = PersistentPriorityQueue(tmp_path / "q")
    core.adapters = {}
    core._maintenance_scheduler = None
    core.config = None
    core._busy = threading.Event()
    core.graceful_exit = MagicMock()
    return core


def test_run_returns_restart_after_graceful_exit(tmp_path):
    core = _core(tmp_path)
    core._queue.put(RestartSentinel())

    assert core.run() is ExitReason.RESTART
    core.graceful_exit.assert_called_once()


def test_run_returns_shutdown_on_shutdown_sentinel(tmp_path):
    core = _core(tmp_path)
    core._queue.put(ShutdownSentinel(graceful=False))

    assert core.run() is ExitReason.SHUTDOWN
    core.graceful_exit.assert_not_called()


def test_restart_runs_only_after_pending_inbound(tmp_path):
    core = _core(tmp_path)
    seen: list[str] = []
    busy_during_turn: list[bool] = []

    def _process(msg, receipt):
        busy_during_turn.append(core.is_busy())
        seen.append(msg.content)
        core._queue.ack(receipt)

    core._process_inbound = _process
    core._queue.put(RestartSentinel())
    core._queue.put(InboundMessage(channel="cli", content="hello", priority=0, sender="u"))

    assert core.run() is ExitReason.RESTART
    assert seen == ["hello"]
    assert busy_during_turn == [True]
    assert core.is_busy() is False


def test_clear_sentinel_clears_conversation_and_reports(tmp_path):
    core = _core(tmp_path)
    core.conversation = MagicMock()
    core.turn_context = MagicMock()
    core.console = MagicMock()
    core._queue.put(ClearSentinel())
    # Shutdown would jump ahead (priority -1); restart (999) drains the clear first.
    core._queue.put(RestartSentinel())

    assert core.run() is ExitReason.RESTART
    core.conversation.clear.assert_called_once_with()
    core.turn_context.clear.assert_called_once_with()
    core.console.print_info.assert_called_once_with("Conversation cleared.")


def test_compact_sentinel_runs_manual_compact_and_reports(tmp_path):
    from lincy.agent.compaction import ContextCompactionResult
    from lincy.agent.schema import CompactSentinel

    core = _core(tmp_path)
    core.console = MagicMock()
    core.run_manual_compact = MagicMock(
        return_value=ContextCompactionResult(
            changed=True,
            removed_messages=3,
            source="compactor",
            trigger="manual",
        )
    )
    core._queue.put(CompactSentinel())
    core._queue.put(RestartSentinel())

    assert core.run() is ExitReason.RESTART
    core.console.print_info.assert_called_once_with(
        "Context compacted via compactor agent: 3 messages removed."
    )
