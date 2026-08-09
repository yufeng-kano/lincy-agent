"""Tests for token-only soft limit compaction and overflow retry behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.llm.schema import ContextLengthExceededError, LLMResponse


def _seed_turns(conv: Conversation, count: int) -> None:
    for i in range(count):
        conv.add("user", f"user-{i}")
        conv.add("assistant", f"assistant-{i}")


def _make_core(tmp_path, *, provider: str, preserve_turns: int = 2, soft_limit: int = 128_000):
    from lincy.agent.core import AgentCore, _LatestTokenStatus, _TurnTokenUsage
    from lincy.agent.turn_context import TurnContext

    core = AgentCore.__new__(AgentCore)
    core.client = MagicMock()
    core.sync_client = None
    core.conversation = Conversation()
    core.builder = ContextBuilder(system_prompt="sys", preserve_turns=preserve_turns)
    core.registry = MagicMock()
    core.registry.get_definitions.return_value = []
    core.ui_sink = MagicMock()
    core.console = MagicMock()
    core.console.debug = False
    core.workspace = MagicMock()
    core.config = SimpleNamespace(
        app=SimpleNamespace(timezone="UTC+8"),
        context=SimpleNamespace(
            common_ground=SimpleNamespace(enabled=False),
            preserve_turns=preserve_turns,
            soft_max_prompt_tokens=soft_limit,
        ),
        tools=SimpleNamespace(
            max_tool_iterations=3,
            memory_edit=SimpleNamespace(turn_retry_limit=1),
            memory_sync=SimpleNamespace(every_n_turns=None, max_retries=1),
        ),
        maintenance=SimpleNamespace(archive=SimpleNamespace()),
        agents={"brain": SimpleNamespace(llm=SimpleNamespace(provider=provider))},
    )
    core.agent_os_dir = tmp_path
    core.user_id = "user"
    core.session_mgr = MagicMock()
    core.display_name = "User"
    core.memory_edit_allow_failure = False
    core.memory_backup_mgr = None
    core._queue = None
    core.turn_context = TurnContext()
    core.turn_cancel = None
    core.shared_state_store = None
    core.scope_resolver = None
    core.conversation_compaction_client = None
    core._maintenance_scheduler = None
    core._turns_since_memory_sync = 0
    core.adapters = {}
    core._brain_provider = provider
    core._soft_max_prompt_tokens = soft_limit
    core._latest_token_status = _LatestTokenStatus()
    core._turn_token_usage = _TurnTokenUsage()
    return core


def test_soft_limit_compacts_to_preserve_turns(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", preserve_turns=2, soft_limit=128_000)
    _seed_turns(core.conversation, 4)

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=140_000,
            completion_tokens=80,
            total_tokens=140_080,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn("new message", output_fn=lambda _text: None, channel="cli", sender="tester")

    user_count = sum(1 for m in core.conversation.get_messages() if m.role == "user")
    assert user_count == 2
    assert core.session_mgr.rewrite_messages.called
    assert "soft-over" in core.get_token_status_text()


def test_copilot_missing_usage_shows_unavailable_and_skips_compaction(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="copilot", preserve_turns=2, soft_limit=128_000)
    _seed_turns(core.conversation, 3)

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(content="ok", tool_calls=[], usage_available=False)
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn("copilot turn", output_fn=lambda _text: None, channel="cli", sender="tester")

    user_count = sum(1 for m in core.conversation.get_messages() if m.role == "user")
    assert user_count > 2
    assert core.get_token_status_text() == "tok unavailable/128,000 (copilot no usage)"


def test_soft_limit_uses_remote_codex_compaction_when_injected(monkeypatch, tmp_path):
    from lincy.agent import core as core_module
    from lincy.llm.schema import Message

    core = _make_core(tmp_path, provider="codex", preserve_turns=2, soft_limit=128_000)
    _seed_turns(core.conversation, 4)

    class _CompactionClient:
        def compact_messages(self, messages, tools=None):
            assert messages
            assert tools == []
            return [
                Message(
                    role="assistant",
                    content="[Codex compaction checkpoint]",
                    codex_compaction_encrypted_content="enc_123",
                )
            ]

    core.conversation_compaction_client = _CompactionClient()

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=140_000,
            completion_tokens=80,
            total_tokens=140_080,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn("new message", output_fn=lambda _text: None, channel="cli", sender="tester")

    messages = core.conversation.get_messages()
    assert len(messages) == 1
    assert messages[0].codex_compaction_encrypted_content == "enc_123"
    assert messages[0].metadata == {"rendered_static": True}
    soft_limit_warnings = [
        call.args[0]
        for call in core.console.print_warning.call_args_list
        if call.args
    ]
    assert any("via codex remote" in message for message in soft_limit_warnings)


def test_token_status_text_includes_cache_breakdown(tmp_path):
    core = _make_core(tmp_path, provider="claude_code", soft_limit=128_000)

    core._record_brain_response_usage(
        LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=3_200,
            completion_tokens=80,
            total_tokens=3_280,
            usage_available=True,
            cache_read_tokens=2_048,
            cache_write_tokens=32,
        )
    )
    core._finalize_turn_token_status()

    assert (
        core.get_token_status_text()
        == "tok 3,200/128,000 (2.5%) cache r2,048/3,200 (64.0%) w32"
    )


def test_token_status_text_shows_zero_cache_rate_on_miss(tmp_path):
    core = _make_core(tmp_path, provider="codex", soft_limit=128_000)

    core._record_brain_response_usage(
        LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=63_289,
            completion_tokens=80,
            total_tokens=63_369,
            usage_available=True,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    )
    core._finalize_turn_token_status()

    assert (
        core.get_token_status_text()
        == "tok 63,289/128,000 (49.4%) cache r0/63,289 (0.0%)"
    )


def test_ollama_token_status_text_shows_cache_unavailable_and_skips_warning(tmp_path):
    core = _make_core(tmp_path, provider="ollama", soft_limit=128_000)

    for _ in range(2):
        core._reset_turn_token_usage()
        core._record_brain_response_usage(
            LLMResponse(
                content="ok",
                tool_calls=[],
                prompt_tokens=63_289,
                completion_tokens=80,
                total_tokens=63_369,
                usage_available=True,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
        )
        core._finalize_turn_token_status()

    assert (
        core.get_token_status_text()
        == "tok 63,289/128,000 (49.4%) cache unavailable"
    )
    warning_messages = [
        call.args[0]
        for call in core.console.print_warning.call_args_list
        if call.args
    ]
    assert not any("Low cache hit rate" in message for message in warning_messages)


def test_token_status_text_keeps_best_cache_read_within_turn(tmp_path):
    core = _make_core(tmp_path, provider="codex", soft_limit=128_000)

    core._record_brain_response_usage(
        LLMResponse(
            content=None,
            tool_calls=[],
            prompt_tokens=62_236,
            completion_tokens=10,
            total_tokens=62_246,
            usage_available=True,
            cache_read_tokens=0,
        )
    )
    core._record_brain_response_usage(
        LLMResponse(
            content=None,
            tool_calls=[],
            prompt_tokens=63_233,
            completion_tokens=12,
            total_tokens=63_245,
            usage_available=True,
            cache_read_tokens=61_824,
        )
    )
    core._record_brain_response_usage(
        LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=63_289,
            completion_tokens=80,
            total_tokens=63_369,
            usage_available=True,
            cache_read_tokens=0,
        )
    )
    core._finalize_turn_token_status()

    assert (
        core.get_token_status_text()
        == "tok 63,289/128,000 (49.4%) cache r61,824/63,233 (97.8%)"
    )


def test_context_length_overflow_retries_once_with_emergency_compaction(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(
        tmp_path,
        provider="openrouter",
        preserve_turns=2,
        soft_limit=128_000,
    )
    _seed_turns(core.conversation, 5)
    calls = {"count": 0}

    def _fake_run_brain_responder(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ContextLengthExceededError("context length exceeded")
        response = LLMResponse(
            content="recovered",
            tool_calls=[],
            prompt_tokens=1000,
            completion_tokens=20,
            total_tokens=1020,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn("retry turn", output_fn=lambda _text: None, channel="cli", sender="tester")

    assert calls["count"] == 2
    user_count = sum(1 for m in core.conversation.get_messages() if m.role == "user")
    assert user_count <= 3
    assert core.session_mgr.rewrite_messages.called


def test_context_length_overflow_retry_preserves_original_timestamp(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(
        tmp_path,
        provider="openrouter",
        preserve_turns=2,
        soft_limit=128_000,
    )
    _seed_turns(core.conversation, 5)
    calls = {"count": 0}
    original_timestamp = datetime(2024, 1, 2, 3, 4, 5)

    def _fake_run_brain_responder(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ContextLengthExceededError("context length exceeded")
        response = LLMResponse(
            content="recovered",
            tool_calls=[],
            prompt_tokens=1000,
            completion_tokens=20,
            total_tokens=1020,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn(
        "retry turn",
        output_fn=lambda _text: None,
        channel="cli",
        sender="tester",
        timestamp=original_timestamp,
    )

    retry_user = next(
        entry
        for entry in reversed(core.conversation.get_messages())
        if entry.role == "user" and entry.content == "retry turn"
    )
    assert calls["count"] == 2
    assert retry_user.timestamp == original_timestamp


def test_prepare_turn_attempt_reuses_common_ground_rev_for_turn_debug(tmp_path):
    core = _make_core(tmp_path, provider="openrouter")
    core.console.debug = True
    core.config.context.common_ground = SimpleNamespace(
        enabled=True,
        max_entries=10,
        max_chars=4000,
        max_entry_chars=1000,
    )
    core.shared_state_store = MagicMock()
    core.shared_state_store.get_current_rev.side_effect = [2, 3]

    core._prepare_turn_attempt(
        "debug me",
        channel="cli",
        sender="tester",
        timestamp=None,
        turn_metadata={"scope_id": "scope-1", "anchor_shared_rev": 2},
    )

    assert core.shared_state_store.get_current_rev.call_count == 1
    core.console.print_debug.assert_any_call(
        "common-ground-turn",
        "injected=False scope=scope-1 anchor=2 current=2",
    )


def test_memory_sync_reminder_uses_rollup_instruction():
    from lincy.agent.core import _build_memory_sync_reminder

    text = _build_memory_sync_reminder(
        ["memory/agent/recent.md"],
        turns_accumulated=5,
    )

    assert "[MEMORY SYNC - ROLLUP]" in text
    assert "not been updated for 5 turns" in text
    assert "EXACTLY ONE rollup entry" in text
    assert "[rollup 5 turns]" in text
    assert "DELTA ONLY" in text
    assert "Open loops only" in text
    assert "POINTER for durable homes" in text
    assert "FORBIDDEN boilerplate" in text


def test_memory_sync_side_channel_uses_brain_client(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter")
    core.config.tools.memory_sync.every_n_turns = 1
    core.sync_client = MagicMock(name="deprecated_sync_client")

    captured: dict[str, object] = {}

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=1000,
            completion_tokens=30,
            total_tokens=1030,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return ["memory/agent/recent.md"]

    def _fake_run_memory_sync_side_channel(client, *_args, **_kwargs):
        captured["client"] = client

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(
        core_module,
        "_run_memory_sync_side_channel",
        _fake_run_memory_sync_side_channel,
    )
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *args, **kwargs: None)

    core.run_turn("needs sync", output_fn=lambda _text: None, channel="cli", sender="tester")

    assert captured["client"] is core.client


def test_soft_limit_exceeded_forces_memory_sync(monkeypatch, tmp_path):
    """Memory sync forced when soft limit exceeded, even if counter < threshold."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.config.tools.memory_sync.every_n_turns = 5
    core._turns_since_memory_sync = 1  # below threshold (5)
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=140_000, completion_tokens=80,
            total_tokens=140_080, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return ["memory/agent/recent.md"]

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        sync_called["count"] += 1

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *a, **kw: None)

    core.run_turn("over limit", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 1
    assert core._turns_since_memory_sync == 0


def test_soft_limit_exceeded_no_sync_when_targets_met(monkeypatch, tmp_path):
    """No forced sync when targets were naturally updated, even if over soft limit."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.config.tools.memory_sync.every_n_turns = 5
    core._turns_since_memory_sync = 2
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=140_000, completion_tokens=80,
            total_tokens=140_080, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return []  # targets met

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        sync_called["count"] += 1

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *a, **kw: None)

    core.run_turn("over but met", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 0
    assert core._turns_since_memory_sync == 0


def test_below_soft_limit_uses_counter_only(monkeypatch, tmp_path):
    """Below soft limit: sync only fires when counter reaches threshold."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.config.tools.memory_sync.every_n_turns = 5
    core._turns_since_memory_sync = 2
    _seed_turns(core.conversation, 2)

    sync_called = {"count": 0}

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=50_000, completion_tokens=80,
            total_tokens=50_080, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return ["memory/agent/recent.md"]

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        sync_called["count"] += 1

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *a, **kw: None)

    core.run_turn("below limit", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 0
    assert core._turns_since_memory_sync == 3  # incremented, not at threshold


def test_counter_sync_failed_pre_compaction_retries(monkeypatch, tmp_path):
    """Pre-compaction sync fires when counter sync triggered but failed."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.config.tools.memory_sync.every_n_turns = 5
    core._turns_since_memory_sync = 4  # will hit threshold (5) after increment
    _seed_turns(core.conversation, 4)

    sync_calls: list[str] = []

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=140_000, completion_tokens=80,
            total_tokens=140_080, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return ["memory/agent/recent.md"]

    call_count = {"n": 0}

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("LLM error")  # counter sync fails
        sync_calls.append("pre-compaction")  # pre-compaction retries

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *a, **kw: None)

    core.run_turn("sync fail retry", output_fn=lambda _: None, channel="cli", sender="tester")

    assert call_count["n"] == 2  # counter sync (fail) + pre-compaction (retry)
    assert core._turns_since_memory_sync == 0  # reset by pre-compaction success


def test_heartbeat_soft_over_no_accumulated_skips_sync(monkeypatch, tmp_path):
    """Heartbeat with soft-over but no accumulated turns does NOT sync."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.config.tools.memory_sync.every_n_turns = 5
    core._turns_since_memory_sync = 0  # nothing accumulated
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_brain_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=140_000, completion_tokens=80,
            total_tokens=140_080, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    def _fake_find_missing(_turn_messages):
        return ["memory/agent/recent.md"]

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        sync_called["count"] += 1

    monkeypatch.setattr(core_module, "_run_brain_responder", _fake_run_brain_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)
    monkeypatch.setattr(core_module, "_run_memory_archive", lambda *a, **kw: None)

    # Simulate heartbeat turn: set metadata so is_system_heartbeat is True
    core.turn_context.set_inbound("system", "system", {"system": True})
    core.run_turn(
        "[HEARTBEAT]\nCheck memory.",
        output_fn=lambda _: None,
        channel="system", sender="system",
    )

    assert sync_called["count"] == 0  # no sync: counter=0
