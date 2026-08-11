from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

from types import SimpleNamespace

from lincy.agent.core import _run_responder
from lincy.agent.responder import _run_brain_responder
from lincy.agent.skill_governance import (
    SKILL_PREREQUISITE_TOOL_NAME,
    SkillGovernanceRegistry,
    parse_skill_frontmatter,
)
from lincy.agent.turn_context import TurnContext
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.core.schema import GovernanceRule, SkillGovernanceConfig, ToolsConfig
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition, ToolParameter
from lincy.tools.registry import ToolResult

# -- governance config used by most tests --------------------------------

_DISCORD_GOVERNANCE = SkillGovernanceConfig(
    external_skills_dir=None,
    rules=[
        GovernanceRule(
            skill="discord-messaging",
            tool="send_message",
            when={"channel": "discord"},
            enforcement="require_context",
        ),
    ],
)


def _write_discord_skill(tmp_path: Path) -> Path:
    """Create a discord-messaging skill with SKILL.md frontmatter."""
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "discord-messaging"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: discord-messaging\n"
        'description: "Discord messaging guide"\n'
        "---\n\n"
        "discord guide body",
        encoding="utf-8",
    )
    return skill_dir / "SKILL.md"


def _console():
    console = MagicMock()
    console.spinner.side_effect = lambda *a, **k: nullcontext()
    console.debug = False
    console.show_tool_use = False
    return console


def _base_messages(conversation: Conversation, builder: ContextBuilder) -> list[Message]:
    return builder.build(conversation)


def _tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="send_message",
            description="send",
            parameters={
                "channel": ToolParameter(type="string", description="channel"),
                "body": ToolParameter(type="string", description="body"),
            },
            required=["channel", "body"],
        ),
        ToolDefinition(
            name="read_file",
            description="read",
            parameters={"path": ToolParameter(type="string", description="path")},
            required=["path"],
        ),
    ]


def _brain_config():
    return SimpleNamespace(
        agents={
            "brain": SimpleNamespace(
                staged_planning=SimpleNamespace(enabled=False),
            ),
        },
        tools=ToolsConfig(),
        features=SimpleNamespace(
            send_message_batch_guidance=SimpleNamespace(enabled=False),
        ),
    )


class _Client:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    def chat_with_tools(self, messages, tools, temperature=None):
        del tools, temperature
        self.calls.append(list(messages))
        if not self._responses:
            return LLMResponse(content="done", tool_calls=[])
        return self._responses.pop(0)


class _Registry:
    def __init__(self, results: dict[str, str]):
        self._results = dict(results)
        self.executed: list[str] = []

    def has_tool(self, name):
        return name in self._results

    def execute(self, tool_call):
        self.executed.append(tool_call.name)
        content = self._results[tool_call.name]
        is_error = isinstance(content, str) and content.startswith("Error")
        return ToolResult(content, is_error=is_error)


class _SkillCheckAgent:
    def __init__(self, selected: list[str]):
        self.selected = selected
        self.calls: list[dict[str, object]] = []

    def pick_skill_names(self, *, latest_user_input, skills, loaded_skill_names=None, max_skills=1):
        self.calls.append(
            {
                "latest_user_input": latest_user_input,
                "skills": list(skills),
                "loaded_skill_names": set(loaded_skill_names or set()),
                "max_skills": max_skills,
            }
        )
        return list(self.selected)


# -- frontmatter parser tests -------------------------------------------


def test_parse_skill_frontmatter_valid():
    text = '---\nname: my-skill\ndescription: "does stuff"\n---\n\n# Body\n'
    result = parse_skill_frontmatter(text)
    assert result == {"name": "my-skill", "description": "does stuff"}


def test_parse_skill_frontmatter_missing():
    assert parse_skill_frontmatter("# No frontmatter\nHello") == {}


def test_parse_skill_frontmatter_invalid_yaml():
    assert parse_skill_frontmatter("---\n: :\n---\n") == {}


def test_parse_skill_frontmatter_non_dict():
    assert parse_skill_frontmatter("---\n- list\n- item\n---\n") == {}


def test_parse_skill_frontmatter_bom():
    text = '\ufeff---\nname: bom-skill\ndescription: "bom"\n---\n'
    result = parse_skill_frontmatter(text)
    assert result["name"] == "bom-skill"


def test_parse_skill_frontmatter_eof_after_closing():
    text = '---\nname: eof\ndescription: "x"\n---'
    result = parse_skill_frontmatter(text)
    assert result["name"] == "eof"


# -- registry loading tests ----------------------------------------------


def test_skill_registry_matches_conditional_send_message(tmp_path: Path):
    guide_path = _write_discord_skill(tmp_path)
    registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    requirements = registry.requirements_for_tool_call(
        ToolCall(
            id="t1",
            name="send_message",
            arguments={"channel": "discord", "body": "hi"},
        )
    )
    assert [item.skill_name for item in requirements] == ["discord-messaging"]
    assert requirements[0].guide_rel_path == "kernel/builtin-skills/discord-messaging/SKILL.md"

    assert registry.requirements_for_tool_call(
        ToolCall(
            id="t2",
            name="send_message",
            arguments={"channel": "gmail", "body": "hi"},
        )
    ) == []
    assert registry.note_loaded_guide(path=str(guide_path)) == "discord-messaging"


def test_meta_yaml_fallback_when_skill_md_absent(tmp_path: Path):
    """Legacy meta.yaml should work when SKILL.md is missing."""
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "discord-messaging"
    skill_dir.mkdir(parents=True)
    (skill_dir / "guide.md").write_text("discord guide body", encoding="utf-8")
    (skill_dir / "meta.yaml").write_text(
        "id: discord-messaging\nguide: guide.md\n",
        encoding="utf-8",
    )

    registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )
    requirements = registry.requirements_for_tool_call(
        ToolCall(id="t1", name="send_message", arguments={"channel": "discord", "body": "hi"}),
    )
    assert len(requirements) == 1
    assert requirements[0].skill_name == "discord-messaging"


def test_skill_md_invalid_frontmatter_skipped(tmp_path: Path):
    """SKILL.md with invalid frontmatter should be skipped (no meta.yaml fallback)."""
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# No frontmatter at all\nHello", encoding="utf-8")
    # Even if meta.yaml exists, SKILL.md takes precedence and fails
    (skill_dir / "meta.yaml").write_text("id: bad-skill\nguide: guide.md\n", encoding="utf-8")

    config = SkillGovernanceConfig(external_skills_dir=None)
    registry = SkillGovernanceRegistry.load(tmp_path, governance_config=config)
    assert registry.requirements_for_tool_call(
        ToolCall(id="t1", name="anything", arguments={}),
    ) == []


def test_skill_md_wins_over_meta_yaml(tmp_path: Path):
    """When both SKILL.md and meta.yaml exist, SKILL.md is used."""
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "dual"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: dual\ndescription: "from SKILL.md"\n---\nBody',
        encoding="utf-8",
    )
    (skill_dir / "meta.yaml").write_text("id: dual-legacy\nguide: guide.md\n", encoding="utf-8")

    config = SkillGovernanceConfig(external_skills_dir=None)
    registry = SkillGovernanceRegistry.load(tmp_path, governance_config=config)
    # Should use name from SKILL.md, not meta.yaml
    assert registry.note_loaded_guide(path=str(skill_dir / "SKILL.md")) == "dual"


def test_three_root_priority(tmp_path: Path):
    """Builtin > personal > external priority."""
    builtin = tmp_path / "kernel" / "builtin-skills" / "my-skill"
    builtin.mkdir(parents=True)
    (builtin / "SKILL.md").write_text(
        '---\nname: my-skill\ndescription: "builtin"\n---\nBuiltin body',
        encoding="utf-8",
    )

    personal = tmp_path / "memory" / "agent" / "skills" / "my-skill"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        '---\nname: my-skill\ndescription: "personal"\n---\nPersonal body',
        encoding="utf-8",
    )

    ext_dir = tmp_path / "ext" / "my-skill"
    ext_dir.mkdir(parents=True)
    (ext_dir / "SKILL.md").write_text(
        '---\nname: my-skill\ndescription: "external"\n---\nExternal body',
        encoding="utf-8",
    )

    config = SkillGovernanceConfig(external_skills_dir=str(tmp_path / "ext"))
    registry = SkillGovernanceRegistry.load(tmp_path, governance_config=config)

    # Should resolve to builtin
    assert registry.note_loaded_guide(path=str(builtin / "SKILL.md")) == "my-skill"
    # Personal and external paths should NOT be registered
    assert registry.note_loaded_guide(path=str(personal / "SKILL.md")) is None
    assert registry.note_loaded_guide(path=str(ext_dir / "SKILL.md")) is None


def test_governance_rule_unknown_skill_warns(tmp_path: Path, caplog):
    """Governance rule referencing non-existent skill should log warning."""
    config = SkillGovernanceConfig(
        external_skills_dir=None,
        rules=[
            GovernanceRule(skill="nonexistent", tool="send_message", when={}),
        ],
    )
    import logging
    with caplog.at_level(logging.WARNING):
        SkillGovernanceRegistry.load(tmp_path, governance_config=config)
    assert any("unknown skill 'nonexistent'" in r.message for r in caplog.records)


def test_name_derived_from_directory(tmp_path: Path):
    """When frontmatter has no name, use directory name."""
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "auto-named"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\ndescription: "no name field"\n---\nBody',
        encoding="utf-8",
    )
    config = SkillGovernanceConfig(external_skills_dir=None)
    registry = SkillGovernanceRegistry.load(tmp_path, governance_config=config)
    assert registry.note_loaded_guide(path=str(skill_dir / "SKILL.md")) == "auto-named"


# -- responder integration tests -----------------------------------------


def test_run_responder_injects_required_skill_before_discord_send(tmp_path: Path):
    _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    turn_context = TurnContext()
    turn_context.set_inbound("discord", "alice", {})

    client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
        ]
    )
    registry = _Registry({"send_message": "OK: sent to discord"})

    response = _run_responder(
        client=client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=turn_context,
    )

    assert response.finish_reason != "error"
    assert registry.executed == ["send_message"]
    assert len(client.calls) == 3
    assert any(
        msg.role == "tool"
        and msg.name == SKILL_PREREQUISITE_TOOL_NAME
        and "discord guide body" in str(msg.content)
        for msg in client.calls[1]
    )


def test_run_brain_responder_proactively_injects_selected_skill(tmp_path: Path):
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "skill-creator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: skill-creator\n"
        'description: "Create or modify skills."\n'
        "---\n\n"
        "skill creator guide body",
        encoding="utf-8",
    )
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path,
        governance_config=SkillGovernanceConfig(external_skills_dir=None),
    )
    conversation = Conversation()
    conversation.add("user", "Fix SKILL.md format in personal-skills.", channel="cli")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    captured: dict[str, object] = {}
    console = _console()

    def _fake_run_responder(*args, **kwargs):
        captured["messages"] = list(args[1])
        return LLMResponse(content="ok", tool_calls=[])

    result = _run_brain_responder(
        client=MagicMock(),
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=MagicMock(),
        console=console,  # type: ignore[arg-type]
        config=_brain_config(),
        channel="cli",
        sender=None,
        run_responder_fn=_fake_run_responder,
        skill_registry=skill_registry,
        skill_check_agent=_SkillCheckAgent(["skill-creator"]),  # type: ignore[arg-type]
    )

    assert result.content == "ok"
    injected_tools = [
        entry for entry in conversation.get_messages()
        if entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
    ]
    assert len(injected_tools) == 1
    assert "skill creator guide body" in str(injected_tools[0].content)
    passed_messages = captured["messages"]
    assert any(
        msg.role == "tool"
        and msg.name == SKILL_PREREQUISITE_TOOL_NAME
        and "skill creator guide body" in str(msg.content)
        for msg in passed_messages
    )
    console.print_info.assert_any_call("Loaded skill guide: skill-creator")


def test_run_brain_responder_skips_proactive_skill_when_already_loaded(tmp_path: Path):
    skill_dir = tmp_path / "kernel" / "builtin-skills" / "skill-creator"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: skill-creator\n"
        'description: "Create or modify skills."\n'
        "---\n\n"
        "skill creator guide body",
        encoding="utf-8",
    )
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path,
        governance_config=SkillGovernanceConfig(external_skills_dir=None),
    )
    conversation = Conversation()
    conversation.add("user", "Fix SKILL.md format in personal-skills.", channel="cli")
    old_call = ToolCall(
        id="old-skill",
        name=SKILL_PREREQUISITE_TOOL_NAME,
        arguments={
            "skill_name": "skill-creator",
            "path": "kernel/builtin-skills/skill-creator/SKILL.md",
        },
    )
    conversation.add_assistant_with_tools(None, [old_call])
    conversation.add_tool_result(old_call.id, old_call.name, "already loaded")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    console = _console()

    _run_brain_responder(
        client=MagicMock(),
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=MagicMock(),
        console=console,  # type: ignore[arg-type]
        config=_brain_config(),
        channel="cli",
        sender=None,
        run_responder_fn=lambda *args, **kwargs: LLMResponse(content="ok", tool_calls=[]),
        skill_registry=skill_registry,
        skill_check_agent=_SkillCheckAgent(["skill-creator"]),  # type: ignore[arg-type]
    )

    assert sum(
        1
        for entry in conversation.get_messages()
        if entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
    ) == 1
    console.print_info.assert_any_call("Skill guide already loaded: skill-creator")


def test_read_file_of_skill_guide_marks_turn_as_loaded(tmp_path: Path):
    guide_path = _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    turn_context = TurnContext()
    turn_context.set_inbound("discord", "alice", {})

    client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="read_file",
                        arguments={"path": str(guide_path)},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
        ]
    )
    registry = _Registry(
        {
            "read_file": '<file path="kernel/builtin-skills/discord-messaging/SKILL.md">discord guide body</file>',
            "send_message": "OK: sent to discord",
        }
    )

    response = _run_responder(
        client=client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=turn_context,
    )

    assert response.finish_reason != "error"
    assert registry.executed == ["read_file", "send_message"]
    assert len(client.calls) == 3
    assert all(
        not any(msg.name == SKILL_PREREQUISITE_TOOL_NAME for msg in call if msg.role == "tool")
        for call in client.calls
    )


def test_second_turn_reuses_existing_injected_skill_guide(tmp_path: Path):
    _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    registry = _Registry({"send_message": "OK: sent to discord"})

    first_turn_context = TurnContext()
    first_turn_context.set_inbound("discord", "alice", {})
    first_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
        ]
    )

    first_response = _run_responder(
        client=first_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=first_turn_context,
    )

    assert first_response.finish_reason != "error"
    assert len(first_client.calls) == 3
    initial_injected_count = sum(
        1
        for entry in conversation.get_messages()
        if entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
    )

    conversation.add("user", "again", channel="discord", sender="alice")
    second_turn_context = TurnContext()
    second_turn_context.set_inbound("discord", "alice", {})
    second_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t3",
                        name="send_message",
                        arguments={"channel": "discord", "body": "second"},
                    )
                ],
            ),
        ]
    )

    second_response = _run_responder(
        client=second_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=second_turn_context,
    )

    assert second_response.finish_reason != "error"
    assert len(second_client.calls) == 2
    assert sum(
        1
        for entry in conversation.get_messages()
        if entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
    ) == initial_injected_count


def test_second_turn_reuses_prior_read_file_guide_from_conversation(tmp_path: Path):
    guide_path = _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    registry = _Registry(
        {
            "read_file": '<file path="kernel/builtin-skills/discord-messaging/SKILL.md">discord guide body</file>',
            "send_message": "OK: sent to discord",
        }
    )

    first_turn_context = TurnContext()
    first_turn_context.set_inbound("discord", "alice", {})
    first_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="read_file",
                        arguments={"path": str(guide_path)},
                    )
                ],
            ),
            LLMResponse(content=None, tool_calls=[]),
        ]
    )

    first_response = _run_responder(
        client=first_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=first_turn_context,
    )

    assert first_response.tool_calls == []

    conversation.add("user", "send now", channel="discord", sender="alice")
    second_turn_context = TurnContext()
    second_turn_context.set_inbound("discord", "alice", {})
    second_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
        ]
    )

    second_response = _run_responder(
        client=second_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=second_turn_context,
    )

    assert second_response.finish_reason != "error"
    assert len(second_client.calls) == 2
    assert not any(
        msg.role == "tool" and msg.name == SKILL_PREREQUISITE_TOOL_NAME
        for msg in second_client.calls[0]
    )


def test_compaction_drops_guide_and_next_turn_reinjects(tmp_path: Path):
    _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")
    builder = ContextBuilder(system_prompt="sys", agent_os_dir=tmp_path)
    registry = _Registry({"send_message": "OK: sent to discord"})

    first_turn_context = TurnContext()
    first_turn_context.set_inbound("discord", "alice", {})
    first_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t1",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t2",
                        name="send_message",
                        arguments={"channel": "discord", "body": "hi"},
                    )
                ],
            ),
        ]
    )

    first_response = _run_responder(
        client=first_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=first_turn_context,
    )

    assert first_response.finish_reason != "error"
    assert len(first_client.calls) == 3
    assert any(
        entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
        for entry in conversation.get_messages()
    )

    conversation.add("user", "placeholder", channel="discord", sender="alice")
    removed = conversation.compact(1)
    assert removed > 0
    assert not any(
        entry.role == "tool" and entry.name == SKILL_PREREQUISITE_TOOL_NAME
        for entry in conversation.get_messages()
    )

    conversation.add("user", "send again", channel="discord", sender="alice")
    second_turn_context = TurnContext()
    second_turn_context.set_inbound("discord", "alice", {})
    second_client = _Client(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t3",
                        name="send_message",
                        arguments={"channel": "discord", "body": "again"},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="t4",
                        name="send_message",
                        arguments={"channel": "discord", "body": "again"},
                    )
                ],
            ),
        ]
    )

    second_response = _run_responder(
        client=second_client,  # type: ignore[arg-type]
        messages=_base_messages(conversation, builder),
        tools=_tool_definitions(),
        conversation=conversation,
        builder=builder,
        registry=registry,  # type: ignore[arg-type]
        console=_console(),  # type: ignore[arg-type]
        skill_registry=skill_registry,
        turn_context=second_turn_context,
    )

    assert second_response.finish_reason != "error"
    assert len(second_client.calls) == 3


def test_old_skill_id_in_conversation_still_recognized(tmp_path: Path):
    """Backward compat: old conversations with skill_id should still work."""
    _write_discord_skill(tmp_path)
    skill_registry = SkillGovernanceRegistry.load(
        tmp_path, governance_config=_DISCORD_GOVERNANCE,
    )

    conversation = Conversation()
    conversation.add("user", "hello", channel="discord", sender="alice")

    # Simulate old-format injected guide using skill_id
    old_call = ToolCall(
        id="old_skill_001",
        name=SKILL_PREREQUISITE_TOOL_NAME,
        arguments={"skill_id": "discord-messaging", "path": "kernel/builtin-skills/discord-messaging/SKILL.md"},
    )
    conversation.add_assistant_with_tools(None, [old_call])
    conversation.add_tool_result(
        old_call.id,
        SKILL_PREREQUISITE_TOOL_NAME,
        "[Required Skill Guide Loaded]\nskill_id: discord-messaging\ndiscord guide body",
    )

    loaded = skill_registry.loaded_skill_names_from_conversation(conversation)
    assert "discord-messaging" in loaded
