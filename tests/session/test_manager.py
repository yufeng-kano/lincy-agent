"""Tests for SessionManager."""

import json
from datetime import datetime, timezone as tz
from pathlib import Path

import pytest

from lincy.llm.schema import Message, ToolCall
from lincy.session.manager import SessionManager
from lincy.session.debug_schema import (
    SessionCheckpoint,
    SessionLLMRequestRecord,
    SessionLLMResponseRecord,
    SessionTurnRecord,
)
from lincy.session.schema import SessionEntry
from lincy.llm import LLMResponse, ServedCandidate, ToolDefinition, ToolParameter


def _entry(msg: Message, *, channel: str | None = None, sender: str | None = None) -> SessionEntry:
    """Wrap a Message in a SessionEntry for testing."""
    return SessionEntry(message=msg, channel=channel, sender=sender)


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


@pytest.fixture
def mgr(sessions_dir: Path) -> SessionManager:
    return SessionManager(sessions_dir)


class TestCreate:
    def test_creates_directory_and_meta(self, mgr: SessionManager, sessions_dir: Path):
        sid = mgr.create("alice", "Alice")
        session_dir = sessions_dir / sid
        assert session_dir.is_dir()
        assert (session_dir / "meta.json").exists()

    def test_meta_fields(self, mgr: SessionManager, sessions_dir: Path):
        sid = mgr.create("bob", "Bob")
        from lincy.session.schema import SessionMetadata

        meta = SessionMetadata.model_validate_json(
            (sessions_dir / sid / "meta.json").read_text()
        )
        assert meta.session_id == sid
        assert meta.user_id == "bob"
        assert meta.display_name == "Bob"
        assert meta.status == "active"
        assert meta.message_count == 0

    def test_sets_current_session(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        assert mgr.current_session_id == sid


class TestAppendAndLoad:
    def test_append_and_load_roundtrip(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        entry = _entry(
            Message(role="user", content="hello", timestamp=datetime.now(tz.utc)),
            channel="cli",
            sender="alice",
        )
        mgr.append_message(entry)

        entries = mgr.load(sid)
        assert len(entries) == 1
        assert entries[0].role == "user"
        assert entries[0].content == "hello"
        assert entries[0].channel == "cli"
        assert entries[0].sender == "alice"

    def test_multiple_messages(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        mgr.append_message(_entry(Message(role="user", content="hi"), channel="cli"))
        mgr.append_message(_entry(Message(role="assistant", content="hello")))
        mgr.append_message(_entry(Message(role="user", content="bye"), channel="cli"))

        entries = mgr.load(sid)
        assert len(entries) == 3
        assert [e.role for e in entries] == ["user", "assistant", "user"]

    def test_updates_message_count(self, mgr: SessionManager, sessions_dir: Path):
        sid = mgr.create("alice", "Alice")
        mgr.append_message(_entry(Message(role="user", content="one")))
        mgr.append_message(_entry(Message(role="assistant", content="two")))

        from lincy.session.schema import SessionMetadata

        meta = SessionMetadata.model_validate_json(
            (sessions_dir / sid / "meta.json").read_text()
        )
        assert meta.message_count == 2

    def test_tool_call_message_roundtrip(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        tool_calls = [
            ToolCall(id="tc1", name="get_time", arguments={"tz": "UTC"}),
        ]
        mgr.append_message(
            _entry(Message(role="assistant", content=None, tool_calls=tool_calls))
        )
        mgr.append_message(
            _entry(Message(
                role="tool",
                content='{"time": "12:00"}',
                tool_call_id="tc1",
                name="get_time",
            ))
        )

        entries = mgr.load(sid)
        assert len(entries) == 2
        assert entries[0].tool_calls is not None
        assert entries[0].tool_calls[0].name == "get_time"
        assert entries[0].tool_calls[0].arguments == {"tz": "UTC"}
        assert entries[1].role == "tool"
        assert entries[1].tool_call_id == "tc1"

    def test_append_without_create_is_noop(self, sessions_dir: Path):
        mgr = SessionManager(sessions_dir)
        mgr.append_message(_entry(Message(role="user", content="ignored")))
        assert list(sessions_dir.iterdir()) == []


class TestFinalize:
    def test_finalize_completed(self, mgr: SessionManager, sessions_dir: Path):
        sid = mgr.create("alice", "Alice")
        mgr.finalize("completed")

        from lincy.session.schema import SessionMetadata

        meta = SessionMetadata.model_validate_json(
            (sessions_dir / sid / "meta.json").read_text()
        )
        assert meta.status == "completed"

    def test_finalize_exited(self, mgr: SessionManager, sessions_dir: Path):
        sid = mgr.create("alice", "Alice")
        mgr.finalize("exited")

        from lincy.session.schema import SessionMetadata

        meta = SessionMetadata.model_validate_json(
            (sessions_dir / sid / "meta.json").read_text()
        )
        assert meta.status == "exited"


class TestListRecent:
    def test_empty(self, mgr: SessionManager):
        assert mgr.list_recent("alice") == []

    def test_lists_user_sessions(self, mgr: SessionManager):
        mgr.create("alice", "Alice")
        mgr.create("bob", "Bob")
        mgr.create("alice", "Alice2")

        results = mgr.list_recent("alice")
        assert len(results) == 2
        assert all(m.user_id == "alice" for m in results)

    def test_sorted_by_updated_at_desc(self, mgr: SessionManager):
        s1 = mgr.create("alice", "Alice")
        mgr.append_message(_entry(Message(role="user", content="first")))

        s2 = mgr.create("alice", "Alice")
        mgr.append_message(_entry(Message(role="user", content="second")))

        results = mgr.list_recent("alice")
        assert results[0].session_id == s2
        assert results[1].session_id == s1

    def test_limit(self, mgr: SessionManager):
        for _ in range(5):
            mgr.create("alice", "Alice")

        results = mgr.list_recent("alice", limit=3)
        assert len(results) == 3


class TestLoadNonexistent:
    def test_raises_on_missing(self, mgr: SessionManager):
        with pytest.raises(FileNotFoundError):
            mgr.load("nonexistent_session")

    def test_load_empty_session(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        entries = mgr.load(sid)
        assert entries == []


class TestRewriteMessages:
    def test_rewrite(self, mgr: SessionManager):
        sid = mgr.create("alice", "Alice")
        mgr.append_message(_entry(Message(role="user", content="one")))
        mgr.append_message(_entry(Message(role="assistant", content="two")))

        # Rewrite with fewer entries
        mgr.rewrite_messages([_entry(Message(role="user", content="only"))])
        entries = mgr.load(sid)
        assert len(entries) == 1
        assert entries[0].content == "only"


class TestDebugArtifacts:
    def test_finish_turn_writes_turn_summary_and_checkpoint(
        self,
        mgr: SessionManager,
        sessions_dir: Path,
    ):
        sid = mgr.create("alice", "Alice")
        started_at = datetime.now(tz.utc)
        mgr.start_turn(
            channel="cli",
            sender="alice",
            inbound_kind="user_message",
            input_text="hello",
            input_timestamp=started_at,
            turn_metadata={"scope_id": "scope-1"},
        )
        entries = [
            _entry(Message(role="user", content="hello", timestamp=started_at)),
            _entry(Message(role="assistant", content="hi there")),
        ]
        for entry in entries:
            mgr.append_message(entry)

        mgr.finish_turn(
            status="completed",
            final_content="hi there",
            failure_category=None,
            soft_limit_exceeded=False,
            turn_messages=entries,
            checkpoint_messages=entries,
        )

        turn_path = sessions_dir / sid / "turns.jsonl"
        turn = SessionTurnRecord.model_validate_json(
            turn_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert turn.input_text == "hello"
        assert turn.final_content == "hi there"
        assert turn.turn_message_count == 2

        checkpoint = SessionCheckpoint.model_validate_json(
            (sessions_dir / sid / "checkpoints" / "latest.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(checkpoint.messages) == 2
        assert checkpoint.messages[1].content == "hi there"

    def test_compaction_event_is_written_to_turn_and_events(
        self,
        mgr: SessionManager,
        sessions_dir: Path,
    ):
        sid = mgr.create("alice", "Alice")
        started_at = datetime.now(tz.utc)
        mgr.start_turn(
            channel="cli",
            sender="alice",
            inbound_kind="user_message",
            input_text="hello",
            input_timestamp=started_at,
            turn_metadata=None,
        )
        mgr.record_compaction(
            source="codex_remote",
            trigger="soft_limit",
            removed_messages=7,
            fallback=False,
        )
        entries = [
            _entry(Message(role="user", content="hello", timestamp=started_at)),
            _entry(Message(role="assistant", content="hi there")),
        ]
        mgr.finish_turn(
            status="completed",
            final_content="hi there",
            failure_category=None,
            soft_limit_exceeded=True,
            turn_messages=entries,
            checkpoint_messages=entries,
        )

        turn_path = sessions_dir / sid / "turns.jsonl"
        turn = SessionTurnRecord.model_validate_json(
            turn_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert turn.compaction_source == "codex_remote"
        assert turn.compaction_trigger == "soft_limit"
        assert turn.compacted_messages_removed == 7
        assert turn.compaction_fallback is False

        event_path = sessions_dir / sid / "events.jsonl"
        events = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        compaction_event = next(event for event in events if event["kind"] == "compaction")
        assert compaction_event["data"] == {
            "source": "codex_remote",
            "trigger": "soft_limit",
            "removed_messages": 7,
            "fallback": False,
        }

    def test_llm_request_response_logs_include_cache_usage(
        self,
        mgr: SessionManager,
        sessions_dir: Path,
    ):
        sid = mgr.create("alice", "Alice")
        mgr.start_turn(
            channel="cli",
            sender="alice",
            inbound_kind="user_message",
            input_text="plan this",
            input_timestamp=datetime.now(tz.utc),
            turn_metadata=None,
        )
        tool = ToolDefinition(
            name="schedule_action",
            description="schedule later work",
            parameters={
                "action": ToolParameter(
                    type="string",
                    description="operation",
                ),
            },
            required=["action"],
        )
        pending = mgr.begin_llm_request(
            client_label="brain",
            provider="claude_code",
            model="claude-sonnet-4-6",
            call_type="chat_with_tools",
            messages=[Message(role="user", content="plan this")],
            tools=[tool],
            temperature=0.2,
        )
        assert pending is not None

        mgr.complete_llm_response(
            pending,
            response=LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="tool_1",
                        name="schedule_action",
                        arguments={
                            "action": "batch_add",
                            "adds": [
                                {
                                    "reason": "plan later",
                                    "trigger_spec": "2030-01-01T00:00",
                                }
                            ],
                        },
                    )
                ],
                finish_reason="tool_use",
                prompt_tokens=120,
                completion_tokens=25,
                total_tokens=145,
                usage_available=True,
                cache_read_tokens=90,
                cache_write_tokens=15,
            ),
            latency_ms=321,
        )
        mgr.finish_turn(
            status="completed",
            final_content=None,
            failure_category=None,
            soft_limit_exceeded=False,
            turn_messages=[],
            checkpoint_messages=[],
        )

        request_path = sessions_dir / sid / "requests.jsonl"
        request = SessionLLMRequestRecord.model_validate_json(
            request_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert request.client_label == "brain"
        assert request.provider == "claude_code"
        assert request.model == "claude-sonnet-4-6"
        assert request.tools is not None
        assert request.tools[0].name == "schedule_action"

        response_path = sessions_dir / sid / "responses.jsonl"
        response = SessionLLMResponseRecord.model_validate_json(
            response_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert response.response is not None
        assert response.response.cache_read_tokens == 90
        assert response.response.cache_write_tokens == 15

        turn_path = sessions_dir / sid / "turns.jsonl"
        turn = SessionTurnRecord.model_validate_json(
            turn_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert turn.llm_rounds == 1
        assert turn.cache_read_tokens == 90
        assert turn.cache_write_tokens == 15

    def test_response_record_names_the_failover_candidate_that_served(
        self,
        mgr: SessionManager,
        sessions_dir: Path,
    ):
        sid = mgr.create("alice", "Alice")
        mgr.start_turn(
            channel="cli",
            sender="alice",
            inbound_kind="user_message",
            input_text="hi",
            input_timestamp=datetime.now(tz.utc),
            turn_metadata=None,
        )
        pending = mgr.begin_llm_request(
            client_label="brain",
            provider="kano_proxy",
            model="lincy-brain-agent",
            call_type="chat_with_tools",
            messages=[Message(role="user", content="hi")],
            tools=None,
            temperature=None,
        )
        assert pending is not None

        mgr.complete_llm_response(
            pending,
            response=LLMResponse(content="ok", tool_calls=[]),
            latency_ms=120,
            served=ServedCandidate(
                provider="heyroute",
                model="deepseek-v3",
                label="heyroute:deepseek-v3",
                index=1,
            ),
        )

        response_path = sessions_dir / sid / "responses.jsonl"
        response = SessionLLMResponseRecord.model_validate_json(
            response_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        # The intended profile stays untouched; the served fields are additive.
        assert response.provider == "kano_proxy"
        assert response.model == "lincy-brain-agent"
        assert response.served_provider == "heyroute"
        assert response.served_model == "deepseek-v3"
        assert response.served_candidate_index == 1

        event_path = sessions_dir / sid / "events.jsonl"
        events = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        response_event = next(e for e in events if e["kind"] == "llm_response")
        assert response_event["data"]["served_provider"] == "heyroute"
        assert response_event["data"]["served_candidate_index"] == 1

    def test_response_record_omits_served_fields_when_unknown(
        self,
        mgr: SessionManager,
        sessions_dir: Path,
    ):
        sid = mgr.create("alice", "Alice")
        pending = mgr.begin_llm_request(
            client_label="brain",
            provider="kano_proxy",
            model="lincy-brain-agent",
            call_type="chat",
            messages=[Message(role="user", content="hi")],
            tools=None,
            temperature=None,
        )
        assert pending is not None

        mgr.complete_llm_text_response(pending, response_text="ok", latency_ms=5)

        response_path = sessions_dir / sid / "responses.jsonl"
        response = SessionLLMResponseRecord.model_validate_json(
            response_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        assert response.served_provider is None
        assert response.served_model is None
        assert response.served_candidate_index is None
