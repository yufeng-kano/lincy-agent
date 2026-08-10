"""Brain staged planning helpers (gather -> plan -> execute).

Stage 1: read-only tool gathering
Stage 2: planning with optional read-only gap-filling
Stage 3: execution happens in AgentCore via the normal responder loop
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import uuid
from typing import Any

from ..context.builder import ContextBuilder
from ..llm.base import LLMClient
from ..llm.schema import (
    ContentPart,
    LLMResponse,
    Message,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    make_tool_result_message,
)
from ..send_message_batch_guidance import build_stage2_message_economy_rule
from ..tools import ToolRegistry
from ..tools.registry import ToolResult
from .side_channel import run_side_channel_tool_loop
from .turn_overlay import DECISION_REMINDER_LABEL
from .ui_event_console import AgentUiPort

STAGE1_SYNTHETIC_TOOL_NAME = "_stage1_gather"

_STAGE1_USER_PROMPT = (
    "[SYSTEM] You are in Stage 1 of a 3-stage pipeline.\n"
    "Stage 1 gathers evidence only. Stage 2 decides what to do. Stage 3 executes.\n"
    "A full execution tool schema may be visible for prompt-cache parity, but in this stage "
    "you may only use read-only tools to inspect memory/files/history/images/web sources.\n"
    "You are not replying to the user in this stage.\n"
    "Do not draft user-facing messages. Do not send messages. Do not modify memory or schedules.\n"
    "When the next reply depends on current external facts (for example menu/item availability, store hours, prices, transit, weather, or schedules), gather evidence with web_search, web_fetch, or another read-only tool before concluding.\n"
    "If the latest user message corrects or contradicts a concrete claim, treat that as a red flag: verify it now or drop the contradicted claim from findings. Do not restate unverified contradicted facts.\n"
    "If prior [Stage 1 Findings] exist in conversation and remain relevant, "
    "you may reuse them and skip redundant searches.\n"
    "If you already know the likely reply or action, record that as findings for Stage 2/3 instead of calling tools.\n"
    "When you have sufficient information, stop calling tools."
)
_STAGE1_FORCE_MEMORY_SEARCH_PROMPT = (
    "[SYSTEM] Stage 1 gate: memory_search is required before any other action. "
    "Call memory_search now with a non-empty query based on the latest user message. "
    "Use concise keywords only."
)
_STAGE2_PLAN_PROMPT_TEMPLATE = (
    "[SYSTEM] Stage 2/3: planning first. Do not send messages.\n"
    "You may call read_file, web_search, or web_fetch only when Stage 1 findings are insufficient.\n"
    "Any other tool call will be rejected.\n"
    "Before output, perform ULTRA THINK internally to reason about current state and risks.\n"
    "Produce a complete plain-text execution plan for Stage 3:\n"
    "[CURRENT_STATE]\n"
    "- What is happening, key signals, confidence/uncertainty.\n"
    "- Model the exchange like a coherent human conversation: track what was already said, suggested, corrected, resolved, or made stale in the latest turn and recent turns.\n"
    "- Separate confirmed facts from inferences. Do not invent bridge events between different times, people, or commitments just to make the reply sound smoother.\n"
    "- Normalize any conflicting date/day/time claims into a single timeline before deciding.\n"
    "- In timeline conflicts, prefer the latest explicit user correction in the current turn over earlier summaries and older memory.\n"
    "- If a date/day pairing is impossible, or memory conflicts with the current turn, flag the conflict and either verify or speak with uncertainty; do not carry superseded facts into the plan.\n"
    "- If a concrete claim depends on current external reality (for example menu/item availability, opening hours, prices, transit, weather, or schedules), require evidence from Stage 1 or add an explicit verification step before reuse.\n"
    "- For time-sensitive replies, use the latest user timestamp as 'now'. If you derive a delay or target time, verify the arithmetic before planning any wording.\n"
    "[DECISION]\n"
    "- Act now or stay silent, and why.\n"
    "[ACTION_PLAN]\n"
    "- Exact tool actions to execute (or explicitly `none`).\n"
    "[FILE_UPDATE_PLAN]\n"
    "- Whether any files need updating.\n"
    "- For each file: path, reason, and suggested content.\n"
    "- Never target `memory/archive/` for live updates; archive is system-managed recall storage, not a write target.\n"
    "- Durable user instructions, bans, agreements, cross-day commitments, and future behavior constraints belong in `memory/agent/long-term.md`.\n"
    "- Current-turn context, temporary state, and recent emotional timeline belong in `memory/agent/temp-memory.md`.\n"
    "- Reusable tool/process lessons belong in `personal-skills/`.\n"
    "- Identity or relationship-boundary changes belong in `memory/agent/persona.md`.\n"
    "- If one turn contains both durable rules and short-term context, split them into the appropriate files instead of merging into one note.\n"
    "- If no file updates needed, explicitly `none`.\n"
    "[SCHEDULE_PLAN]\n"
    "- Whether to adjust schedule (batch_add/batch_remove/list) and why.\n"
    "[EXECUTION_RULES]\n"
    "- Constraints and guardrails for Stage 3 execution.\n"
    "- Preserve the resolved timeline from above; never revive an earlier fact that was invalidated by a later correction.\n"
    "- Before any send_message, check logical relationships across recent messages. Do not repeat the same clock-based reminder, recommendation, question, or factual claim unless new evidence, new urgency, or a user reply makes it meaningfully different.\n"
    "- Only mention people, locations, or activities that are explicitly supported by the current turn, gathered evidence, or memory. Do not turn a later pickup/meeting into a current shared meal or plan without evidence.\n"
    "- If the user just corrected a factual claim, acknowledge the correction or verify it first; never confidently repeat the contradicted claim.\n"
    "- If external facts remain unverified, Stage 3 must verify first (for example with web_search or web_fetch) or speak with explicit uncertainty instead of asserting.\n"
    "- Keep user-facing time natural. Do not expose internal clock math or exact wall-clock timestamps in casual chat unless the user asked for precision or conflict resolution requires it.\n"
    "- If you mention both a relative delay and an absolute time, they must match exactly; otherwise drop one or re-check the math before sending.\n"
    "{message_economy_rule}\n"
    "Use the facts gathered below to decide the next actions.\n\n"
    "[Stage 1 Findings]\n{findings}\n\n"
    "Build an execution plan for Stage 3."
)
_STAGE3_EXECUTION_PROMPT_TEMPLATE = (
    "[SYSTEM] Stage 3/3: execute according to the plan below.\n"
    "Follow the plan strictly; adjust only when necessary.\n"
    "Do not revive superseded date/day/time facts when newer corrections exist.\n"
    "Keep message logic coherent: do not restate the same reminder, recommendation, question, or factual claim across multiple send_message calls unless new information justifies it.\n"
    "If a concrete external fact is still unverified, verify it first or clearly hedge instead of asserting it.\n"
    "Do not leak internal time arithmetic or mismatched relative-plus-absolute time wording to the user.\n"
    "If deviating, final behavior must still align with user intent.\n\n"
    "[Stage 2 Plan]\n{plan_text}"
)
_PLAN_CONTEXT_HEADER = (
    "[SYSTEM] Planning context: "
    "prioritize these files when planning and executing."
)


@dataclass
class Stage1GatheringResult:
    transcript: str
    findings_text: str
    tool_calls: int
    final_response: LLMResponse


@dataclass
class Stage2PlanningResult:
    plan_text: str
    raw_response: str


def format_stage2_plan_for_tui(plan_text: str) -> str:
    return plan_text.strip()


def build_stage3_plan_overlay_message(plan_text: str) -> Message:
    return Message(
        role="system",
        content=_STAGE3_EXECUTION_PROMPT_TEMPLATE.format(plan_text=plan_text),
    )


def build_stage1_findings_overlay_message(findings_text: str) -> Message:
    return Message(
        role="system",
        content=f"[SYSTEM] Stage 1 findings for reference:\n{findings_text}",
    )


def build_stage1_findings_for_conversation(
    findings_text: str,
) -> tuple[ToolCall, str]:
    """Build synthetic tool call + result content for persisting findings."""
    call = ToolCall(
        id=f"stage1_{uuid.uuid4().hex[:8]}",
        name=STAGE1_SYNTHETIC_TOOL_NAME,
        arguments={},
    )
    return call, findings_text


def build_plan_context_message(files: list[tuple[str, str]]) -> Message | None:
    """Build a single system message embedding plan_context_files content.

    Each entry in *files* is (rel_path, file_content).
    Returns None when the list is empty.
    """
    if not files:
        return None
    sections = [
        f'<file path="{rel_path}">\n{content.rstrip()}\n</file>'
        for rel_path, content in files
    ]
    return Message(
        role="system",
        content=f"{_PLAN_CONTEXT_HEADER}\n" + "\n".join(sections),
    )


class _Stage1RegistryProxy:
    """Read-only execution proxy for Stage 1 tool calls."""

    def __init__(self, base_registry: ToolRegistry, allowed_tool_names: set[str]):
        self._base = base_registry
        self._allowed = allowed_tool_names
        self._seen_memory_search_results: set[str] = set()

    def has_tool(self, name: str) -> bool:
        return name in self._allowed and self._base.has_tool(name)

    def is_forbidden_action(self, tool_call: ToolCall) -> bool:
        if tool_call.name not in self._allowed:
            return True
        if tool_call.name == "schedule_action":
            return tool_call.arguments.get("action") != "list"
        return False

    def execute(self, tool_call: ToolCall) -> ToolResult:
        if tool_call.name not in self._allowed:
            return ToolResult(
                "Error: Stage 1 is read-only. "
                f"Tool '{tool_call.name}' is not allowed. "
                "If you intended to act, summarize that intended action as findings "
                "for Stage 3 and stop calling tools.",
                is_error=True,
            )
        if tool_call.name == "schedule_action":
            action = tool_call.arguments.get("action")
            if action != "list":
                return ToolResult(
                    "Error: Stage 1 is read-only. schedule_action only supports "
                    "action='list'. If you intended to reschedule something, record "
                    "that intent in findings for Stage 3 instead of calling tools now.",
                    is_error=True,
                )
        result = self._base.execute(tool_call)
        if tool_call.name != "memory_search" or result.is_error:
            return result

        result_key = _stage1_memory_search_result_key(result.content)
        if result_key in self._seen_memory_search_results:
            return ToolResult(
                "Error: same result as previous search, refine query or stop",
                is_error=True,
            )

        self._seen_memory_search_results.add(result_key)
        return result


class _Stage2RegistryProxy:
    """Read-only execution proxy for Stage 2 planning tool calls."""

    def __init__(self, base_registry: ToolRegistry, allowed_tool_names: set[str]):
        self._base = base_registry
        self._allowed = allowed_tool_names

    def is_forbidden_action(self, tool_call: ToolCall) -> bool:
        return tool_call.name not in self._allowed

    def execute(self, tool_call: ToolCall) -> ToolResult:
        if tool_call.name not in self._allowed:
            return ToolResult(
                "Error: Stage 2 planning only allows read_file, web_search, and web_fetch. "
                f"Tool '{tool_call.name}' is not allowed. "
                "Revise the plan using gathered evidence instead of executing actions now.",
                is_error=True,
            )
        return self._base.execute(tool_call)


def build_stage1_tools(all_tools: list[ToolDefinition]) -> list[ToolDefinition]:
    by_name = {tool.name: tool for tool in all_tools}
    names = [
        "memory_search",
        "web_search",
        "web_fetch",
        "read_file",
        "get_channel_history",
        "read_image",
        "read_image_by_subagent",
    ]
    selected: list[ToolDefinition] = []
    for name in names:
        tool = by_name.get(name)
        if tool is not None:
            selected.append(tool)

    if "schedule_action" in by_name:
        selected.append(_schedule_action_list_only_definition(by_name["schedule_action"]))
    return selected


def build_stage2_tools(all_tools: list[ToolDefinition]) -> list[ToolDefinition]:
    by_name = {tool.name: tool for tool in all_tools}
    names = [
        "read_file",
        "web_search",
        "web_fetch",
    ]
    selected: list[ToolDefinition] = []
    for name in names:
        tool = by_name.get(name)
        if tool is not None:
            selected.append(tool)
    return selected


def _scrub_stage1_messages(messages: list[Message]) -> list[Message]:
    """Remove action-oriented reminders that mis-prime Stage 1 gathering."""
    reminder_blocks = list(ContextBuilder.channel_reminder_variants())
    reminder_blocks.extend(ContextBuilder._GENERAL_REMINDERS.values())
    decision_marker = f"\n\n{DECISION_REMINDER_LABEL}\n"

    scrubbed: list[Message] = []
    for msg in messages:
        if msg.role != "user" or not isinstance(msg.content, str):
            scrubbed.append(msg)
            continue

        content = msg.content
        for reminder in reminder_blocks:
            content = content.replace(f"\n{reminder}", "")
        if decision_marker in content:
            content = content.split(decision_marker, 1)[0]
        if content == msg.content:
            scrubbed.append(msg)
        else:
            scrubbed.append(msg.model_copy(update={"content": content}))
    return scrubbed


def run_stage1_information_gathering(
    *,
    client: LLMClient,
    messages: list[Message],
    all_tools: list[ToolDefinition],
    registry: ToolRegistry,
    console: AgentUiPort,
    raise_if_cancel_requested: Callable[[], None] | None = None,
    max_iterations: int = 4,
    skip_memory_search_gate: bool = False,
) -> Stage1GatheringResult:
    stage1_tools = build_stage1_tools(all_tools)
    if not stage1_tools:
        return Stage1GatheringResult(
            transcript="(no stage1 tools available)",
            findings_text="(no stage1 tools available)",
            tool_calls=0,
            final_response=LLMResponse(content=None, tool_calls=[]),
        )

    local_messages = [
        *_scrub_stage1_messages(messages),
        Message(role="user", content=_STAGE1_USER_PROMPT),
    ]
    proxy = _Stage1RegistryProxy(
        registry,
        allowed_tool_names={tool.name for tool in stage1_tools},
    )
    lines: list[str] = []
    total_tool_calls = 0
    iterations = 0
    response = LLMResponse(content=None, tool_calls=[])

    if skip_memory_search_gate:
        initial_memory_search_done = True
    else:
        requires_initial_memory_search = any(
            tool.name == "memory_search" for tool in stage1_tools
        )
        initial_memory_search_done = not requires_initial_memory_search

    while True:
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        with console.spinner("Stage 1/3: gathering..."):
            response = client.chat_with_tools(local_messages, all_tools)
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        iterations += 1
        if response.content and response.content.strip():
            lines.append("[assistant]")
            lines.append(response.content.strip())

        if not initial_memory_search_done:
            gate_error = _validate_initial_memory_search_call(response)
            if gate_error is not None:
                lines.append(f"[stage1-gate] {gate_error}")
                local_messages.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        reasoning_content=response.reasoning_content,
                        reasoning_details=response.reasoning_details,
                        tool_calls=response.tool_calls,
                    )
                )
                local_messages.append(
                    Message(role="user", content=_STAGE1_FORCE_MEMORY_SEARCH_PROMPT),
                )
                if iterations >= max(1, max_iterations):
                    lines.append(f"[stage1] reached max iterations={max(1, max_iterations)}")
                    break
                continue
            initial_memory_search_done = True

        if not response.has_tool_calls():
            break
        local_messages.append(
            Message(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                reasoning_details=response.reasoning_details,
                tool_calls=response.tool_calls,
            ),
        )
        stop_after_forbidden_action = False
        for tool_call in response.tool_calls:
            total_tool_calls += 1
            console.print_tool_call(tool_call)
            result = proxy.execute(tool_call)
            console.print_tool_result(tool_call, result.content)
            result_preview = _result_to_preview_text(result.content)
            lines.append(f"[tool_call] {tool_call.name} {json.dumps(tool_call.arguments, ensure_ascii=False)}")
            lines.append(f"[tool_result] {result_preview}")
            if proxy.is_forbidden_action(tool_call):
                lines.append(
                    "[stage1-intent] "
                    f"Attempted {tool_call.name} {json.dumps(tool_call.arguments, ensure_ascii=False)}. "
                    "Capture this as execution intent for Stage 3 and stop Stage 1."
                )
                stop_after_forbidden_action = True
                break
            local_messages.append(
                make_tool_result_message(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=result.content,
                )
            )
        if stop_after_forbidden_action:
            break
        if iterations >= max(1, max_iterations):
            lines.append(f"[stage1] reached max iterations={max(1, max_iterations)}")
            break

    transcript = "\n".join(lines).strip() or "(no stage1 transcript)"
    return Stage1GatheringResult(
        transcript=transcript,
        findings_text=transcript,
        tool_calls=total_tool_calls,
        final_response=response,
    )


def run_stage2_brain_planning(
    *,
    client: LLMClient,
    messages: list[Message],
    stage1: Stage1GatheringResult,
    all_tools: list[ToolDefinition],
    registry: ToolRegistry,
    console: AgentUiPort,
    raise_if_cancel_requested: Callable[[], None] | None = None,
    send_message_batch_guidance: bool = False,
    max_iterations: int = 3,
) -> Stage2PlanningResult | None:
    stage2_tools = build_stage2_tools(all_tools)
    proxy = _Stage2RegistryProxy(
        registry,
        allowed_tool_names={tool.name for tool in stage2_tools},
    )
    user_prompt = _STAGE2_PLAN_PROMPT_TEMPLATE.format(
        findings=stage1.findings_text,
        message_economy_rule=build_stage2_message_economy_rule(
            enabled=send_message_batch_guidance,
        ),
    )
    local_messages = [*messages, Message(role="user", content=user_prompt)]
    iterations = 0

    while True:
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        with console.spinner("Stage 2/3: planning..."):
            response = client.chat_with_tools(local_messages, all_tools)
        if raise_if_cancel_requested is not None:
            raise_if_cancel_requested()
        iterations += 1

        if not response.has_tool_calls():
            plan_text = (response.content or "").strip()
            if plan_text:
                return Stage2PlanningResult(
                    plan_text=plan_text,
                    raw_response=response.content or "",
                )
            if console.debug:
                console.print_debug("staged-plan", "stage2 planning failed: empty response")
            return None

        local_messages.append(
            Message(
                role="assistant",
                content=response.content,
                reasoning_content=response.reasoning_content,
                reasoning_details=response.reasoning_details,
                tool_calls=response.tool_calls,
            )
        )
        for tool_call in response.tool_calls:
            console.print_tool_call(tool_call)
            result = proxy.execute(tool_call)
            console.print_tool_result(tool_call, result.content)
            local_messages.append(
                make_tool_result_message(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    content=result.content,
                )
            )

        if iterations >= max(1, max_iterations):
            if console.debug:
                console.print_debug(
                    "staged-plan",
                    f"stage2 planning failed: reached max iterations={max(1, max_iterations)}",
                )
            return None


def _schedule_action_list_only_definition(source: ToolDefinition) -> ToolDefinition:
    return ToolDefinition(
        name=source.name,
        description=(
            "Read-only list of pending scheduled actions. "
            "Stage 1 only supports action='list'."
        ),
        parameters={
            "action": ToolParameter(
                type="string",
                description="Must be 'list' in Stage 1.",
                enum=["list"],
            ),
        },
        required=["action"],
    )


def _result_to_preview_text(result: str | list[ContentPart]) -> str:
    if isinstance(result, list):
        text_parts = [
            part.text for part in result
            if part.type == "text" and part.text
        ]
        return "\n".join(text_parts).strip() or "(multimodal tool result)"
    return str(result).strip()


def _stage1_memory_search_result_key(result: str | list[ContentPart]) -> str:
    if isinstance(result, list):
        return json.dumps(
            [part.model_dump(mode="json") for part in result],
            ensure_ascii=False,
            sort_keys=True,
        )
    return str(result).strip()


def _validate_initial_memory_search_call(response: LLMResponse) -> str | None:
    if not response.has_tool_calls():
        return "missing required initial memory_search tool call."
    first = response.tool_calls[0]
    if first.name != "memory_search":
        return "first tool call must be memory_search."
    query = _extract_memory_search_query(first.arguments)
    if not isinstance(query, str) or not query.strip():
        return "initial memory_search query must be non-empty."
    return None


def _extract_memory_search_query(arguments: dict[str, Any]) -> Any:
    return arguments.get("query") or arguments.get("q") or arguments.get("search")
