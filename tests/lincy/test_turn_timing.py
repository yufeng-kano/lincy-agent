from datetime import datetime, timedelta, timezone

from lincy.llm.schema import Message
from lincy.session.schema import SessionEntry
from lincy.turn_timing import build_turn_timing_metadata, build_turn_timing_notice


def test_scheduled_turn_near_on_time_does_not_mark_stale():
    event_ts = datetime(2026, 3, 12, 7, 50, tzinfo=timezone.utc)
    turn_metadata = build_turn_timing_metadata(
        channel="system",
        metadata={"scheduled_reason": "wake"},
        event_timestamp=event_ts,
        processing_started_at=event_ts + timedelta(minutes=2),
    )

    assert "turn_processing_delay_reason" not in turn_metadata
    assert "turn_processing_stale" not in turn_metadata


def test_failed_retry_keeps_delay_reason_even_before_stale_threshold():
    event_ts = datetime(2026, 3, 12, 0, 27, tzinfo=timezone.utc)
    turn_metadata = build_turn_timing_metadata(
        channel="discord",
        metadata={"turn_failure_requeue_count": 1},
        event_timestamp=event_ts,
        processing_started_at=event_ts + timedelta(minutes=1),
    )

    assert turn_metadata["turn_processing_delay_reason"] == "failed_retry"


def test_timing_notice_for_stale_scheduled_turn():
    entry = SessionEntry(
        message=Message(
            role="user",
            content="[SCHEDULED]\nReason: wake up",
            timestamp=datetime(2026, 3, 11, 23, 50, tzinfo=timezone.utc),
        ),
        channel="system",
        sender="system",
        metadata={
            "turn_processing_started_at": "2026-03-12T09:11:00+08:00",
            "turn_processing_delay_seconds": 48660,
            "turn_processing_delay_reason": "scheduled_turn",
            "turn_processing_stale": True,
        },
    )

    notice = build_turn_timing_notice(entry)

    assert notice is not None
    assert notice.startswith("[Timing Notice]")
    assert "Current processing time: 2026-03-12 (Thu) 09:11" in notice
    assert "Original event time: 2026-03-12 (Thu) 07:50" in notice
    assert (
        "Do not send stale wake-up, sleep, meal, medication, or schedule "
        "reminder wording." in notice
    )


def test_non_stale_timing_notice_uses_softer_wording():
    entry = SessionEntry(
        message=Message(
            role="user",
            content="retry this",
            timestamp=datetime(2026, 3, 11, 23, 50, tzinfo=timezone.utc),
        ),
        channel="discord",
        sender="alice",
        metadata={
            "turn_failure_requeue_count": 1,
            "turn_processing_started_at": "2026-03-12T08:51:00+08:00",
            "turn_processing_delay_seconds": 60,
            "turn_processing_delay_reason": "failed_retry",
        },
    )

    notice = build_turn_timing_notice(entry)

    assert notice is not None
    assert "This turn is delayed." in notice
    assert "Recheck wake-up, sleep, meal, medication, or schedule reminder wording" in notice
    assert "Do not send stale wake-up" not in notice
