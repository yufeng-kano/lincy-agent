"""Tests for PersistentPriorityQueue."""

import json
from datetime import datetime, timezone

from lincy.agent.queue import PersistentPriorityQueue, _serialize, _deserialize
from lincy.agent.schema import InboundMessage, ShutdownSentinel


def _make_msg(content="hi", channel="cli", priority=0, sender="u"):
    return InboundMessage(
        channel=channel,
        content=content,
        priority=priority,
        sender=sender,
    )


class TestSerialize:
    def test_roundtrip(self):
        msg = _make_msg(content="hello")
        data = _serialize(msg)
        restored = _deserialize(data)
        assert restored.channel == msg.channel
        assert restored.content == msg.content
        assert restored.priority == msg.priority
        assert restored.sender == msg.sender

    def test_serialize_contains_required_fields(self):
        msg = _make_msg()
        data = _serialize(msg)
        assert set(data.keys()) == {
            "channel", "content", "priority", "sender", "metadata", "timestamp",
        }


class TestPutAndGet:
    def test_put_get_single(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        msg = _make_msg(content="test")
        q.put(msg)
        got, receipt = q.get()
        assert got.content == "test"
        assert receipt is not None

    def test_priority_ordering(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="low", priority=2))
        q.put(_make_msg(content="high", priority=0))
        q.put(_make_msg(content="mid", priority=1))

        r1, _ = q.get()
        r2, _ = q.get()
        r3, _ = q.get()
        assert r1.content == "high"
        assert r2.content == "mid"
        assert r3.content == "low"

    def test_fifo_within_same_priority(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="first", priority=0))
        q.put(_make_msg(content="second", priority=0))
        q.put(_make_msg(content="third", priority=0))

        r1, _ = q.get()
        r2, _ = q.get()
        r3, _ = q.get()
        assert r1.content == "first"
        assert r2.content == "second"
        assert r3.content == "third"


class TestPersistence:
    def test_pending_file_created(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="persist"))
        pending = list((tmp_path / "q" / "pending").iterdir())
        assert len(pending) == 1
        data = json.loads(pending[0].read_text())
        assert data["content"] == "persist"

    def test_get_moves_to_active(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="move"))
        _, receipt = q.get()
        assert receipt is not None
        assert receipt.parent.name == "active"
        pending = list((tmp_path / "q" / "pending").iterdir())
        assert len(pending) == 0

    def test_ack_deletes_file(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="delete"))
        _, receipt = q.get()
        q.ack(receipt)
        active = list((tmp_path / "q" / "active").iterdir())
        assert len(active) == 0

    def test_ack_none_is_noop(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.ack(None)  # should not raise

    def test_requeue_active_rewrites_same_inflight_message(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="retry-me", priority=1))
        _, receipt = q.get()
        assert receipt is not None

        retried = InboundMessage(
            channel="cli",
            content="retry-me",
            priority=1,
            sender="u",
            metadata={"turn_failure_requeue_count": 1, "anchor_shared_rev": 7},
            not_before=datetime.now(timezone.utc),
        )
        pending_path = q.requeue_active(receipt, retried)

        assert not receipt.exists()
        assert pending_path.parent.name == "pending"
        pending_files = list((tmp_path / "q" / "pending").iterdir())
        assert pending_files == [pending_path]
        restored = _deserialize(json.loads(pending_path.read_text()))
        assert restored.metadata["turn_failure_requeue_count"] == 1
        assert restored.metadata["anchor_shared_rev"] == 7


class TestRecovery:
    def test_active_recovered_on_startup(self, tmp_path):
        """Simulate crash: leave a file in active/, verify it's recovered."""
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)

        msg = _make_msg(content="crashed")
        data = json.dumps(_serialize(msg))
        (qdir / "active" / "0000_00000001.json").write_text(data)

        q = PersistentPriorityQueue(qdir)

        q.recover()
        assert q.pending_count() == 1
        got, _ = q.get()
        assert got.content == "crashed"

    def test_discard_channels(self, tmp_path):
        """CLI messages are discarded on startup, others kept."""
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)

        cli_msg = _make_msg(content="stale-cli", channel="cli")
        line_msg = _make_msg(content="keep-line", channel="line", priority=1)
        (qdir / "pending" / "0000_00000001.json").write_text(
            json.dumps(_serialize(cli_msg)),
        )
        (qdir / "pending" / "0001_00000002.json").write_text(
            json.dumps(_serialize(line_msg)),
        )

        q = PersistentPriorityQueue(qdir, discard_channels={"cli"})

        q.recover()
        assert q.pending_count() == 1
        got, _ = q.get()
        assert got.content == "keep-line"

    def test_corrupt_file_skipped(self, tmp_path):
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)

        (qdir / "pending" / "0000_00000001.json").write_text("not json")
        q = PersistentPriorityQueue(qdir)
        q.recover()
        assert q.pending_count() == 0


class TestShutdownSentinel:
    def test_sentinel_not_persisted(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel(graceful=True))
        pending = list((tmp_path / "q" / "pending").iterdir())
        assert len(pending) == 0

    def test_sentinel_has_highest_priority(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="normal", priority=0))
        q.put(ShutdownSentinel())

        got, receipt = q.get()
        assert isinstance(got, ShutdownSentinel)
        assert receipt is None

    def test_sentinel_graceful_flag(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel(graceful=False))
        got, _ = q.get()
        assert isinstance(got, ShutdownSentinel)
        assert got.graceful is False


class TestPendingCount:
    def test_empty(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        assert q.pending_count() == 0

    def test_after_put(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg())
        assert q.pending_count() == 1

    def test_after_get(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg())
        q.get()
        assert q.pending_count() == 0


class TestReadyInboundForConversation:
    def _scoped(self, content, scope_id, channel="discord"):
        return InboundMessage(
            channel=channel,
            content=content,
            priority=0,
            sender="u",
            metadata={"scope_id": scope_id},
        )

    def test_has_ready_matches_scope_only(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(self._scoped("a", "discord:dm:1"))

        assert q.has_ready_pending_inbound(scope_id="discord:dm:1", channel="discord")
        assert not q.has_ready_pending_inbound(scope_id="discord:dm:2", channel="discord")
        assert q.has_ready_pending_inbound_for_scope("discord:dm:1")

    def test_has_ready_falls_back_to_channel_without_scope(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(channel="cli"))

        assert q.has_ready_pending_inbound(scope_id=None, channel="cli")
        assert not q.has_ready_pending_inbound(scope_id=None, channel="discord")

    def test_system_messages_never_count(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(InboundMessage(
            channel="system", content="hb", priority=5, sender="system",
            metadata={"scope_id": "discord:dm:1"},
        ))

        assert not q.has_ready_pending_inbound(scope_id="discord:dm:1", channel="discord")
        assert not q.has_ready_pending_inbound(scope_id=None, channel="system")

    def test_take_moves_matching_messages_to_active_in_order(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(self._scoped("first", "discord:dm:1"))
        q.put(self._scoped("other", "discord:dm:2"))
        q.put(self._scoped("second", "discord:dm:1"))

        taken = q.take_ready_pending_inbound(scope_id="discord:dm:1", channel="discord")

        assert [m.content for m, _ in taken] == ["first", "second"]
        for _, receipt in taken:
            assert receipt.parent == tmp_path / "q" / "active"
            assert receipt.exists()
        assert q.pending_inbound_count() == 1
        remaining, _ = q.get()
        assert remaining.content == "other"

    def test_take_returns_empty_when_nothing_matches(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(self._scoped("other", "discord:dm:2"))

        assert q.take_ready_pending_inbound(scope_id="discord:dm:1", channel="discord") == []
        assert q.pending_inbound_count() == 1

    def test_take_skips_delayed_messages(self, tmp_path):
        from datetime import timedelta

        q = PersistentPriorityQueue(tmp_path / "q")
        msg = self._scoped("later", "discord:dm:1")
        msg.not_before = datetime.now(timezone.utc) + timedelta(hours=1)
        q.put(msg)

        assert q.take_ready_pending_inbound(scope_id="discord:dm:1", channel="discord") == []

    def test_taken_receipts_ack_like_get(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(channel="cli"))

        [(_, receipt)] = q.take_ready_pending_inbound(scope_id=None, channel="cli")
        q.ack(receipt)

        assert not receipt.exists()
        assert q.pending_inbound_count() == 0
