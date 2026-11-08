"""Tests for the recall sub-agent: corpus tools, path guard, capped tool loop."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from lincy.core.schema import RecallAgentConfig
from lincy.llm.schema import ContentPart, LLMResponse, Message, ToolCall
from lincy.session.schema import SessionEntry
from lincy.tools.builtin.recall import (
    RecallAgent,
    _expand_braces,
    _RENDERER_TOKENS,
    _grep_transcript,
    _literal_terms,
    build_recall_registry,
    create_recall_tool,
    format_context_tail,
    render_transcript_entry,
)

SESSION_ID = "20261105_030000_abc123"


def _line(
    role: str,
    content: str | None,
    *,
    ts: str,
    channel: str | None = None,
    sender: str | None = None,
    tool_calls: list[ToolCall] | None = None,
    name: str | None = None,
    tool_call_id: str | None = None,
) -> str:
    message = Message(
        role=role,
        content=content,
        timestamp=datetime.fromisoformat(ts),
        tool_calls=tool_calls or [],
        name=name,
        tool_call_id=tool_call_id,
    )
    return SessionEntry(message=message, channel=channel, sender=sender).model_dump_json()


def _workspace(tmp_path: Path) -> Path:
    people = tmp_path / "memory" / "people" / "yufeng"
    people.mkdir(parents=True, exist_ok=True)
    (people / "health.md").write_text(
        "# health\n\n- 2026-11-02 心寬福拿藥\n- 睡前藥一包\n",
        encoding="utf-8",
    )
    agent_dir = tmp_path / "memory" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "index.md").write_text("# index\n", encoding="utf-8")
    (agent_dir / "notes.json").write_text("{}", encoding="utf-8")
    session = tmp_path / "session" / "brain" / SESSION_ID
    session.mkdir(parents=True, exist_ok=True)
    lines = [
        _line(
            "user", "我今天去心寬福拿藥了",
            ts="2026-11-05T14:44:00+08:00", channel="discord", sender="yufeng",
        ),
        _line(
            "assistant", None, ts="2026-11-05T14:45:00+08:00",
            tool_calls=[ToolCall(
                id="t1", name="send_message",
                arguments={"channel": "discord", "to": "yufeng", "body": "太好了，藥拿到了"},
            )],
        ),
        _line("tool", "sent", ts="2026-11-05T14:45:01+08:00", name="send_message", tool_call_id="t1"),
        _line(
            "user", "晚餐吃麥當勞",
            ts="2026-11-05T18:00:00+08:00", channel="discord", sender="yufeng",
        ),
    ]
    (session / "transcript.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (session / "requests.jsonl").write_text('{"dump": "心寬福 secret"}\n', encoding="utf-8")
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "notes.json").write_text('{"x": "心寬福"}', encoding="utf-8")
    return tmp_path


def _run(registry, name: str, **arguments) -> str:
    return registry.execute(ToolCall(id="c", name=name, arguments=arguments)).content


# -- grep ---------------------------------------------------------------------

def test_grep_markdown_returns_matches_with_context(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(registry, "grep", pattern="心寬福")

    assert "memory/people/yufeng/health.md:3: - 2026-11-02 心寬福拿藥" in out
    assert "memory/people/yufeng/health.md-4- - 睡前藥一包" in out


def test_grep_transcript_renders_entries_and_send_message_body(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(
        registry, "grep", pattern="心寬福",
        path_glob="session/brain/*/transcript.jsonl", context_lines=1,
    )

    assert f"session/brain/{SESSION_ID}/transcript.jsonl:1: " in out
    assert "[2026-11-05 (Thu) 14:44 discord/yufeng user] 我今天去心寬福拿藥了" in out
    assert "transcript.jsonl-2- [2026-11-05 (Thu) 14:45 assistant] -> send_message(yufeng): 太好了" in out
    assert "麥當勞" not in out


def test_grep_lists_live_files_before_archive_copies(tmp_path: Path):
    ws = _workspace(tmp_path)
    daily = ws / "memory" / "archive" / "temp-memory"
    daily.mkdir(parents=True)
    (daily / "2026-11-01.md").write_text("- 心寬福 舊快照\n", encoding="utf-8")
    (daily / "2026-11-03.md").write_text("- 心寬福 新快照\n", encoding="utf-8")
    curation = ws / "memory" / "archive" / "curation" / "memory" / "people" / "yufeng" / "health.md"
    curation.mkdir(parents=True)
    (curation / "2026-10-01.md").write_text("- 心寬福 舊版本\n", encoding="utf-8")
    registry = build_recall_registry(ws)

    output = registry.execute(
        ToolCall(id="g", name="grep", arguments={"pattern": "心寬福", "max_matches": 10})
    ).content

    positions = [
        output.index(marker)
        for marker in (
            "memory/people/yufeng/health.md",
            "archive/temp-memory/2026-11-03.md",
            "archive/temp-memory/2026-11-01.md",
            "archive/curation/",
        )
    ]
    assert positions == sorted(positions)


def test_grep_count_only_reports_per_file_counts(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(registry, "grep", pattern="藥", path_glob="session/brain/**/*.jsonl", count_only=True)

    assert f"session/brain/{SESSION_ID}/transcript.jsonl: 2" in out
    assert "Total: 2 match(es) in 1 of 1 file(s)." in out


def test_grep_never_reads_debug_dumps_or_state(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(registry, "grep", pattern="secret|心寬福", path_glob="session/brain/**/*.jsonl")
    assert "requests.jsonl" not in out
    assert "secret" not in out

    assert _run(registry, "grep", pattern="x", path_glob="session/brain/*/requests.jsonl").startswith("No files match")
    assert _run(registry, "grep", pattern="x", path_glob="state/*.json").startswith("Error")
    assert _run(registry, "grep", pattern="x", path_glob="memory/../state/*.json").startswith("Error")
    assert _run(registry, "grep", pattern="x", path_glob="memory/**/*.json").startswith("No files match")
    assert _run(registry, "grep", pattern="x", path_glob=str(tmp_path.parent / "elsewhere")).startswith("Error")


def test_grep_reports_no_matches_and_invalid_pattern(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    assert _run(registry, "grep", pattern="nothing-here").startswith("No matches for /nothing-here/ in 2 file(s)")
    assert _run(registry, "grep", pattern="(").startswith("Error: invalid pattern")


def test_grep_caps_matches(tmp_path: Path):
    root = _workspace(tmp_path)
    big = root / "memory" / "agent" / "big.md"
    big.write_text("\n".join(f"- line {i} hit" for i in range(100)), encoding="utf-8")
    registry = build_recall_registry(root)

    out = _run(registry, "grep", pattern="hit", path_glob="memory/agent/big.md", max_matches=5, context_lines=0)

    assert out.count("big.md:") == 5
    assert "stopped after 5 matches" in out


# -- list_dir / read_file ------------------------------------------------------

def test_list_dir_lists_sessions_and_memory_files(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    sessions = _run(registry, "list_dir", path_glob="session/brain/*")
    assert f"session/brain/{SESSION_ID}/  (dir" in sessions

    files = _run(registry, "list_dir", path_glob="memory/people/yufeng/*")
    assert "memory/people/yufeng/health.md  (" in files
    assert "notes.json" not in _run(registry, "list_dir", path_glob="memory/agent/*")


def test_read_file_renders_transcript_lines(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(
        registry, "read_file",
        path=f"session/brain/{SESSION_ID}/transcript.jsonl", offset=1, limit=2,
    )

    assert out.startswith(f"session/brain/{SESSION_ID}/transcript.jsonl lines 1-2 of 4")
    assert "1: [2026-11-05 (Thu) 14:44 discord/yufeng user] 我今天去心寬福拿藥了" in out
    assert "2: [2026-11-05 (Thu) 14:45 assistant] -> send_message(yufeng)" in out
    assert "3:" not in out


def test_read_file_reads_markdown_and_rejects_dumps(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(registry, "read_file", path="memory/people/yufeng/health.md")
    assert "心寬福拿藥" in out

    assert _run(registry, "read_file", path=f"session/brain/{SESSION_ID}/requests.jsonl").startswith("Error")
    assert _run(registry, "read_file", path="state/notes.json").startswith("Error")


def test_render_transcript_entry_tool_result_is_clipped():
    entry = SessionEntry(
        message=Message(
            role="tool", content="x" * 500, name="grep", tool_call_id="t1",
            timestamp=datetime.fromisoformat("2026-11-05T09:00:00+08:00"),
        ),
    )
    text = render_transcript_entry(entry)
    assert text.startswith("[2026-11-05 (Thu) 09:00 tool] (grep) ")
    assert len(text) < 260


def test_format_context_tail_takes_last_user_and_assistant_texts():
    entries = [
        SessionEntry(message=Message(role="user", content="first")),
        SessionEntry(message=Message(role="assistant", content="a" * 400)),
        SessionEntry(message=Message(role="tool", content="ignored", name="x", tool_call_id="1")),
        SessionEntry(message=Message(role="user", content="last")),
    ]
    tail = format_context_tail(entries, max_entries=2, max_chars=50)
    lines = tail.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("assistant: aaa")
    assert lines[0].endswith("...")
    assert lines[1] == "user: last"

    # User lines keep who said them, so the sub-agent knows whose thread it is.
    attributed = [
        SessionEntry(message=Message(role="user", content="hi"), channel="discord", sender="yufeng"),
        SessionEntry(message=Message(role="user", content="yo"), channel="cli"),
    ]
    assert format_context_tail(attributed).splitlines() == ["user (discord/yufeng): hi", "user (cli): yo"]


# -- RecallAgent ----------------------------------------------------------------

class _ScriptedClient:
    """Keeps calling grep until the loop tells it to stop, then answers."""

    def __init__(self, final_text: str, *, always_call_tools: bool = True):
        self.calls: list[list[Message]] = []
        self._final_text = final_text
        self._always_call_tools = always_call_tools

    def chat_with_tools(self, messages, tools, temperature=None):
        del temperature
        self.calls.append(list(messages))
        assert tools, "recall must keep its tools attached on every request"
        limit_hit = any(
            m.role == "user" and "Tool call limit reached" in (m.content or "")
            for m in messages
        )
        if self._always_call_tools and not limit_hit:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id=f"t{len(self.calls)}", name="grep", arguments={"pattern": "藥"})],
            )
        return LLMResponse(content=self._final_text, tool_calls=[])


def _agent(tmp_path: Path, client, **config) -> RecallAgent:
    return RecallAgent(
        client,
        "system prompt",
        agent_os_dir=_workspace(tmp_path),
        config=RecallAgentConfig(**config),
    )


def test_recall_agent_stops_at_tool_call_limit_and_finalizes(tmp_path: Path):
    client = _ScriptedClient("- [2026-11-05 14:44][session/x] fact")
    agent = _agent(tmp_path, client, max_tool_calls=12, max_result_chars=1500)

    run = agent.run("question", max_tool_calls=2)

    assert run.tool_calls == 2
    assert run.hit_limit is True
    assert run.text == "- [2026-11-05 14:44][session/x] fact"
    # Two executed grep calls, then the finalisation prompt closes the loop.
    assert len(client.calls) == 3
    final_request = client.calls[-1]
    tool_results = [m for m in final_request if m.role == "tool"]
    assert len(tool_results) == 2
    assert all("health.md" in m.content for m in tool_results)
    assert final_request[-1].role == "user"
    assert "Tool call limit reached" in final_request[-1].content


def test_recall_agent_marks_calls_beyond_the_limit_in_one_batch(tmp_path: Path):
    class _BatchClient:
        def __init__(self):
            self.calls: list[list[Message]] = []

        def chat_with_tools(self, messages, tools, temperature=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return LLMResponse(content=None, tool_calls=[
                    ToolCall(id="a", name="grep", arguments={"pattern": "藥"}),
                    ToolCall(id="b", name="grep", arguments={"pattern": "藥"}),
                    ToolCall(id="c", name="grep", arguments={"pattern": "藥"}),
                ])
            return LLMResponse(content="NONE", tool_calls=[])

    client = _BatchClient()
    run = _agent(tmp_path, client).run("q", max_tool_calls=2)

    assert run.tool_calls == 2
    assert run.hit_limit is True
    results = {m.tool_call_id: m.content for m in client.calls[-1] if m.role == "tool"}
    assert "health.md" in results["a"] and "health.md" in results["b"]
    assert results["c"].startswith("Tool call limit reached")


def test_recall_returns_none_for_none_and_truncates_long_results(tmp_path: Path):
    none_client = _ScriptedClient("NONE.", always_call_tools=False)
    assert _agent(tmp_path, none_client).recall("q") is None

    long_text = "\n".join(f"- [2026-11-0{i % 9 + 1} 10:00][p] fact {i}" for i in range(60))
    long_client = _ScriptedClient(long_text, always_call_tools=False)
    result = _agent(tmp_path, long_client, max_result_chars=300).recall("q")
    assert result is not None
    assert len(result) <= 300 + len("\n(truncated)")
    assert result.endswith("(truncated)")


def test_recall_request_carries_question_context_and_loaded_paths(tmp_path: Path):
    client = _ScriptedClient("NONE", always_call_tools=False)
    agent = _agent(tmp_path, client)

    agent.recall(
        "我上次拿藥是什麼時候",
        sender="yufeng",
        channel="discord",
        context_tail="user: 藥快沒了",
        exclude_paths={"memory/agent/index.md", "memory/agent/temp-memory.md"},
    )

    request = client.calls[0]
    assert request[0].role == "system" and request[0].content == "system prompt"
    body = request[1].content
    assert body.startswith("Now: ")
    assert "Question: 我上次拿藥是什麼時候" in body
    assert "Asked by: yufeng via discord." in body
    assert "Recent conversation" in body and "藥快沒了" in body
    assert "- memory/agent/index.md\n- memory/agent/temp-memory.md" in body


def test_brain_tool_wraps_agent_results_and_errors(tmp_path: Path):
    client = _ScriptedClient("- [2026-11-05 14:44][session/x] fact", always_call_tools=False)
    tool = create_recall_tool(_agent(tmp_path, client))
    assert tool(question="q") == "- [2026-11-05 14:44][session/x] fact"
    assert tool(question="   ") == "Error: question is required."

    none_tool = create_recall_tool(_agent(tmp_path, _ScriptedClient("NONE", always_call_tools=False)))
    assert none_tool(question="q") == "No relevant memory found."

    class _Boom:
        def chat_with_tools(self, messages, tools, temperature=None):
            raise RuntimeError("boom")

    assert create_recall_tool(_agent(tmp_path, _Boom()))(question="q") == "Error: recall failed: boom"


def test_grep_transcript_matches_rendered_text_only(tmp_path: Path):
    ws = _workspace(tmp_path)
    session = ws / "session" / "brain" / SESSION_ID
    overlay_entry = SessionEntry(
        message=Message(
            role="user", content="再問一次",
            timestamp=datetime.fromisoformat("2026-11-05T19:00:00+08:00"),
        ),
        channel="discord", sender="yufeng",
        metadata={"turn_overlay": "[Recall]\n- [2026-11-05 14:44][x] overlay-only text"},
    )
    with (session / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(overlay_entry.model_dump_json() + "\n")
    registry = build_recall_registry(ws)
    glob = "session/brain/*/transcript.jsonl"

    # One pattern can span the rendered stamp, sender and text.
    out = _run(registry, "grep", pattern=r"yufeng user\] .*藥", path_glob=glob)
    assert "[2026-11-05 (Thu) 14:44 discord/yufeng user] 我今天去心寬福拿藥了" in out
    assert "麥當勞" not in out
    assert "麥當勞" in _run(registry, "grep", pattern=r"11-05 \(Thu\) 18:00", path_glob=glob)

    # Raw JSON keys and entry metadata are not searchable: the [Recall] block
    # of an earlier turn must not feed the next recall.
    assert _run(registry, "grep", pattern="tool_call_id", path_glob=glob).startswith("No matches")
    assert _run(registry, "grep", pattern="overlay-only", path_glob=glob).startswith("No matches")


def test_recall_agent_stops_at_the_deadline_and_replays_reasoning(tmp_path: Path):
    class _DeadlineClient:
        def __init__(self):
            self.calls: list[list[Message]] = []

        def chat_with_tools(self, messages, tools, temperature=None):
            del temperature
            self.calls.append(list(messages))
            if any(m.role == "user" and "Time budget reached" in (m.content or "") for m in messages):
                return LLMResponse(content="- [2026-11-05 14:44][session/x] fact", tool_calls=[])
            return LLMResponse(
                content=None,
                reasoning_content="looking",
                reasoning_details=[{"type": "thinking", "signature": "sig"}],
                served_by="haiku-primary",
                tool_calls=[ToolCall(id=f"t{len(self.calls)}", name="grep", arguments={"pattern": "藥"})],
            )

    client = _DeadlineClient()
    run = _agent(tmp_path, client).run("q", max_tool_calls=12, deadline_seconds=1e-6)

    assert run.timed_out is True
    assert run.hit_limit is True
    assert run.tool_calls == 1
    assert run.text == "- [2026-11-05 14:44][session/x] fact"
    final_request = client.calls[-1]
    assert final_request[-1].role == "user"
    assert "Time budget reached" in final_request[-1].content
    # The assistant turn goes back with its reasoning, as the brain's side channels do.
    replayed = next(m for m in final_request if m.role == "assistant")
    assert replayed.reasoning_content == "looking"
    assert replayed.reasoning_details == [{"type": "thinking", "signature": "sig"}]
    assert replayed.reasoning_origin == "haiku-primary"


def test_long_entries_are_excerpted_around_the_match_and_readable_in_full(tmp_path: Path):
    ws = _workspace(tmp_path)
    session = ws / "session" / "brain" / SESSION_ID
    long_text = "開場白 " * 400 + "截止日是 11-20 交件"
    entry = SessionEntry(
        message=Message(
            role="user", content=long_text,
            timestamp=datetime.fromisoformat("2026-11-05T20:00:00+08:00"),
        ),
        channel="discord", sender="yufeng",
    )
    with (session / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")
    registry = build_recall_registry(ws)
    path = f"session/brain/{SESSION_ID}/transcript.jsonl"

    # The excerpt keeps the stamp and speaker and shows the hit itself.
    out = _run(registry, "grep", pattern="截止日", path_glob=path, context_lines=0)
    assert f"{path}:5: [2026-11-05 (Thu) 20:00 discord/yufeng user] ..." in out
    assert "截止日是 11-20 交件" in out
    assert len(out) < 400

    # A multi-entry read clips, a single-entry read does not.
    clipped = _run(registry, "read_file", path=path, offset=5, limit=2)
    assert "截止日" not in clipped
    full = _run(registry, "read_file", path=path, offset=5, limit=1)
    assert "截止日是 11-20 交件" in full


def test_grep_markdown_excerpts_long_lines_around_the_match(tmp_path: Path):
    ws = _workspace(tmp_path)
    health = ws / "memory" / "people" / "yufeng" / "health.md"
    health.write_text("# health\n\n- " + "舊紀錄 " * 200 + "回診日 11-21\n", encoding="utf-8")
    registry = build_recall_registry(ws)

    out = _run(registry, "grep", pattern="回診日", path_glob="memory/people/yufeng/health.md")

    assert "memory/people/yufeng/health.md:3: ..." in out
    assert "回診日 11-21" in out


# -- brace globs and the literal prefilter -------------------------------------

def test_grep_accepts_brace_alternatives_and_guards_each_one(tmp_path: Path):
    registry = build_recall_registry(_workspace(tmp_path))

    out = _run(
        registry, "grep", pattern="心寬福",
        path_glob="{memory/people/**/*.md,session/brain/202611*/transcript.jsonl}",
        context_lines=0,
    )
    assert "memory/people/yufeng/health.md:3:" in out
    assert f"session/brain/{SESSION_ID}/transcript.jsonl:1:" in out

    rejected = _run(registry, "grep", pattern="x", path_glob="{memory/**/*.md,state/*.json}")
    assert rejected.startswith("Error: path_glob must start with memory/ or session/brain/.")
    assert _run(registry, "grep", pattern="x", path_glob="{memory/**/*.md,../*.md}").startswith(
        "Error: '..' is not allowed"
    )
    assert _expand_braces("{a,b}/x{1,2}.md") == ["a/x1.md", "a/x2.md", "b/x1.md", "b/x2.md"]
    assert _expand_braces("plain/*.md") == ["plain/*.md"]


def test_literal_prefilter_never_changes_transcript_grep_results(tmp_path: Path):
    root = _workspace(tmp_path)
    transcript = root / "session" / "brain" / SESSION_ID / "transcript.jsonl"
    extra = [
        # A line written by another tool with ASCII escapes must stay searchable.
        json.dumps(
            json.loads(_line(
                "user", "喉嚨很乾", ts="2026-11-05T19:00:00+08:00", channel="discord", sender="yufeng",
            )),
            ensure_ascii=True,
        ),
        # Text parts are concatenated when rendered: "鼻" + "塞" reads as 鼻塞.
        SessionEntry(
            message=Message(
                role="user",
                content=[ContentPart(type="text", text="鼻"), ContentPart(type="text", text="塞")],
                timestamp=datetime.fromisoformat("2026-11-05T19:01:00+08:00"),
            ),
            channel="discord", sender="yufeng",
        ).model_dump_json(),
        # Tool results lose their newlines when clipped.
        _line("tool", "line one\nline two", ts="2026-11-05T19:02:00+08:00", name="web_fetch", tool_call_id="w1"),
    ]
    transcript.write_text(
        transcript.read_text(encoding="utf-8") + "\n".join(extra) + "\n", encoding="utf-8",
    )
    rel = Path("session") / "brain" / SESSION_ID / "transcript.jsonl"

    cases = {
        "心寬福|麥當勞": [["心寬福"], ["麥當勞"]],
        "喉嚨": [["喉嚨"]],
        "鼻塞": [["鼻塞"]],
        "one line two": [["one", "line", "two"]],
        "Send_Message": [["send_message"]],
        # Terms that touch the rendered header or markers keep their content atoms.
        "discord/yufeng": [["discord", "yufeng"]],
        "-> send_message": [["send_message"]],
        "yufeng user": [["yufeng", "user"]],
        "14:44 discord/yufeng": [["discord", "yufeng"]],
        r"\(web_fetch\) line": None,  # regex syntax
        "Thu": None,  # weekday names come from the re-rendered stamp
        "2026-11-05": None,  # so do the digits
        r"11-05 \(Thu\)": None,
        "yufeng.*藥": None,
    }
    for pattern, terms in cases.items():
        assert _literal_terms(pattern) == terms, pattern
        regex = re.compile(pattern, re.IGNORECASE)
        plain = _grep_transcript(transcript, rel, regex, context_lines=1)
        fast = _grep_transcript(transcript, rel, regex, context_lines=1, prefilter=terms)
        assert plain, pattern
        assert fast == plain, pattern
    assert _literal_terms('說"好"') is None
    assert _literal_terms("a||b") is None
    assert _literal_terms("unknown time") is None

    registry = build_recall_registry(root)
    counted = _run(registry, "grep", pattern="喉嚨", path_glob="session/brain/*/transcript.jsonl", count_only=True)
    assert counted.startswith(f"session/brain/{SESSION_ID}/transcript.jsonl: 1")


def test_renderer_only_adds_stamps_separators_and_known_words(tmp_path: Path):
    """The prefilter contract: every other rendered token comes from the raw line."""
    transcript = _workspace(tmp_path) / "session" / "brain" / SESSION_ID / "transcript.jsonl"
    lines = transcript.read_text(encoding="utf-8").splitlines() + [
        _line("tool", "first line\nsecond line", ts="2026-11-05T19:02:00+08:00", name="web_fetch", tool_call_id="w1"),
        _line(
            "assistant", "查一下", ts="2026-11-05T19:03:00+08:00",
            tool_calls=[ToolCall(id="t2", name="web_search", arguments={"query": "nycu 校曆", "limit": 3})],
        ),
        SessionEntry(message=Message(role="assistant", content="no stamp")).model_dump_json(),
    ]
    for raw in lines:
        rendered = render_transcript_entry(SessionEntry.model_validate_json(raw))
        atoms = [
            atom for atom in re.split(r"[\s/\-:>,()\[\]{}\"]+", rendered.lower())
            if atom and not atom.isdigit() and atom not in _RENDERER_TOKENS
        ]
        assert atoms, rendered
        for atom in atoms:
            assert atom in raw.lower(), (atom, rendered)


def test_brace_expansion_is_capped_before_it_grows(tmp_path: Path):
    assert _expand_braces("{a,b}" * 5) is not None
    assert _expand_braces("{a,b}" * 6) is None
    registry = build_recall_registry(_workspace(tmp_path))
    out = _run(registry, "grep", pattern="x", path_glob="memory/" + "{a,b}" * 6 + "/*.md")
    assert out == "Error: path_glob expands to more than 32 globs."
