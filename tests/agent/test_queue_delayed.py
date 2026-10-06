"""Tests for delayed message (not_before) support in PersistentPriorityQueue."""

import json
from datetime import datetime, timedelta, timezone

from lincy.timezone_utils import now as tz_now


from lincy.agent.queue import (
    PersistentPriorityQueue,
    _deserialize,
    _is_future,
    _serialize,
)
from lincy.agent.schema import InboundMessage, ShutdownSentinel


def _make_msg(
    content="hi",
    channel="cli",
    priority=0,
    sender="u",
    not_before=None,
    metadata=None,
):
    return InboundMessage(
        channel=channel,
        content=content,
        priority=priority,
        sender=sender,
        not_before=not_before,
        metadata=metadata or {},
    )


def _future(hours=1):
    return tz_now() + timedelta(hours=hours)


def _past(hours=1):
    return tz_now() - timedelta(hours=hours)


# ------------------------------------------------------------------
# Serialization
# ------------------------------------------------------------------


class TestSerializeNotBefore:
    def test_roundtrip_with_not_before(self):
        nb = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        msg = _make_msg(not_before=nb)
        data = _serialize(msg)
        assert "not_before" in data
        restored = _deserialize(data)
        assert restored.not_before == nb

    def test_roundtrip_without_not_before(self):
        msg = _make_msg()
        data = _serialize(msg)
        assert "not_before" not in data
        restored = _deserialize(data)
        assert restored.not_before is None

    def test_backward_compat_old_format(self):
        """Old messages without not_before field still deserialize."""
        data = {
            "channel": "cli",
            "content": "hi",
            "priority": 0,
            "sender": "u",
            "metadata": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        msg = _deserialize(data)
        assert msg.not_before is None


class TestIsFuture:
    def test_none(self):
        assert _is_future(None) is False

    def test_past(self):
        assert _is_future(_past()) is False

    def test_future(self):
        assert _is_future(_future()) is True

    def test_naive_datetime_treated_as_utc(self):
        # Naive datetime far in the future
        dt = datetime(2099, 1, 1)
        assert _is_future(dt) is True


# ------------------------------------------------------------------
# Delayed put
# ------------------------------------------------------------------


class TestDelayedPut:
    def test_future_message_goes_to_delayed(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="later", not_before=_future()))
        # Not in mem queue
        assert q.pending_count() == 0
        # But file exists on disk
        pending_files = list((tmp_path / "q" / "pending").iterdir())
        assert len(pending_files) == 1
        # In delayed pool
        with q._delayed_lock:
            assert len(q._delayed) == 1

    def test_past_not_before_goes_to_mem(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="now", not_before=_past()))
        assert q.pending_count() == 1

    def test_none_not_before_goes_to_mem(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="immediate"))
        assert q.pending_count() == 1

    def test_sentinel_unaffected(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(ShutdownSentinel())
        assert q.pending_count() == 1


# ------------------------------------------------------------------
# Promotion
# ------------------------------------------------------------------


class TestPromoteDue:
    def test_promotes_due_messages(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        # Put a future message
        q.put(_make_msg(content="due", not_before=_future()))
        assert q.pending_count() == 0
        # Manually make it due by modifying the delayed entry
        with q._delayed_lock:
            msg, path = q._delayed[0]
            object.__setattr__(msg, "not_before", _past())
        q._promote_due()
        assert q.pending_count() == 1

    def test_keeps_future_messages(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="not-yet", not_before=_future()))
        q._promote_due()
        assert q.pending_count() == 0
        with q._delayed_lock:
            assert len(q._delayed) == 1

    def test_mixed_promotion(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="a", not_before=_future()))
        q.put(_make_msg(content="b", not_before=_future()))
        # Make only first one due
        with q._delayed_lock:
            msg, path = q._delayed[0]
            object.__setattr__(msg, "not_before", _past())
        q._promote_due()
        assert q.pending_count() == 1
        with q._delayed_lock:
            assert len(q._delayed) == 1


# ------------------------------------------------------------------
# Recovery
# ------------------------------------------------------------------


class TestRecoveryDelayed:
    def test_delayed_messages_recovered_to_delayed_pool(self, tmp_path):
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)
        future = _future(hours=2)
        msg = _make_msg(content="future", not_before=future)
        data = json.dumps(_serialize(msg))
        (qdir / "pending" / "0005_00000001.json").write_text(data)

        q = PersistentPriorityQueue(qdir)

        q.recover()
        assert q.pending_count() == 0
        with q._delayed_lock:
            assert len(q._delayed) == 1

    def test_past_not_before_recovered_to_mem(self, tmp_path):
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)
        past = _past(hours=1)
        msg = _make_msg(content="overdue", not_before=past)
        data = json.dumps(_serialize(msg))
        (qdir / "pending" / "0000_00000001.json").write_text(data)

        q = PersistentPriorityQueue(qdir)

        q.recover()
        assert q.pending_count() == 1

    def test_no_not_before_recovered_to_mem(self, tmp_path):
        qdir = tmp_path / "q"
        (qdir / "pending").mkdir(parents=True)
        (qdir / "active").mkdir(parents=True)
        msg = _make_msg(content="immediate")
        data = json.dumps(_serialize(msg))
        (qdir / "pending" / "0000_00000001.json").write_text(data)

        q = PersistentPriorityQueue(qdir)

        q.recover()
        assert q.pending_count() == 1


# ------------------------------------------------------------------
# Scan and remove
# ------------------------------------------------------------------


class TestScanAndRemove:
    def test_scan_all(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="cli-msg", channel="cli"))
        q.put(_make_msg(content="sys-msg", channel="system"))
        results = q.scan_pending()
        assert len(results) == 2

    def test_scan_by_channel(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="cli-msg", channel="cli"))
        q.put(_make_msg(content="sys-msg", channel="system"))
        results = q.scan_pending(channel="system")
        assert len(results) == 1
        assert results[0][1].content == "sys-msg"

    def test_remove_immediate(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="removeme", channel="system"))
        items = q.scan_pending(channel="system")
        assert len(items) == 1
        filepath = items[0][0]
        assert q.remove_pending(filepath)
        assert len(q.scan_pending(channel="system")) == 0

    def test_remove_delayed(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.put(_make_msg(content="removeme", channel="system", not_before=_future()))
        items = q.scan_pending(channel="system")
        assert len(items) == 1
        filepath = items[0][0]
        assert q.remove_pending(filepath)
        assert len(q.scan_pending(channel="system")) == 0
        with q._delayed_lock:
            assert len(q._delayed) == 0

    def test_remove_nonexistent(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        fake = tmp_path / "q" / "pending" / "nonexistent.json"
        assert not q.remove_pending(fake)


# ------------------------------------------------------------------
# get() skips deleted pending files
# ------------------------------------------------------------------


class TestGetSkipsDeleted:
    def test_skips_externally_deleted(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        # Put two messages
        q.put(_make_msg(content="first", priority=0))
        q.put(_make_msg(content="second", priority=1))
        # Delete the first one's pending file
        items = q.scan_pending()
        first_file = min(items, key=lambda x: x[0].name)[0]
        first_file.unlink()
        # get() should skip the deleted one and return the other
        msg, receipt = q.get()
        assert msg.content == "second"


# ------------------------------------------------------------------
# Promotion thread lifecycle
# ------------------------------------------------------------------


class TestPromotionThread:
    def test_start_stop(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.start_promotion()
        assert q._promotion_thread is not None
        assert q._promotion_thread.is_alive()
        q.stop_promotion()
        assert not q._promotion_thread.is_alive()

    def test_stop_without_start(self, tmp_path):
        q = PersistentPriorityQueue(tmp_path / "q")
        q.stop_promotion()  # Should not raise
