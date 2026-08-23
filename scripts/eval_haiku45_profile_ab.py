#!/usr/bin/env python3
"""Replay recent brain turns against old/new Haiku 4.5 profiles.

This script uses real persisted brain session history, rebuilds near-runtime
context with the current brain system prompt + boot files, and compares:
1. old Haiku 4.5 thinking profile
2. current Haiku 4.5 thinking profile

It then asks a stronger judge model to pick the better next-turn response.
No tools are executed; only tool definitions are provided to the LLM.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lincy.context import ContextBuilder, Conversation
from lincy.core.config import load_config, resolve_llm_config
from lincy.core.schema import OpenRouterConfig, OpenRouterReasoningConfig
from lincy.llm import create_client
from lincy.llm.base import LLMClient
from lincy.llm.schema import LLMResponse, Message, ToolCall, ToolDefinition
from lincy.memory import MEMORY_EDIT_DEFINITION, MEMORY_SEARCH_DEFINITION
from lincy.session.schema import SessionEntry
from lincy.timezone_utils import configure as configure_timezone
from lincy.tools.builtin.contact_mapping import UPDATE_CONTACT_MAPPING_DEFINITION
from lincy.tools.builtin.image import READ_IMAGE_BY_SUBAGENT_DEFINITION
from lincy.tools.builtin.schedule_action import SCHEDULE_ACTION_DEFINITION
from lincy.tools.builtin.send_message import SEND_MESSAGE_DEFINITION


TOOL_DEFINITIONS: list[ToolDefinition] = [
    SEND_MESSAGE_DEFINITION,
    SCHEDULE_ACTION_DEFINITION,
    MEMORY_EDIT_DEFINITION,
    MEMORY_SEARCH_DEFINITION,
    READ_IMAGE_BY_SUBAGENT_DEFINITION,
    UPDATE_CONTACT_MAPPING_DEFINITION,
]


JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "winner": {
            "type": "string",
            "enum": ["old", "new", "tie"],
        },
        "reason": {"type": "string"},
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    },
    "required": ["winner", "reason", "confidence"],
    "additionalProperties": False,
}


@dataclass
class CandidateResult:
    label: str
    response: LLMResponse


@dataclass
class TurnSample:
    session_id: str
    turn_index: int
    entries: list[SessionEntry]
    actual_turn: list[SessionEntry]


def _load_session_entries(session_dir: Path) -> list[SessionEntry]:
    jsonl = session_dir / "messages.jsonl"
    entries: list[SessionEntry] = []
    for raw in jsonl.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        entries.append(SessionEntry.model_validate_json(line))
    return entries


def _split_turns(entries: list[SessionEntry]) -> list[list[SessionEntry]]:
    turns: list[list[SessionEntry]] = []
    current: list[SessionEntry] = []
    for entry in entries:
        if entry.role == "user" and current:
            turns.append(current)
            current = []
        current.append(entry)
    if current:
        turns.append(current)
    return turns


def _is_human_turn(turn: list[SessionEntry]) -> bool:
    if not turn or turn[0].role != "user":
        return False
    first = turn[0]
    if first.channel == "system":
        return False
    if (first.metadata or {}).get("system") is True:
        return False
    return True


def _build_old_profile(current: OpenRouterConfig) -> OpenRouterConfig:
    return current.model_copy(
        update={
            "max_tokens": 16384,
            "verbosity": None,
            "reasoning": OpenRouterReasoningConfig(
                enabled=True,
                max_tokens=None,
                effort="high",
                supported_efforts=["low", "medium", "high"],
            ),
        }
    ).validate_reasoning(source_path=Path("old_haiku45_profile.yaml"))


def _entry_to_brief(entry: SessionEntry, *, max_chars: int = 220) -> str:
    msg = entry.message
    role = msg.role
    prefix = role
    if role == "user":
        if entry.channel and entry.sender:
            prefix = f"user[{entry.channel}:{entry.sender}]"
        elif entry.channel:
            prefix = f"user[{entry.channel}]"
    if role == "tool" and msg.name:
        prefix = f"tool[{msg.name}]"

    if role == "assistant" and msg.tool_calls:
        tools = ", ".join(tc.name for tc in msg.tool_calls)
        text = f"tool_calls={tools}"
        if msg.content:
            text += f" content={msg.content}"
    else:
        text = msg.content if isinstance(msg.content, str) else ""

    text = text.replace("\n", " ").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return f"{prefix}: {text}"


def _candidate_to_text(result: CandidateResult) -> str:
    response = result.response
    parts: list[str] = []
    if response.content:
        parts.append(f"content: {response.content}")
    if response.tool_calls:
        tc_lines = []
        for tc in response.tool_calls:
            tc_lines.append(f"- {tc.name} {json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)}")
        parts.append("tool_calls:\n" + "\n".join(tc_lines))
    parts.append(
        "usage: "
        f"prompt_tokens={response.prompt_tokens} "
        f"completion_tokens={response.completion_tokens}"
    )
    return "\n".join(parts)


def _actual_to_text(turn: list[SessionEntry]) -> str:
    lines: list[str] = []
    for entry in turn[1:]:
        lines.append(_entry_to_brief(entry, max_chars=160))
    return "\n".join(lines[:8])


def _judge_pair(
    judge: LLMClient,
    *,
    excerpt: list[str],
    actual_summary: str,
    old_result: CandidateResult,
    new_result: CandidateResult,
    seed: int,
) -> dict[str, str]:
    order = [("old", old_result), ("new", new_result)]
    random.Random(seed).shuffle(order)
    a_name, a_res = order[0]
    b_name, b_res = order[1]

    prompt = (
        "You are judging next-turn outputs for a relationship assistant agent.\n"
        "Pick the better candidate for the immediate next response.\n"
        "Criteria: understand the user's intent, preserve important context,\n"
        "choose appropriate tools, avoid unnecessary actions, and keep the tone\n"
        "appropriate for an intimate DM assistant.\n"
        "Ignore style differences unless they affect usefulness.\n\n"
        "Conversation excerpt:\n"
        + "\n".join(f"- {line}" for line in excerpt)
        + "\n\n"
        + "Actual historical continuation from runtime (reference only, not gold):\n"
        + (actual_summary or "(none)")
        + "\n\n"
        + "Candidate A:\n"
        + _candidate_to_text(a_res)
        + "\n\n"
        + "Candidate B:\n"
        + _candidate_to_text(b_res)
        + "\n\n"
        + "Return JSON with winner=old/new/tie."
    )

    raw = judge.chat(
        [Message(role="user", content=prompt)],
        response_schema=JUDGE_SCHEMA,
        temperature=0,
    )
    parsed = json.loads(raw)
    winner = parsed["winner"]
    if winner == "tie":
        return parsed
    parsed["winner"] = a_name if winner == "old" and a_name == "old" else parsed["winner"]
    parsed["winner"] = b_name if winner == "new" and b_name == "new" else parsed["winner"]
    # Map A/B choice back to old/new.
    if parsed["winner"] not in {"old", "new", "tie"}:
        raise ValueError(f"Unexpected judge winner: {parsed['winner']}")
    return parsed


def _judge_pair_ab(
    judge: LLMClient,
    *,
    excerpt: list[str],
    actual_summary: str,
    old_result: CandidateResult,
    new_result: CandidateResult,
    seed: int,
) -> dict[str, str]:
    order = [("A", old_result), ("B", new_result)]
    random.Random(seed).shuffle(order)
    letter_to_label = {letter: result.label for letter, result in order}

    prompt = (
        "You are judging next-turn outputs for a relationship assistant agent.\n"
        "Pick the better candidate for the immediate next response.\n"
        "Criteria: understand the user's intent, preserve important context,\n"
        "choose appropriate tools, avoid unnecessary actions, and keep the tone\n"
        "appropriate for an intimate DM assistant.\n"
        "Ignore style differences unless they affect usefulness.\n\n"
        "Conversation excerpt:\n"
        + "\n".join(f"- {line}" for line in excerpt)
        + "\n\n"
        + "Actual historical continuation from runtime (reference only, not gold):\n"
        + (actual_summary or "(none)")
        + "\n\n"
        + f"Candidate {order[0][0]}:\n"
        + _candidate_to_text(order[0][1])
        + "\n\n"
        + f"Candidate {order[1][0]}:\n"
        + _candidate_to_text(order[1][1])
        + "\n\n"
        + "Return JSON with winner=A/B/tie."
    )

    schema = {
        "type": "object",
        "properties": {
            "winner": {
                "type": "string",
                "enum": ["A", "B", "tie"],
            },
            "reason": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
        },
        "required": ["winner", "reason", "confidence"],
        "additionalProperties": False,
    }
    raw = judge.chat(
        [Message(role="user", content=prompt)],
        response_schema=schema,
        temperature=0,
    )
    parsed = json.loads(raw)
    winner = parsed["winner"]
    if winner == "tie":
        return {
            "winner": "tie",
            "reason": parsed["reason"],
            "confidence": parsed["confidence"],
        }
    return {
        "winner": letter_to_label[winner],
        "reason": parsed["reason"],
        "confidence": parsed["confidence"],
    }


def _pick_samples(
    session_dir: Path,
    *,
    sample_size: int,
    history_turns: int,
) -> list[TurnSample]:
    entries = _load_session_entries(session_dir)
    turns = _split_turns(entries)
    samples: list[TurnSample] = []
    for idx, turn in enumerate(turns):
        if not _is_human_turn(turn):
            continue
        start = max(0, idx - history_turns + 1)
        slice_entries = [entry for t in turns[start : idx + 1] for entry in t]
        samples.append(
            TurnSample(
                session_id=session_dir.name,
                turn_index=idx,
                entries=slice_entries,
                actual_turn=turn,
            )
        )
    return samples[-sample_size:]


def _tool_names(tool_calls: list[ToolCall] | None) -> list[str]:
    if not tool_calls:
        return []
    return [tc.name for tc in tool_calls]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--sample-size", type=int, default=6)
    parser.add_argument("--history-turns", type=int, default=3)
    args = parser.parse_args()

    app_cfg = load_config()
    configure_timezone(app_cfg.app.timezone)
    agent_os_dir = app_cfg.get_agent_os_dir()
    sessions_dir = agent_os_dir / "session" / "brain"
    if args.session_id:
        session_dir = sessions_dir / args.session_id
    else:
        dirs = sorted([d for d in sessions_dir.iterdir() if d.is_dir()])
        if not dirs:
            raise SystemExit("No brain sessions found.")
        session_dir = dirs[-1]

    samples = _pick_samples(
        session_dir,
        sample_size=args.sample_size,
        history_turns=args.history_turns,
    )
    if not samples:
        raise SystemExit("No human turns found in the selected session.")

    workspace = __import__("lincy.workspace", fromlist=["WorkspaceManager"]).WorkspaceManager(agent_os_dir)
    brain_prompt = workspace.get_system_prompt("brain")
    brain_cfg = app_cfg.agents["brain"]
    builder = ContextBuilder(
        system_prompt=brain_prompt,
        agent_os_dir=agent_os_dir,
        boot_files=app_cfg.context.boot_files,
        boot_files_as_tool=app_cfg.context.boot_files_as_tool,
        preserve_turns=app_cfg.context.preserve_turns,
        provider=brain_cfg.llm.provider,
        cache_ttl=None,
    )
    builder.reload_boot_files()

    new_cfg = resolve_llm_config("llm/openrouter/anthropic-claude-haiku-4.5/thinking.yaml")
    if not isinstance(new_cfg, OpenRouterConfig):
        raise SystemExit("Expected OpenRouterConfig for current Haiku 4.5 profile.")
    old_cfg = _build_old_profile(new_cfg)

    old_client = create_client(old_cfg, transient_retries=1, request_timeout=180, retry_label="haiku45-old-eval")
    new_client = create_client(new_cfg, transient_retries=1, request_timeout=180, retry_label="haiku45-new-eval")
    judge_cfg = resolve_llm_config("llm/openrouter/anthropic-claude-sonnet-4.6/no-thinking.yaml")
    judge = create_client(judge_cfg, transient_retries=1, request_timeout=120, retry_label="haiku45-judge")

    summary = Counter()
    rows: list[dict[str, Any]] = []

    for sample_idx, sample in enumerate(samples, start=1):
        convo = Conversation()
        convo.replace_messages(sample.entries)
        messages = builder.build(convo)
        excerpt = [_entry_to_brief(entry) for entry in sample.entries[-8:]]
        actual_summary = _actual_to_text(sample.actual_turn)

        old_resp = old_client.chat_with_tools(messages, TOOL_DEFINITIONS, temperature=0)
        new_resp = new_client.chat_with_tools(messages, TOOL_DEFINITIONS, temperature=0)

        judgment = _judge_pair_ab(
            judge,
            excerpt=excerpt,
            actual_summary=actual_summary,
            old_result=CandidateResult("old", old_resp),
            new_result=CandidateResult("new", new_resp),
            seed=sample_idx,
        )

        summary[judgment["winner"]] += 1
        historical_tools = []
        for entry in sample.actual_turn[1:]:
            if entry.role == "assistant" and entry.tool_calls:
                historical_tools.extend(tc.name for tc in entry.tool_calls)
                break

        row = {
            "turn_index": sample.turn_index,
            "winner": judgment["winner"],
            "confidence": judgment["confidence"],
            "reason": judgment["reason"],
            "historical_tools": historical_tools,
            "old_tools": _tool_names(old_resp.tool_calls),
            "new_tools": _tool_names(new_resp.tool_calls),
            "old_completion_tokens": old_resp.completion_tokens,
            "new_completion_tokens": new_resp.completion_tokens,
        }
        rows.append(row)

        print(f"[{sample_idx}] turn={sample.turn_index} winner={judgment['winner']} confidence={judgment['confidence']}")
        print(f"  historical_tools={historical_tools}")
        print(f"  old_tools={row['old_tools']} new_tools={row['new_tools']}")
        print(f"  old_completion_tokens={row['old_completion_tokens']} new_completion_tokens={row['new_completion_tokens']}")
        print(f"  reason={judgment['reason']}")
        print()

    print("Summary")
    print(f"- session: {session_dir.name}")
    print(f"- samples: {len(samples)}")
    print(f"- old wins: {summary['old']}")
    print(f"- new wins: {summary['new']}")
    print(f"- ties: {summary['tie']}")
    print()
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
