from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from lincy.agent.core import _run_brain_responder
from lincy.agent.turn_context import TurnContext
from lincy.agent.turn_overlay import (
    DECISION_REMINDER_LABEL,
    _DECISION_REMINDER_TEMPLATE,
    build_decision_reminder_block,
)
from lincy.agent.staged_planning import (
    STAGE1_SYNTHETIC_TOOL_NAME,
    Stage1GatheringResult,
    Stage2PlanningResult,
    _scrub_stage1_messages,
    build_stage1_tools,
    run_stage1_information_gathering,
    run_stage2_brain_planning,
)
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.core.schema import StagedPlanningConfig
from lincy.llm.schema import (
    ContentPart,
    LLMResponse,
    Message,
    ToolCall,
    ToolDefinition,
    ToolParameter,
)
from lincy.tools.registry import ToolResult
from lincy.tools.builtin.schedule_action import SCHEDULE_ACTION_DEFINITION


def _fake_console():
    console = MagicMock()
    console.spinner.side_effect = lambda *a, **k: nullcontext()
    console.debug = False
    console.show_tool_use = False
    return console


def _fake_config(*, enabled: bool, plan_context_files: list[str] | None = None):
    return SimpleNamespace(
        agents={
            "brain": SimpleNamespace(
                staged_planning=StagedPlanningConfig(
                    enabled=enabled,
                    plan_context_files=plan_context_files or [],
                ),
            ),
        },
        features=SimpleNamespace(
            send_message_batch_guidance=SimpleNamespace(enabled=True),
        ),
    )


def _dummy_plan_text() -> str:
    return (
        "Decision: reply briefly.\n"
        "Facts: user sounds sleepy.\n"
        "Actions: send_message once.\n"
        "Rules: keep it short."
    )


def _conversation_cache_breakpoint(messages: list[Message]) -> Message:
    for message in messages:
        if message.role == "system" or not isinstance(message.content, list):
            continue
        first = message.content[0]
        if (
            isinstance(first, ContentPart)
            and first.cache_control == {"type": "ephemeral", "ttl": "1h"}
        ):
            return message
    raise AssertionError("conversation cache breakpoint not found")


def _read_file_tool() -> ToolDefinition:
    return ToolDefinition(
        name="read_file",
        description="read",
        parameters={"path": ToolParameter(type="string", description="path")},
        required=["path"],
    )


def _read_image_tool() -> ToolDefinition:
    return ToolDefinition(
        name="read_image",
        description="read image",
        parameters={"path": ToolParameter(type="string", description="path")},
        required=["path"],
    )


def _read_image_by_subagent_tool() -> ToolDefinition:
    return ToolDefinition(
        name="read_image_by_subagent",
        description="read image by subagent",
        parameters={
            "path": ToolParameter(type="string", description="path"),
            "context": ToolParameter(type="string", description="context"),
        },
        required=["path", "context"],
    )


def _web_search_tool() -> ToolDefinition:
    return ToolDefinition(
        name="web_search",
        description="search",
        parameters={"query": ToolParameter(type="string", description="query")},
        required=["query"],
    )


def _web_fetch_tool() -> ToolDefinition:
    return ToolDefinition(
        name="web_fetch",
        description="fetch",
        parameters={"url": ToolParameter(type="string", description="url")},
        required=["url"],
    )


def _send_message_tool() -> ToolDefinition:
    return ToolDefinition(
        name="send_message",
        description="send",
        parameters={"body": ToolParameter(type="string", description="body")},
        required=["body"],
    )


def test_run_brain_responder_feature_disabled_uses_legacy(monkeypatch):
    console = _fake_console()
    legacy_response = LLMResponse(content="ok", tool_calls=[])
    calls: list[dict] = []

    def _legacy(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return legacy_response

    monkeypatch.setattr("lincy.agent.core._run_responder", _legacy)
    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: (_ for _ in ()).throw(AssertionError("stage1 should not run")),
    )

    result = _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys")],
        tools=[],
        conversation=Conversation(),
        builder=MagicMock(),
        registry=MagicMock(),
        console=console,
        config=_fake_config(enabled=False),
        channel="cli",
        sender=None,
    )

    assert result is legacy_response
    assert len(calls) == 1


def test_run_brain_responder_staged_persists_findings_and_shows_plan(monkeypatch):
    console = _fake_console()
    convo = Conversation()
    legacy_response = LLMResponse(content=None, tool_calls=[])
    captured: dict = {}

    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: Stage1GatheringResult(
            transcript="[tool_call] read_file {}",
            findings_text="facts",
            tool_calls=1,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
    )
    monkeypatch.setattr(
        "lincy.agent.core.run_stage2_brain_planning",
        lambda **_: Stage2PlanningResult(
            plan_text=_dummy_plan_text(),
            raw_response=_dummy_plan_text(),
        ),
    )

    def _legacy(*args, **kwargs):
        captured["kwargs"] = kwargs
        return legacy_response

    monkeypatch.setattr("lincy.agent.core._run_responder", _legacy)

    result = _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        tools=[
            ToolDefinition(
                name="send_message",
                description="send",
                parameters={"body": ToolParameter(type="string", description="body")},
                required=["body"],
            )
        ],
        conversation=convo,
        builder=MagicMock(),
        registry=MagicMock(),
        console=console,
        config=_fake_config(enabled=True),
        channel="discord",
        sender="alice",
    )

    assert result is legacy_response

    # Stage 1 findings persisted in conversation
    msgs = convo.get_messages()
    assert len(msgs) == 2
    assert msgs[0].role == "assistant"
    assert msgs[1].role == "tool"
    assert msgs[1].name == STAGE1_SYNTHETIC_TOOL_NAME
    assert "facts" in msgs[1].content

    # Plan shown in TUI
    console.print_inner_thoughts.assert_called()
    _, _, shown_text = console.print_inner_thoughts.call_args.args
    assert shown_text.startswith("[PLAN][Stage2]\n")

    # Stage 3 overlay includes both findings and plan
    overlay = captured["kwargs"]["message_overlay"]
    overlaid = overlay([Message(role="system", content="sys")])
    assert any(
        m.role == "system"
        and isinstance(m.content, str)
        and "Stage 1 findings" in m.content
        for m in overlaid
    )
    assert any(
        m.role == "system"
        and isinstance(m.content, str)
        and "Stage 3/3" in m.content
        for m in overlaid
    )


def test_run_brain_responder_staged_advances_breakpoint_before_stage1_and_stage2(monkeypatch):
    console = _fake_console()
    convo = Conversation()
    convo.add("user", "u1")
    convo.add("assistant", "a1")
    convo.add("user", "u2")
    builder = ContextBuilder(system_prompt="sys", cache_ttl="1h")
    messages = builder.build(convo)
    captured: dict[str, list[Message]] = {}

    def _stage1(**kwargs):
        captured["stage1"] = kwargs["messages"]
        return Stage1GatheringResult(
            transcript="stage1",
            findings_text="facts",
            tool_calls=0,
            final_response=LLMResponse(content=None, tool_calls=[]),
        )

    def _stage2(**kwargs):
        captured["stage2"] = kwargs["messages"]
        return Stage2PlanningResult(
            plan_text=_dummy_plan_text(),
            raw_response=_dummy_plan_text(),
        )

    monkeypatch.setattr("lincy.agent.core.run_stage1_information_gathering", _stage1)
    monkeypatch.setattr("lincy.agent.core.run_stage2_brain_planning", _stage2)
    monkeypatch.setattr(
        "lincy.agent.core._run_responder",
        lambda *args, **kwargs: LLMResponse(content="ok", tool_calls=[]),
    )

    _run_brain_responder(
        client=MagicMock(),
        messages=messages,
        tools=[],
        conversation=convo,
        builder=builder,
        registry=MagicMock(),
        console=console,
        config=_fake_config(enabled=True),
        channel="cli",
        sender=None,
    )

    stage1_breakpoint = _conversation_cache_breakpoint(captured["stage1"])
    stage2_breakpoint = _conversation_cache_breakpoint(captured["stage2"])

    assert stage1_breakpoint.role == "user"
    assert "u2" in stage1_breakpoint.content[0].text
    assert stage2_breakpoint.role == "user"
    assert "u2" in stage2_breakpoint.content[0].text


def test_run_brain_responder_plan_context_files_injected(monkeypatch, tmp_path):
    console = _fake_console()
    legacy_response = LLMResponse(content=None, tool_calls=[])
    captured: dict[str, object] = {}
    stage3_captured: dict[str, object] = {}

    long_term = tmp_path / "memory" / "agent" / "long-term.md"
    long_term.parent.mkdir(parents=True, exist_ok=True)
    long_term.write_text(
        "- keep caring naturally\n- do not assume missing facts\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: Stage1GatheringResult(
            transcript="stage1",
            findings_text="facts",
            tool_calls=1,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
    )

    def _stage2(**kwargs):
        captured["messages"] = kwargs["messages"]
        return Stage2PlanningResult(
            plan_text=_dummy_plan_text(),
            raw_response=_dummy_plan_text(),
        )

    monkeypatch.setattr("lincy.agent.core.run_stage2_brain_planning", _stage2)

    def _legacy(*args, **kwargs):
        stage3_captured["kwargs"] = kwargs
        return legacy_response

    monkeypatch.setattr("lincy.agent.core._run_responder", _legacy)

    builder = SimpleNamespace(agent_os_dir=tmp_path)

    result = _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        tools=[],
        conversation=Conversation(),
        builder=builder,
        registry=MagicMock(),
        console=console,
        config=_fake_config(
            enabled=True,
            plan_context_files=["memory/agent/long-term.md"],
        ),
        channel="discord",
        sender="alice",
    )

    assert result is legacy_response

    # plan_context_files injected into Stage 2
    stage2_messages = captured["messages"]
    assert isinstance(stage2_messages, list)
    ctx_msgs = [
        m for m in stage2_messages
        if m.role == "system"
        and isinstance(m.content, str)
        and "Planning context" in m.content
    ]
    assert len(ctx_msgs) == 1
    assert '<file path="memory/agent/long-term.md">' in ctx_msgs[0].content
    assert "keep caring naturally" in ctx_msgs[0].content

    # plan_context_files also injected into Stage 3 overlay
    overlay = stage3_captured["kwargs"]["message_overlay"]
    overlaid = overlay([Message(role="system", content="sys")])
    assert any(
        m.role == "system"
        and isinstance(m.content, str)
        and "Planning context" in m.content
        and "keep caring naturally" in m.content
        for m in overlaid
    )


def test_run_brain_responder_passes_skill_registry_and_turn_context(monkeypatch):
    console = _fake_console()
    convo = Conversation()
    captured: dict[str, object] = {}
    skill_registry = object()
    turn_context = TurnContext()

    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: Stage1GatheringResult(
            transcript="stage1",
            findings_text="facts",
            tool_calls=1,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
    )
    monkeypatch.setattr(
        "lincy.agent.core.run_stage2_brain_planning",
        lambda **_: Stage2PlanningResult(
            plan_text=_dummy_plan_text(),
            raw_response=_dummy_plan_text(),
        ),
    )

    def _legacy(*args, **kwargs):
        captured.update(kwargs)
        return LLMResponse(content=None, tool_calls=[])

    monkeypatch.setattr("lincy.agent.core._run_responder", _legacy)

    _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        tools=[],
        conversation=convo,
        builder=MagicMock(),
        registry=MagicMock(),
        console=console,
        config=_fake_config(enabled=True),
        channel="discord",
        sender="alice",
        skill_registry=skill_registry,
        turn_context=turn_context,
    )

    assert captured["skill_registry"] is skill_registry
    assert captured["turn_context"] is turn_context


def test_run_brain_responder_plan_context_file_missing_warns_and_continues(monkeypatch, tmp_path):
    console = _fake_console()
    legacy_response = LLMResponse(content=None, tool_calls=[])
    captured: dict[str, object] = {"stage2_called": False}

    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: Stage1GatheringResult(
            transcript="stage1",
            findings_text="facts",
            tool_calls=1,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
    )

    def _stage2(**kwargs):
        captured["stage2_called"] = True
        captured["messages"] = kwargs["messages"]
        return Stage2PlanningResult(
            plan_text=_dummy_plan_text(),
            raw_response=_dummy_plan_text(),
        )

    monkeypatch.setattr("lincy.agent.core.run_stage2_brain_planning", _stage2)
    monkeypatch.setattr(
        "lincy.agent.core._run_responder",
        lambda *args, **kwargs: legacy_response,
    )

    builder = SimpleNamespace(agent_os_dir=tmp_path)

    result = _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        tools=[],
        conversation=Conversation(),
        builder=builder,
        registry=MagicMock(),
        console=console,
        config=_fake_config(
            enabled=True,
            plan_context_files=["memory/agent/long-term.md"],
        ),
        channel="discord",
        sender="alice",
    )

    assert result is legacy_response
    assert captured["stage2_called"] is True
    stage2_messages = captured["messages"]
    assert isinstance(stage2_messages, list)
    assert not any(
        m.role == "system"
        and isinstance(m.content, str)
        and "Planning context" in m.content
        for m in stage2_messages
    )
    warning_texts = [str(call.args[0]) for call in console.print_warning.call_args_list]
    assert any("plan_context_files: skipping" in text for text in warning_texts)


def test_run_brain_responder_stage2_failure_falls_back(monkeypatch):
    console = _fake_console()
    legacy_response = LLMResponse(content="legacy", tool_calls=[])
    legacy_calls: list[dict] = []

    monkeypatch.setattr(
        "lincy.agent.core.run_stage1_information_gathering",
        lambda **_: Stage1GatheringResult(
            transcript="x",
            findings_text="x",
            tool_calls=0,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
    )
    monkeypatch.setattr("lincy.agent.core.run_stage2_brain_planning", lambda **_: None)

    def _legacy(*args, **kwargs):
        legacy_calls.append(kwargs)
        return legacy_response

    monkeypatch.setattr("lincy.agent.core._run_responder", _legacy)

    result = _run_brain_responder(
        client=MagicMock(),
        messages=[Message(role="system", content="sys")],
        tools=[],
        conversation=Conversation(),
        builder=MagicMock(),
        registry=MagicMock(),
        console=console,
        config=_fake_config(enabled=True),
        channel="cli",
        sender=None,
    )

    assert result is legacy_response
    assert len(legacy_calls) == 1
    warning_texts = [str(call.args[0]) for call in console.print_warning.call_args_list]
    assert any("Stage 2 planning failed" in text for text in warning_texts)


def test_stage1_schedule_action_is_list_only():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="s1",
                            name="schedule_action",
                            arguments={
                                "action": "batch_add",
                                "adds": [
                                    {
                                        "reason": "x",
                                        "trigger_spec": "2030-01-01T00:00",
                                    }
                                ],
                            },
                        )
                    ],
                )
            return LLMResponse(content="done", tool_calls=[])

    class _Registry:
        def __init__(self):
            self.execute_calls = 0

        def has_tool(self, name):
            return name == "schedule_action"

        def execute(self, tool_call):
            del tool_call
            self.execute_calls += 1
            return ToolResult("SHOULD_NOT_RUN")

    console = _fake_console()
    client = _Client()
    registry = _Registry()

    result = run_stage1_information_gathering(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[SCHEDULE_ACTION_DEFINITION],
        registry=registry,  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=2,
    )

    assert registry.execute_calls == 0
    assert "only supports action='list'" in result.transcript
    assert "[stage1-intent]" in result.transcript
    assert client.calls == 1


def test_stage1_requires_initial_memory_search_when_available():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="Context already sufficient; no additional lookup needed.",
                    tool_calls=[],
                )
            if self.calls == 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m1",
                            name="memory_search",
                            arguments={"query": "reminder take out trash"},
                        )
                    ],
                )
            return LLMResponse(content="done", tool_calls=[])

    class _Registry:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, tool_call):
            self.execute_calls += 1
            assert tool_call.name == "memory_search"
            return ToolResult("## memory/people/yufeng/schedule.md\n\n- [17:00] take out trash")

        def has_tool(self, name):
            return name == "memory_search"

    console = _fake_console()
    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="memory_search",
                description="search memory",
                parameters={"query": ToolParameter(type="string", description="query")},
                required=["query"],
            )
        ],
        registry=_Registry(),  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=3,
    )

    assert result.tool_calls == 1
    assert "[stage1-gate]" in result.findings_text


def test_stage1_retries_when_initial_memory_search_query_is_empty():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m1",
                            name="memory_search",
                            arguments={"query": "   "},
                        )
                    ],
                )
            if self.calls == 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m2",
                            name="memory_search",
                            arguments={"query": "reminder take out trash"},
                        )
                    ],
                )
            return LLMResponse(content="done", tool_calls=[])

    class _Registry:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, tool_call):
            self.execute_calls += 1
            assert tool_call.name == "memory_search"
            return ToolResult("ok")

        def has_tool(self, name):
            return name == "memory_search"

    console = _fake_console()
    registry = _Registry()
    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="memory_search",
                description="search memory",
                parameters={"query": ToolParameter(type="string", description="query")},
                required=["query"],
            )
        ],
        registry=registry,  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=3,
    )

    assert registry.execute_calls == 1
    assert "query must be non-empty" in result.findings_text


def test_stage1_blocks_duplicate_memory_search_results_within_same_gather():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m1",
                            name="memory_search",
                            arguments={"query": "Discord 通訊規則 訊息分段"},
                        )
                    ],
                )
            if self.calls == 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="m2",
                            name="memory_search",
                            arguments={"query": "Discord 分段 傳送 回覆"},
                        )
                    ],
                )
            return LLMResponse(content="done", tool_calls=[])

    class _Registry:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, tool_call):
            self.execute_calls += 1
            assert tool_call.name == "memory_search"
            return ToolResult(
                "## memory/agent/journal/recent/2026-02-24.md\n\n"
                "- Discord DM should prefer short segmented messages.\n"
            )

        def has_tool(self, name):
            return name == "memory_search"

    console = _fake_console()
    registry = _Registry()
    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="memory_search",
                description="search memory",
                parameters={"query": ToolParameter(type="string", description="query")},
                required=["query"],
            )
        ],
        registry=registry,  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=3,
    )

    assert registry.execute_calls == 2
    assert result.tool_calls == 2
    assert "same result as previous search, refine query or stop" in result.findings_text


def test_stage1_forbidden_tool_is_captured_as_intent_and_stops():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="I already know what to send.",
                    tool_calls=[
                        ToolCall(
                            id="s1",
                            name="send_message",
                            arguments={"channel": "discord", "body": "hi"},
                        )
                    ],
                )
            return LLMResponse(content="should not be called", tool_calls=[])

    class _Registry:
        def __init__(self):
            self.execute_calls = 0

        def execute(self, tool_call):
            del tool_call
            self.execute_calls += 1
            return ToolResult("SHOULD_NOT_RUN")

        def has_tool(self, name):
            return name == "read_file"

    client = _Client()
    registry = _Registry()
    result = run_stage1_information_gathering(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[_read_file_tool()],
        registry=registry,  # type: ignore[arg-type]
        console=_fake_console(),  # type: ignore[arg-type]
        max_iterations=3,
        skip_memory_search_gate=True,
    )

    assert client.calls == 1
    assert registry.execute_calls == 0
    assert result.tool_calls == 1
    assert "Stage 1 is read-only" in result.findings_text
    assert "[stage1-intent]" in result.findings_text
    assert "Attempted send_message" in result.findings_text


def test_scrub_stage1_messages_removes_action_oriented_reminders():
    reminder = ContextBuilder.channel_reminder_variants()[0]
    for candidate in ContextBuilder.channel_reminder_variants():
        if "discord-messaging" in candidate:
            reminder = candidate
            break
    memory = ContextBuilder._GENERAL_REMINDERS["memory"]
    decision = _DECISION_REMINDER_TEMPLATE.format(
        anchors="long-term.md",
    )
    messages = [
        Message(
            role="user",
            content=(
                "[2026-03-08 (Sun) 12:00] [discord, from alice] hello\n"
                f"{reminder}\n"
                f"{memory}\n\n"
                f"{DECISION_REMINDER_LABEL}\n{decision}"
            ),
        )
    ]

    scrubbed = _scrub_stage1_messages(messages)

    assert scrubbed[0].content == "[2026-03-08 (Sun) 12:00] [discord, from alice] hello"


def test_scrub_stage1_messages_leaves_non_user_messages_untouched():
    assistant_msg = Message(
        role="assistant",
        content="(Discord: read builtin skill discord-messaging before channel-specific formatting)",
    )

    scrubbed = _scrub_stage1_messages([assistant_msg])

    assert scrubbed[0] is assistant_msg


def test_scrub_stage1_messages_skips_multimodal_user_content():
    user_msg = Message(
        role="user",
        content=[ContentPart(type="text", text="hello")],
    )

    scrubbed = _scrub_stage1_messages([user_msg])

    assert scrubbed[0] is user_msg


def test_scrub_stage1_messages_strips_context_builder_output():
    builder = ContextBuilder(
        system_prompt="sys",
        format_reminders={"discord": True, "memory": True},
        decision_reminder={
            "enabled": True,
            "files": ["memory/agent/long-term.md"],
        },
    )
    conv = Conversation()
    conv.add("user", "hello", channel="discord", sender="alice")

    messages = builder.build(conv)
    # [Decision Reminder] is no longer injected by ContextBuilder.build();
    # simulate the responder's per-turn overlay (see agent/turn_overlay.py)
    # landing on the latest user message before Stage 1 sees it.
    decision_block = build_decision_reminder_block(
        enabled=builder.decision_reminder_enabled,
        anchor_files=builder.decision_reminder_files,
        core_values=builder.decision_reminder_core_values,
    )
    user_idx = next(i for i, m in enumerate(messages) if m.role == "user")
    messages[user_idx] = messages[user_idx].model_copy(
        update={"content": f"{messages[user_idx].content}\n\n{decision_block}"}
    )

    scrubbed = _scrub_stage1_messages(messages)
    user_msg = [m for m in scrubbed if m.role == "user"][0]

    assert "multiple messages -> call send_message" not in user_msg.content
    assert "(memory:" not in user_msg.content
    assert "[Decision Reminder]" not in user_msg.content
    assert "hello" in user_msg.content


def test_stage1_information_gathering_uses_scrubbed_messages():
    class _Client:
        def __init__(self):
            self.messages = None

        def chat_with_tools(self, messages, tools, temperature=None):
            del tools, temperature
            self.messages = messages
            return LLMResponse(content="done", tool_calls=[])

    builder = ContextBuilder(
        system_prompt="sys",
        format_reminders={"discord": True, "memory": True},
        decision_reminder={
            "enabled": True,
            "files": ["memory/agent/long-term.md"],
        },
    )
    conv = Conversation()
    conv.add("user", "hello", channel="discord", sender="alice")
    built_messages = builder.build(conv)
    # [Decision Reminder] is no longer injected by ContextBuilder.build();
    # simulate the responder's per-turn overlay (see agent/turn_overlay.py)
    # landing on the latest user message before Stage 1 sees it.
    decision_block = build_decision_reminder_block(
        enabled=builder.decision_reminder_enabled,
        anchor_files=builder.decision_reminder_files,
        core_values=builder.decision_reminder_core_values,
    )
    user_idx = next(i for i, m in enumerate(built_messages) if m.role == "user")
    built_messages[user_idx] = built_messages[user_idx].model_copy(
        update={"content": f"{built_messages[user_idx].content}\n\n{decision_block}"}
    )
    client = _Client()

    result = run_stage1_information_gathering(
        client=client,  # type: ignore[arg-type]
        messages=built_messages,
        all_tools=[_read_file_tool()],
        registry=MagicMock(),
        console=_fake_console(),  # type: ignore[arg-type]
        max_iterations=1,
        skip_memory_search_gate=True,
    )

    assert result.final_response.content == "done"
    stage1_user = [
        m for m in client.messages[:-1]
        if m.role == "user" and isinstance(m.content, str)
    ][0]
    assert "multiple messages -> call send_message" not in stage1_user.content
    assert "(memory:" not in stage1_user.content
    assert "[Decision Reminder]" not in stage1_user.content


def test_stage2_planning_prompt_can_disable_batch_guidance():
    class _Client:
        def __init__(self):
            self.messages = None

        def chat_with_tools(self, messages, tools, temperature=None):
            del tools, temperature
            self.messages = messages
            return LLMResponse(content="plan", tool_calls=[])

    client = _Client()
    result = run_stage2_brain_planning(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys")],
        stage1=Stage1GatheringResult(
            transcript="notes",
            findings_text="notes",
            tool_calls=0,
            final_response=LLMResponse(content=None, tool_calls=[]),
        ),
        all_tools=[_read_file_tool(), _web_search_tool(), _web_fetch_tool()],
        registry=MagicMock(),
        console=_fake_console(),  # type: ignore[arg-type]
        send_message_batch_guidance=False,
    )

    assert result is not None
    assert result.plan_text == "plan"
    user_prompt = client.messages[-1].content
    assert isinstance(user_prompt, str)
    assert "prefer fewer send_message calls" not in user_prompt
    assert "merge them into one send_message" not in user_prompt


def test_stage1_tool_loop_preserves_reasoning_roundtrip():
    class _Client:
        def __init__(self):
            self.calls = 0
            self.history: list[list[Message]] = []

        def chat_with_tools(self, messages, tools, temperature=None):
            del tools, temperature
            self.calls += 1
            self.history.append(list(messages))
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    reasoning_content="stage1 thinking text",
                    reasoning_details=[
                        {
                            "type": "reasoning.text",
                            "text": "step-1",
                            "signature": "sig-1",
                            "id": "r1",
                            "format": "plain",
                            "index": 0,
                        }
                    ],
                    tool_calls=[
                        ToolCall(
                            id="m1",
                            name="memory_search",
                            arguments={"query": "reminder take out trash"},
                        )
                    ],
                )
            return LLMResponse(content="done", tool_calls=[])

    class _Registry:
        def execute(self, tool_call):
            assert tool_call.name == "memory_search"
            return ToolResult("ok")

        def has_tool(self, name):
            return name == "memory_search"

    client = _Client()
    console = _fake_console()
    result = run_stage1_information_gathering(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="memory_search",
                description="search memory",
                parameters={"query": ToolParameter(type="string", description="query")},
                required=["query"],
            )
        ],
        registry=_Registry(),  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=2,
    )

    assert result.tool_calls == 1
    assert len(client.history) == 2
    second_call_msgs = client.history[1]
    assistant_tool_msgs = [
        m for m in second_call_msgs
        if m.role == "assistant" and m.tool_calls
    ]
    assert len(assistant_tool_msgs) == 1
    replayed = assistant_tool_msgs[0]
    assert replayed.reasoning_content == "stage1 thinking text"
    assert replayed.reasoning_details is not None
    assert replayed.reasoning_details[0]["signature"] == "sig-1"


def test_stage1_can_skip_tool_calls_when_memory_search_unavailable():
    class _Client:
        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            return LLMResponse(
                content="Context already sufficient; no additional lookup needed.",
                tool_calls=[],
            )

    class _Registry:
        def execute(self, tool_call):
            raise AssertionError(f"should not execute tool: {tool_call.name}")

        def has_tool(self, name):
            return name == "read_file"

    console = _fake_console()
    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="read_file",
                description="read",
                parameters={"path": ToolParameter(type="string", description="path")},
                required=["path"],
            )
        ],
        registry=_Registry(),  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=2,
    )

    assert result.tool_calls == 0
    assert "no additional lookup needed" in result.findings_text


def test_stage1_uses_full_tool_schema_for_cache_parity():
    class _Client:
        def __init__(self):
            self.tools_seen = None

        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, temperature
            self.tools_seen = [tool.name for tool in tools]
            return LLMResponse(content="enough", tool_calls=[])

    class _Registry:
        def execute(self, tool_call):
            raise AssertionError(f"should not execute tool: {tool_call.name}")

        def has_tool(self, name):
            return name in {"read_file", "send_message"}

    client = _Client()
    result = run_stage1_information_gathering(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[_read_file_tool(), _send_message_tool()],
        registry=_Registry(),  # type: ignore[arg-type]
        console=_fake_console(),  # type: ignore[arg-type]
        max_iterations=2,
        skip_memory_search_gate=True,
    )

    assert result.tool_calls == 0
    assert client.tools_seen == ["read_file", "send_message"]


def test_stage1_skips_memory_search_gate_when_prior_findings_exist():
    """When skip_memory_search_gate=True, Stage 1 can return without calling memory_search."""

    class _Client:
        def chat_with_tools(self, messages, tools, temperature=None):
            del messages, tools, temperature
            return LLMResponse(
                content="Prior findings are still relevant; no new search needed.",
                tool_calls=[],
            )

    class _Registry:
        def execute(self, tool_call):
            raise AssertionError(f"should not execute tool: {tool_call.name}")

        def has_tool(self, name):
            return name == "memory_search"

    console = _fake_console()
    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[
            ToolDefinition(
                name="memory_search",
                description="search memory",
                parameters={"query": ToolParameter(type="string", description="query")},
                required=["query"],
            )
        ],
        registry=_Registry(),  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        max_iterations=2,
        skip_memory_search_gate=True,
    )

    assert result.tool_calls == 0
    assert "[stage1-gate]" not in result.findings_text
    assert "no new search needed" in result.findings_text


def test_build_stage1_tools_includes_read_only_image_tools():
    tools = build_stage1_tools(
        [
            _read_file_tool(),
            _read_image_tool(),
            _read_image_by_subagent_tool(),
            SCHEDULE_ACTION_DEFINITION,
        ]
    )

    names = [tool.name for tool in tools]
    assert "read_file" in names
    assert "read_image" in names
    assert "read_image_by_subagent" in names
    assert "schedule_action" in names


def test_stage2_planning_accepts_plain_text():
    class _Client:
        def chat_with_tools(self, messages, tools):
            del messages, tools
            return LLMResponse(
                content="Decision: keep silent now.\nAction: do not send_message.",
                tool_calls=[],
            )

    console = _fake_console()
    stage1 = Stage1GatheringResult(
        transcript="x",
        findings_text="x",
        tool_calls=0,
        final_response=LLMResponse(content=None, tool_calls=[]),
    )

    result = run_stage2_brain_planning(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        stage1=stage1,
        all_tools=[_read_file_tool(), _web_search_tool(), _web_fetch_tool()],
        registry=MagicMock(),
        console=console,  # type: ignore[arg-type]
        send_message_batch_guidance=True,
    )

    assert result is not None
    assert result.plan_text == "Decision: keep silent now.\nAction: do not send_message."


def test_stage2_planning_can_use_read_only_tools_with_full_schema():
    class _Client:
        def __init__(self):
            self.calls = 0
            self.tools_seen: list[list[str]] = []

        def chat_with_tools(self, messages, tools):
            del messages
            self.calls += 1
            self.tools_seen.append([tool.name for tool in tools])
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="r1",
                            name="read_file",
                            arguments={"path": "memory/agent/context.md"},
                        )
                    ],
                )
            return LLMResponse(content="Plan text", tool_calls=[])

    class _Registry:
        def execute(self, tool_call):
            assert tool_call.name == "read_file"
            return ToolResult("<file path=\"memory/agent/context.md\">ctx</file>")

    client = _Client()
    stage1 = Stage1GatheringResult(
        transcript="x",
        findings_text="x",
        tool_calls=0,
        final_response=LLMResponse(content=None, tool_calls=[]),
    )
    all_tools = [
        _read_file_tool(),
        _web_search_tool(),
        _web_fetch_tool(),
        _send_message_tool(),
    ]

    result = run_stage2_brain_planning(
        client=client,  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        stage1=stage1,
        all_tools=all_tools,
        registry=_Registry(),  # type: ignore[arg-type]
        console=_fake_console(),  # type: ignore[arg-type]
        send_message_batch_guidance=True,
        max_iterations=2,
    )

    assert result is not None
    assert result.plan_text == "Plan text"
    assert client.calls == 2
    assert client.tools_seen[0] == [tool.name for tool in all_tools]
    assert "send_message" in client.tools_seen[0]


def test_stage2_planning_rejects_disallowed_tools_and_recovers():
    class _Client:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools):
            del messages, tools
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="s1",
                            name="send_message",
                            arguments={"body": "hi"},
                        )
                    ],
                )
            return LLMResponse(content="Plan text", tool_calls=[])

    class _Registry:
        def execute(self, tool_call):
            raise AssertionError(f"unexpected execution: {tool_call.name}")

    console = _fake_console()
    stage1 = Stage1GatheringResult(
        transcript="x",
        findings_text="x",
        tool_calls=0,
        final_response=LLMResponse(content=None, tool_calls=[]),
    )

    result = run_stage2_brain_planning(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        stage1=stage1,
        all_tools=[_read_file_tool(), _web_search_tool(), _web_fetch_tool(), _send_message_tool()],
        registry=_Registry(),  # type: ignore[arg-type]
        console=console,  # type: ignore[arg-type]
        send_message_batch_guidance=True,
        max_iterations=2,
    )

    assert result is not None
    assert result.plan_text == "Plan text"
    tool_result_texts = [str(call.args[1]) for call in console.print_tool_result.call_args_list]
    assert any("Stage 2 planning only allows read_file, web_search, and web_fetch" in text for text in tool_result_texts)


def test_stage1_gather_prompt_mentions_external_fact_verification():
    captured: dict[str, str] = {}

    class _Client:
        def chat_with_tools(self, messages, tools):
            del tools
            last = messages[-1]
            assert last.role == "user"
            assert isinstance(last.content, str)
            captured["prompt"] = last.content
            return LLMResponse(content="enough", tool_calls=[])

    result = run_stage1_information_gathering(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        all_tools=[_read_file_tool()],
        registry=MagicMock(),
        console=_fake_console(),  # type: ignore[arg-type]
    )

    assert result is not None
    prompt = captured["prompt"]
    assert "current external facts" in prompt
    assert "menu/item availability" in prompt
    assert "verify it now or drop the contradicted claim from findings" in prompt


def test_stage2_planning_prompt_includes_structured_sections():
    captured: dict[str, str] = {}

    class _Client:
        def chat_with_tools(self, messages, tools):
            del tools
            last = messages[-1]
            assert last.role == "user"
            assert isinstance(last.content, str)
            captured["prompt"] = last.content
            return LLMResponse(content="Plan text", tool_calls=[])

    console = _fake_console()
    stage1 = Stage1GatheringResult(
        transcript="stage1",
        findings_text="known facts",
        tool_calls=0,
        final_response=LLMResponse(content=None, tool_calls=[]),
    )

    result = run_stage2_brain_planning(
        client=_Client(),  # type: ignore[arg-type]
        messages=[Message(role="system", content="sys"), Message(role="user", content="hi")],
        stage1=stage1,
        all_tools=[_read_file_tool(), _web_search_tool(), _web_fetch_tool()],
        registry=MagicMock(),
        console=console,  # type: ignore[arg-type]
        send_message_batch_guidance=True,
    )

    assert result is not None
    prompt = captured["prompt"]
    assert "ULTRA THINK" in prompt
    assert "[CURRENT_STATE]" in prompt
    assert "[FILE_UPDATE_PLAN]" in prompt
    assert "coherent human conversation" in prompt
    assert "Separate confirmed facts from inferences" in prompt
    assert "Normalize any conflicting date/day/time claims into a single timeline" in prompt
    assert "prefer the latest explicit user correction in the current turn" in prompt
    assert "do not carry superseded facts into the plan" in prompt
    assert "current external reality" in prompt
    assert "add an explicit verification step before reuse" in prompt
    assert "use the latest user timestamp as 'now'" in prompt
    assert "check logical relationships across recent messages" in prompt
    assert "later pickup/meeting" in prompt
    assert "never confidently repeat the contradicted claim" in prompt
    assert "Stage 3 must verify first" in prompt
    assert "Do not expose internal clock math" in prompt
    assert "relative delay and an absolute time" in prompt
    assert "Message economy" in prompt
    assert "same immediate ask, reminder, or action" in prompt
    assert "only when the points are truly distinct" in prompt
    assert "never revive an earlier fact that was invalidated by a later correction" in prompt
    assert "Never target `memory/archive/` for live updates" in prompt
    assert "Durable user instructions, bans, agreements" in prompt
    assert "Current-turn context, temporary state, and recent emotional timeline" in prompt
    assert "Reusable tool/process lessons belong in `personal-skills/`" in prompt
    assert "Identity or relationship-boundary changes belong in `memory/agent/persona.md`" in prompt
