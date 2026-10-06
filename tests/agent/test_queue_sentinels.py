"""Priority of the API-driven control sentinels and explicit queue recovery."""

import json

import pytest

from lincy.agent.queue import PersistentPriorityQueue, _serialize
from lincy.agent.schema import (
    ClearSentinel,
    CompactSentinel,
    InboundMessage,
    MaintenanceSentinel,
    RestartSentinel,
)


def _msg(content: str, priority: int, channel: str = "discord") -> InboundMessage:
    return InboundMessage(channel=channel, content=content, priority=priority, sender="u")


@pytest.mark.parametrize("sentinel_type", [CompactSentinel, ClearSentinel])
def test_prompt_sentinels_run_before_lower_priority_inbound(tmp_path, sentinel_type):
    q = PersistentPriorityQueue(tmp_path / "q")
    q.put(_msg("user", priority=0))
    q.put(_msg("gmail", priority=1))
    q.put(sentinel_type())

    order = [q.get()[0] for _ in range(3)]

    # Priority 0 like user input: FIFO behind the earlier priority-0 message,
    # ahead of everything with a larger priority number.
    assert isinstance(order[0], InboundMessage) and order[0].content == "user"
    assert isinstance(order[1], sentinel_type)
    assert isinstance(order[2], InboundMessage) and order[2].content == "gmail"


def test_restart_sentinel_waits_for_all_ready_inbound(tmp_path):
    q = PersistentPriorityQueue(tmp_path / "q")
    q.put(RestartSentinel())
    q.put(_msg("user", priority=0))
    q.put(_msg("heartbeat", priority=5, channel="system"))

    order = [q.get()[0] for _ in range(3)]

    assert [getattr(item, "content", None) for item in order[:2]] == ["user", "heartbeat"]
    assert isinstance(order[2], RestartSentinel)


def test_restart_shares_maintenance_priority_fifo(tmp_path):
    q = PersistentPriorityQueue(tmp_path / "q")
    q.put(MaintenanceSentinel())
    q.put(RestartSentinel())

    assert isinstance(q.get()[0], MaintenanceSentinel)
    assert isinstance(q.get()[0], RestartSentinel)


@pytest.mark.parametrize("sentinel_type", [CompactSentinel, ClearSentinel, RestartSentinel])
def test_new_sentinels_are_not_persisted(tmp_path, sentinel_type):
    q = PersistentPriorityQueue(tmp_path / "q")
    q.put(sentinel_type())

    assert list((tmp_path / "q" / "pending").iterdir()) == []
    assert q.get() == (sentinel_type(), None)


def _seed(qdir):
    (qdir / "pending").mkdir(parents=True)
    (qdir / "active").mkdir(parents=True)
    (qdir / "active" / "0001_00000001.json").write_text(json.dumps(_serialize(_msg("in-flight", 1))))
    (qdir / "pending" / "0000_00000002.json").write_text(
        json.dumps(_serialize(_msg("stale-cli", 0, channel="cli")))
    )


def test_constructor_does_not_recover(tmp_path):
    qdir = tmp_path / "q"
    _seed(qdir)

    q = PersistentPriorityQueue(qdir, discard_channels={"cli"})

    assert q.pending_count() == 0
    assert (qdir / "active" / "0001_00000001.json").exists()
    assert (qdir / "pending" / "0000_00000002.json").exists()


def test_recover_loads_disk_state_and_applies_discard_channels(tmp_path):
    qdir = tmp_path / "q"
    _seed(qdir)
    q = PersistentPriorityQueue(qdir, discard_channels={"cli"})

    q.recover()

    assert q.pending_count() == 1
    assert q.get()[0].content == "in-flight"
    assert not (qdir / "pending" / "0000_00000002.json").exists()
