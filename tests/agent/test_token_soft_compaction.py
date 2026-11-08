"""Tests for token-only soft limit compaction and overflow retry behavior."""

from __future__ import annotations

from datetime import datetime
import threading
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
    from lincy.agent.core import AgentCore
    from lincy.agent.token_telemetry import LatestTokenStatus, TurnTokenUsage
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
            memory_edit=SimpleNamespace(turn_retry_limit=1, temp_memory_max_chars=6000),
            max_preempts_per_turn=3,
            memory_sync=SimpleNamespace(max_retries=1),
        ),
        maintenance=SimpleNamespace(),
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
    core._maintenance_scheduler = None
    core._busy = threading.Event()
    core._turns_since_memory_sync = 0
    core.adapters = {}
    core._brain_provider = provider
    core._soft_max_prompt_tokens = soft_limit
    core._latest_token_status = LatestTokenStatus()
    core._turn_token_usage = TurnTokenUsage()
    return core


def test_soft_limit_compacts_to_preserve_turns(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", preserve_turns=2, soft_limit=128_000)
    _seed_turns(core.conversation, 4)

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)

    core.run_turn("new message", output_fn=lambda _text: None, channel="cli", sender="tester")

    user_count = sum(1 for m in core.conversation.get_messages() if m.role == "user")
    assert user_count == 2
    assert core.session_mgr.rewrite_messages.called
    assert "soft-over" in core.get_token_status_text()


def test_token_status_text_includes_cache_breakdown(tmp_path):
    core = _make_core(tmp_path, provider="kano_proxy", soft_limit=128_000)

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


def test_finalize_turn_token_status_emits_ctx_status_event(tmp_path):
    from lincy.agent.ui_event_console import UiEventConsole
    from lincy.ui.events import CtxStatusEvent

    core = _make_core(tmp_path, provider="kano_proxy", soft_limit=128_000)
    emitted: list[object] = []
    core.console = UiEventConsole(SimpleNamespace(emit=emitted.append))

    core._record_brain_response_usage(
        LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=3_200,
            completion_tokens=80,
            total_tokens=3_280,
            usage_available=True,
        )
    )
    core._finalize_turn_token_status()

    assert len(emitted) == 1
    assert isinstance(emitted[0], CtxStatusEvent)
    assert emitted[0].text == core.get_token_status_text()
    assert emitted[0].text.startswith("tok 3,200/128,000")


def test_token_status_text_shows_zero_cache_rate_on_miss(tmp_path):
    core = _make_core(tmp_path, provider="openai", soft_limit=128_000)

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
    core = _make_core(tmp_path, provider="openai", soft_limit=128_000)

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

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)

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

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)

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


def test_memory_sync_reminder_asks_for_state_refresh():
    from lincy.agent.turn_runtime import _build_memory_sync_reminder

    text = _build_memory_sync_reminder(
        ["memory/agent/temp-memory.md"],
        turns_accumulated=5,
        max_chars=4321,
    )

    assert "[MEMORY SYNC - STATE REFRESH]" in text
    assert "not been refreshed for 5 user turns" in text
    assert "ONE memory_edit call" in text
    assert "# 今日現況" in text
    assert "## 下一步" in text
    assert "Stay under 4321 characters" in text
    assert "Do not add dated log entries" in text
    assert "rollup" not in text.lower()


def test_memory_sync_side_channel_uses_brain_client(monkeypatch, tmp_path):
    """The pre-compaction sync runs on the brain client, never a side client."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core.sync_client = MagicMock(name="deprecated_sync_client")
    core._turns_since_memory_sync = 1
    _seed_turns(core.conversation, 4)

    captured: dict[str, object] = {}

    def _fake_run_responder(**kwargs):
        response = LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=140_000,
            completion_tokens=30,
            total_tokens=140_030,
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(
        core_module,
        "_run_memory_sync_side_channel",
        _fake_run_memory_sync_side_channel,
    )

    core.run_turn("needs sync", output_fn=lambda _text: None, channel="cli", sender="tester")

    assert captured["client"] is core.client


def test_soft_limit_exceeded_forces_memory_sync(monkeypatch, tmp_path):
    """Any state-file debt is settled before soft compaction drops history."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core._turns_since_memory_sync = 1
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)

    core.run_turn("over limit", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 1
    assert core._turns_since_memory_sync == 0


def test_soft_limit_exceeded_no_sync_when_targets_met(monkeypatch, tmp_path):
    """No forced sync when targets were naturally updated, even if over soft limit."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core._turns_since_memory_sync = 2
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)

    core.run_turn("over but met", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 0
    assert core._turns_since_memory_sync == 0


def test_below_soft_limit_only_tracks_state_debt(monkeypatch, tmp_path):
    """Below the soft limit a user turn without a state-file write only adds debt."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core._turns_since_memory_sync = 2
    _seed_turns(core.conversation, 2)

    sync_called = {"count": 0}

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)

    core.run_turn("below limit", output_fn=lambda _: None, channel="cli", sender="tester")

    assert sync_called["count"] == 0
    assert core._turns_since_memory_sync == 3  # debt grows; no LLM call


def test_pre_compaction_sync_failure_keeps_debt(monkeypatch, tmp_path):
    """A failed pre-compaction sync leaves the debt for the next forced sync."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core._turns_since_memory_sync = 4
    _seed_turns(core.conversation, 4)

    def _fake_run_responder(**kwargs):
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
        return ["memory/agent/temp-memory.md"]

    call_count = {"n": 0}

    def _fake_run_memory_sync_side_channel(*_args, **_kwargs):
        call_count["n"] += 1
        raise RuntimeError("LLM error")

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)

    core.run_turn("sync fail", output_fn=lambda _: None, channel="cli", sender="tester")

    assert call_count["n"] == 1  # only the pre-compaction sync; no per-turn sync exists
    assert core._turns_since_memory_sync == 5  # this turn's debt plus the unpaid 4


def test_internal_turns_never_add_state_debt(monkeypatch, tmp_path):
    """Heartbeat, scheduled and task-due turns do not count as unpaid user turns."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=128_000)
    core._turns_since_memory_sync = 2

    def _fake_run_responder(**kwargs):
        response = LLMResponse(
            content="ok", tool_calls=[],
            prompt_tokens=1_000, completion_tokens=10,
            total_tokens=1_010, usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(
        core_module, "find_missing_memory_sync_targets",
        lambda _m: ["memory/agent/temp-memory.md"],
    )

    for metadata in ({"system": True}, {"scheduled_reason": "follow up"}, {"task_due": True}):
        core.turn_context.set_inbound("system", "system", metadata)
        core.run_turn(
            "[HEARTBEAT]", output_fn=lambda _: None,
            channel="system", sender="system", turn_metadata=metadata,
        )
    assert core._turns_since_memory_sync == 2

    core.turn_context.set_inbound("cli", "tester", {})
    core.run_turn("a real message", output_fn=lambda _: None, channel="cli", sender="tester")
    assert core._turns_since_memory_sync == 3


def test_heartbeat_soft_over_no_accumulated_skips_sync(monkeypatch, tmp_path):
    """Heartbeat with soft-over but no accumulated turns does NOT sync."""
    from lincy.agent import core as core_module

    core = _make_core(tmp_path, provider="openrouter", soft_limit=96_000)
    core._turns_since_memory_sync = 0  # nothing accumulated
    _seed_turns(core.conversation, 4)

    sync_called = {"count": 0}

    def _fake_run_responder(**kwargs):
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

    monkeypatch.setattr(core_module, "_run_responder", _fake_run_responder)
    monkeypatch.setattr(core_module, "find_missing_memory_sync_targets", _fake_find_missing)
    monkeypatch.setattr(core_module, "_run_memory_sync_side_channel", _fake_run_memory_sync_side_channel)

    # Simulate heartbeat turn: set metadata so is_system_heartbeat is True
    core.turn_context.set_inbound("system", "system", {"system": True})
    core.run_turn(
        "[HEARTBEAT]\nCheck memory.",
        output_fn=lambda _: None,
        channel="system", sender="system",
    )

    assert sync_called["count"] == 0  # no sync: counter=0
    assert core._turns_since_memory_sync == 0  # internal turns never add debt


def _core_with_boot_file(tmp_path, *, soft_limit: int):
    core = _make_core(tmp_path, provider="openrouter", preserve_turns=2, soft_limit=soft_limit)
    boot_file = tmp_path / "memory" / "agent" / "temp-memory.md"
    boot_file.parent.mkdir(parents=True)
    boot_file.write_text("- [2024-01-01 09:00] morning note\n", encoding="utf-8")
    core.builder = ContextBuilder(
        system_prompt="sys",
        agent_os_dir=tmp_path,
        boot_files_as_tool=["memory/agent/temp-memory.md"],
        preserve_turns=2,
    )
    core.builder.reload_boot_files()
    _seed_turns(core.conversation, 4)
    return core, boot_file


def _responder_that_appends_note(boot_file, *, prompt_tokens: int):
    def _fake_run_responder(**kwargs):
        # The brain appends to temp-memory during the turn; the in-memory boot
        # snapshot keeps showing the stale content until something reloads it.
        boot_file.write_text(
            "- [2024-01-01 09:00] morning note\n- [2024-01-01 21:00] evening note\n",
            encoding="utf-8",
        )
        response = LLMResponse(
            content="ok",
            tool_calls=[],
            prompt_tokens=prompt_tokens,
            completion_tokens=80,
            total_tokens=prompt_tokens + 80,
            usage_available=True,
        )
        cb = kwargs.get("on_model_response")
        if cb is not None:
            cb(response)
        return response

    return _fake_run_responder


def _boot_tool_text(core) -> str:
    return "\n".join(content for _path, content in core.builder._tool_boot_segments)


def test_soft_limit_compaction_reloads_boot_files(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core, boot_file = _core_with_boot_file(tmp_path, soft_limit=128_000)
    monkeypatch.setattr(
        core_module, "_run_responder", _responder_that_appends_note(boot_file, prompt_tokens=140_000)
    )

    core.run_turn("new message", output_fn=lambda _text: None, channel="cli", sender="tester")

    assert core.session_mgr.rewrite_messages.called
    assert "evening note" in _boot_tool_text(core)


def test_below_soft_limit_keeps_boot_snapshot(monkeypatch, tmp_path):
    from lincy.agent import core as core_module

    core, boot_file = _core_with_boot_file(tmp_path, soft_limit=128_000)
    monkeypatch.setattr(
        core_module, "_run_responder", _responder_that_appends_note(boot_file, prompt_tokens=50_000)
    )

    core.run_turn("new message", output_fn=lambda _text: None, channel="cli", sender="tester")

    assert not core.session_mgr.rewrite_messages.called
    assert "evening note" not in _boot_tool_text(core)
