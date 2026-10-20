#!/usr/bin/env python3
"""Verify prompt-cache stability with the current production classes.

This script uses:
- ``ContextBuilder`` from the current repo
- ``resolve_llm_config()`` + ``create_client()`` from the current repo
- OpenRouter Claude Haiku 4.5 from ``cfgs/llm/openrouter/anthropic-claude-haiku-4.5/thinking.yaml``

Scenarios:
1. Baseline: same-turn request rebuilt twice should hit cache strongly.
2. Runtime snapshot: rebuilding later still reuses the frozen ``current_local_time`` note.
3. Timing notice: delayed-turn notes stay on the latest turn and no longer poison raw/advanced paths.
4. Common ground: an overlay stored on the latest turn survives rebuilds without wrecking cache.

Usage:
    uv run python scripts/verify_cache_behavior.py
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from lincy.agent.responder import _advance_responder_cache_breakpoint
from lincy.context.builder import TURN_OVERLAY_METADATA_KEY, ContextBuilder
from lincy.context.conversation import Conversation
from lincy.core.config import resolve_llm_config
from lincy.core.schema import OpenRouterConfig
from lincy.llm.factory import create_client
from lincy.llm.schema import LLMResponse, Message
from lincy.timezone_utils import configure as configure_timezone

MODEL_CFG = resolve_llm_config("llm/openrouter/anthropic-claude-haiku-4.5/thinking.yaml")
if not isinstance(MODEL_CFG, OpenRouterConfig):
    raise SystemExit("Expected OpenRouterConfig for Claude Haiku 4.5.")
if not MODEL_CFG.api_key:
    raise SystemExit("OPENROUTER_API_KEY is missing.")

MODEL = MODEL_CFG.model
CACHE_TTL = "1h"

SYSTEM_FILLER = "Reference dossier. " + " ".join(
    f"Record {i}: habitat sector {i % 17}, observed depth {i * 3}m, "
    f"population estimate {i * 41}, first catalogued {1900 + i}."
    for i in range(1, 320)
)

BOOT_FILLER = "Core rules appendix. " + " ".join(
    f"Appendix {i}: policy {i} remains in force unless superseded by event {i + 7}."
    for i in range(1, 260)
)


def _make_client():
    return create_client(
        MODEL_CFG,
        transient_retries=0,
        request_timeout=MODEL_CFG.request_timeout,
        rate_limit_retries=0,
        retry_label="verify_cache_behavior",
    )


def _write_boot_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory" / "agent"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "context.md").write_text(BOOT_FILLER, encoding="utf-8")


def _make_builder(
    tmp_path: Path,
) -> ContextBuilder:
    builder = ContextBuilder(
        system_prompt="You are a concise test assistant. " + SYSTEM_FILLER,
        timezone="Asia/Taipei",
        agent_os_dir=tmp_path,
        boot_files=["memory/agent/context.md"],
        preserve_turns=8,
        provider="openrouter",
        cache_ttl=CACHE_TTL,
    )
    builder.reload_boot_files()
    return builder


def _make_basic_conversation() -> Conversation:
    conv = Conversation()
    conv.add(
        "user",
        "Summarize the key constraints in one short sentence.",
        timestamp=datetime(2026, 3, 24, 0, 0, tzinfo=timezone.utc),
        metadata={"turn_processing_started_at": "2026-03-24T17:11:00+08:00"},
    )
    return conv


def _make_delayed_turn_conversation(
    processing_started_at: str,
    turn_overlay: str | None = None,
) -> Conversation:
    history_blob = "Prior conversation digest. " + " ".join(
        f"Exchange {i}: reminder thread {i} stays open until task {i + 3} is confirmed complete."
        for i in range(1, 180)
    )
    conv = Conversation()
    conv.add(
        "user",
        "Earlier question. " + history_blob,
        timestamp=datetime(2026, 3, 23, 23, 30, tzinfo=timezone.utc),
    )
    conv.add(
        "assistant",
        "Earlier answer. " + history_blob,
        timestamp=datetime(2026, 3, 23, 23, 30, tzinfo=timezone.utc),
    )
    conv.add(
        "user",
        "[SCHEDULED]\nReason: delayed follow-up",
        channel="system",
        sender="system",
        timestamp=datetime(2026, 3, 23, 23, 50, tzinfo=timezone.utc),
        metadata={
            "turn_processing_started_at": processing_started_at,
            "turn_processing_delay_seconds": 48660,
            "turn_processing_delay_reason": "scheduled_turn",
            "turn_processing_stale": True,
            **({TURN_OVERLAY_METADATA_KEY: turn_overlay} if turn_overlay else {}),
        },
    )
    return conv


def _send(label: str, client, messages: list[Message]) -> LLMResponse:
    response = client.chat_with_tools(messages, [])
    read = response.cache_read_tokens or 0
    write = response.cache_write_tokens or 0
    prompt = response.prompt_tokens or 0
    ratio = (read / prompt * 100) if prompt else 0.0
    print(
        f"{label:>28}: prompt={prompt:>6} "
        f"cache_read={read:>6} cache_write={write:>6} "
        f"hit={ratio:>5.1f}% finish={response.finish_reason}"
    )
    return response


def _describe_bp3(messages: list[Message]) -> str:
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if message.role == "system":
            continue
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            if part.type == "text" and part.cache_control is not None:
                text = (part.text or "").splitlines()[0][:80]
                return f"idx={idx} role={message.role} text={text!r}"
    return "no cache-controlled non-system breakpoint found"


def _sleep() -> None:
    time.sleep(3.0)


def test_baseline_same_minute(tmp_path: Path, client) -> tuple[int, int]:
    print("\n=== Baseline: identical same-turn request ===")
    builder = _make_builder(tmp_path)
    conv = _make_basic_conversation()
    messages1 = _advance_responder_cache_breakpoint(builder.build(conv))
    print(f"  BP3: {_describe_bp3(messages1)}")
    r1 = _send("baseline call 1", client, messages1)
    _sleep()

    messages2 = _advance_responder_cache_breakpoint(builder.build(conv))
    r2 = _send("baseline call 2", client, messages2)
    return (r1.cache_read_tokens or 0, r2.cache_read_tokens or 0)


def test_runtime_context_snapshot(tmp_path: Path, client) -> tuple[int, int]:
    print("\n=== Runtime Context Snapshot ===")
    print("  Rebuilding later should reuse the same turn-start runtime note.")
    builder = _make_builder(tmp_path)
    conv = _make_basic_conversation()

    messages1 = _advance_responder_cache_breakpoint(builder.build(conv))
    messages2 = _advance_responder_cache_breakpoint(builder.build(conv))
    print(f"  rebuilt messages identical: {messages1 == messages2}")
    r1 = _send("runtime call 1", client, messages1)
    _sleep()
    r2 = _send("runtime call 2", client, messages2)
    return (r1.cache_read_tokens or 0, r2.cache_read_tokens or 0)


def test_timing_notice_paths(tmp_path: Path, client) -> tuple[int, int, int, int]:
    print("\n=== Timing Notice: raw builder vs responder-advanced ===")
    builder = _make_builder(tmp_path)

    conv = _make_delayed_turn_conversation("2026-03-24T17:11:00+08:00")

    raw_messages1 = builder.build(conv)
    raw_messages2 = builder.build(conv)
    print(f"  Raw BP3: {_describe_bp3(raw_messages1)}")
    raw1 = _send("timing raw call 1", client, raw_messages1)
    _sleep()
    raw2 = _send("timing raw call 2", client, raw_messages2)
    _sleep()

    adv_messages1 = _advance_responder_cache_breakpoint(builder.build(conv))
    adv_messages2 = _advance_responder_cache_breakpoint(builder.build(conv))
    print(f"  Adv BP3: {_describe_bp3(adv_messages1)}")
    adv1 = _send("timing adv call 1", client, adv_messages1)
    _sleep()
    adv2 = _send("timing adv call 2", client, adv_messages2)

    return (
        raw1.cache_read_tokens or 0,
        raw2.cache_read_tokens or 0,
        adv1.cache_read_tokens or 0,
        adv2.cache_read_tokens or 0,
    )


def test_common_ground_overlay(tmp_path: Path, client) -> tuple[int, int]:
    print("\n=== Common Ground: overlay stored on the latest turn ===")
    builder = _make_builder(tmp_path)
    conv = _make_delayed_turn_conversation(
        "2026-03-24T17:11:00+08:00",
        turn_overlay=(
            "[Common Ground at Message Time]\n\n"
            "scope_id: demo\n"
            "message_time_shared_rev: 4\n"
            "turn_start_shared_rev: 6\n\n"
            "The user had already been told in this conversation when they sent the current message:\n"
            "- rev 1: Remember the medication after dinner.\n"
            "- rev 4: Only send one short reminder."
        ),
    )

    overlaid1 = _advance_responder_cache_breakpoint(builder.build(conv))
    overlaid2 = _advance_responder_cache_breakpoint(builder.build(conv))
    print(f"  BP3: {_describe_bp3(overlaid1)}")
    r1 = _send("common-ground call 1", client, overlaid1)
    _sleep()
    r2 = _send("common-ground call 2", client, overlaid2)
    return (r1.cache_read_tokens or 0, r2.cache_read_tokens or 0)


def main() -> None:
    configure_timezone("Asia/Taipei")
    print("=" * 72)
    print("Prompt Cache Verification With Current Production Classes")
    print(f"Model: {MODEL}")
    print("=" * 72)

    client = _make_client()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_boot_files(tmp_path)

        baseline_1, baseline_2 = test_baseline_same_minute(tmp_path, client)
        runtime_1, runtime_2 = test_runtime_context_snapshot(tmp_path, client)
        raw_1, raw_2, adv_1, adv_2 = test_timing_notice_paths(tmp_path, client)
        cg_1, cg_2 = test_common_ground_overlay(tmp_path, client)

    print("\n=== Summary ===")
    print(f"baseline same-minute: call2 - call1 = {baseline_2 - baseline_1}")
    print(f"runtime snapshot rebuild: call2 - call1 = {runtime_2 - runtime_1}")
    print(f"timing raw path: call2 - call1 = {raw_2 - raw_1}")
    print(f"timing advanced path: call2 - call1 = {adv_2 - adv_1}")
    print(f"common-ground overlay: call2 - call1 = {cg_2 - cg_1}")

    print("\nInterpretation:")
    print("- If baseline call 2 is high, the general cache route is healthy.")
    print("- If runtime snapshot call 2 stays high, the builder no longer depends on wall-clock rebuild time.")
    print("- If both timing paths stay high, delayed-turn notes are no longer poisoning the cache prefix.")
    print("- If the common-ground overlay stays high, latest-turn overlays can survive responder rebuilds.")


if __name__ == "__main__":
    main()
