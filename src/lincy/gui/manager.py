"""GUI Manager: AX-first computer-use agent loop.

The manager LLM drives a local OpenComputerUse MCP server (accessibility
tree + window screenshot + background input) and sees tool results
directly, including screenshots. There is no separate vision worker in
this loop; coordinate clicks are the built-in fallback of the same
`click` tool for elements without an accessible surface.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..context.cache_breakpoints import advance_cache_breakpoint
from ..llm.base import LLMClient
from ..llm.schema import (
    ContentPart,
    Message,
    ToolCall,
    ToolDefinition,
    ToolParameter,
    make_tool_result_message,
)
from ..tools.registry import ToolRegistry, ToolResult
from .actions import activate_app
from .mcp_client import MCPError, MCPStdioClient

if TYPE_CHECKING:
    from .session import GUISessionStore

logger = logging.getLogger(__name__)

_MAX_STEPS = 20
_WAIT_CANCEL_POLL_SECONDS = 0.1
_DEFAULT_TOOL_TIMEOUT = 90.0
_STALE_STUB_SUFFIX = (
    "[stale state: screenshot removed; element indexes no longer valid; "
    "call get_app_state for fresh state]"
)

_MAX_STEPS_REPORT_PROMPT = (
    "You have reached the step limit. No tools are available. "
    "Respond with TEXT ONLY (no tool calls).\n\n"
    "Write a concise situation report covering:\n"
    "1. What was accomplished so far.\n"
    "2. What remains to be done.\n"
    "3. Whether the task seems feasible with more steps, "
    "or if the current approach is fundamentally wrong.\n\n"
    "Be specific and factual. Reference the last app state you observed."
)

# Callback: (tool_call, result, step, max_steps, step_elapsed, total_elapsed, worker_timing)
# worker_timing is always None in the AX-first loop; the slot is kept so the
# CLI step-printer signature stays unchanged.
GUIStepCallback = Callable[
    [ToolCall, str, int, int, float, float, dict[str, float] | None], None,
]

# --- MCP-backed tool definitions (mirror the pinned server's schemas) ---

_APP_PARAM = ToolParameter(
    type="string",
    description=(
        "Target app: English app name or bundle id (e.g. 'Calculator', "
        "'com.apple.finder'). Use names exactly as returned by list_apps."
    ),
)

_LIST_APPS_DEF = ToolDefinition(
    name="list_apps",
    description=(
        "List apps on this computer (running state, bundle id, usage). "
        "Use it to discover the exact app name for the other tools."
    ),
    parameters={},
    required=[],
)

_GET_APP_STATE_DEF = ToolDefinition(
    name="get_app_state",
    description=(
        "Start or resume controlling an app (launching it if needed) and "
        "return its key window as an indexed accessibility tree plus a "
        "window screenshot. Element indexes in the tree are the handles "
        "for click/scroll/set_value/perform_secondary_action."
    ),
    parameters={
        "app": _APP_PARAM,
        "max_tree_nodes": ToolParameter(
            type="integer",
            description="Optional cap on tree nodes returned.",
        ),
        "max_tree_depth": ToolParameter(
            type="integer",
            description="Optional cap on tree depth returned.",
        ),
        "text_limit": ToolParameter(
            type="integer",
            description=(
                "Optional cap on characters per text value in the tree: a "
                "positive integer, or the string \"max\" for untruncated text. "
                "Raise it when you need to read long visible text in full."
            ),
            json_schema={"type": ["integer", "string"]},
        ),
    },
    required=["app"],
)

_CLICK_DEF = ToolDefinition(
    name="click",
    description=(
        "Click an element by element_index from the MOST RECENT app state, "
        "or by window pixel coordinates (x, y) as fallback when the target "
        "has no accessible element. Returns fresh app state."
    ),
    parameters={
        "app": _APP_PARAM,
        "element_index": ToolParameter(
            type="string",
            description="Element index from the latest app state tree.",
        ),
        "x": ToolParameter(
            type="number",
            description="Fallback: window-local X pixel coordinate.",
        ),
        "y": ToolParameter(
            type="number",
            description="Fallback: window-local Y pixel coordinate.",
        ),
        "mouse_button": ToolParameter(
            type="string",
            description="'left' (default) or 'right' for context menus.",
        ),
        "click_count": ToolParameter(
            type="integer",
            description="1 (default) or 2 for double-click.",
        ),
    },
    required=["app"],
)

_DRAG_DEF = ToolDefinition(
    name="drag",
    description="Drag from one window pixel coordinate to another.",
    parameters={
        "app": _APP_PARAM,
        "from_x": ToolParameter(type="number", description="Start X."),
        "from_y": ToolParameter(type="number", description="Start Y."),
        "to_x": ToolParameter(type="number", description="End X."),
        "to_y": ToolParameter(type="number", description="End Y."),
    },
    required=["app", "from_x", "from_y", "to_x", "to_y"],
)

_SCROLL_DEF = ToolDefinition(
    name="scroll",
    description=(
        "Scroll a scrollable element by a number of pages. "
        "direction: 'up', 'down', 'left', 'right'."
    ),
    parameters={
        "app": _APP_PARAM,
        "element_index": ToolParameter(
            type="string",
            description="Scrollable element index from the latest app state.",
        ),
        "direction": ToolParameter(
            type="string",
            description="'up', 'down', 'left' or 'right'.",
        ),
        "pages": ToolParameter(
            type="number",
            description="Number of pages to scroll (default 1).",
        ),
    },
    required=["app", "element_index", "direction"],
)

_TYPE_TEXT_DEF = ToolDefinition(
    name="type_text",
    description=(
        "Type literal text via keyboard events into the target app "
        "(focus the field first). Supports newlines and Unicode."
    ),
    parameters={
        "app": _APP_PARAM,
        "text": ToolParameter(type="string", description="Text to type."),
    },
    required=["app", "text"],
)

_PRESS_KEY_DEF = ToolDefinition(
    name="press_key",
    description=(
        "Press a key or key-combination (e.g. 'Return', 'Escape', "
        "'Command+S', 'Down')."
    ),
    parameters={
        "app": _APP_PARAM,
        "key": ToolParameter(type="string", description="Key or combo."),
    },
    required=["app", "key"],
)

_SET_VALUE_DEF = ToolDefinition(
    name="set_value",
    description=(
        "Set the value of a settable element (text field, slider, "
        "checkbox) directly. Preferred over type_text for text fields "
        "marked settable in the tree."
    ),
    parameters={
        "app": _APP_PARAM,
        "element_index": ToolParameter(
            type="string",
            description="Settable element index from the latest app state.",
        ),
        "value": ToolParameter(type="string", description="New value."),
    },
    required=["app", "element_index", "value"],
)

_SECONDARY_ACTION_DEF = ToolDefinition(
    name="perform_secondary_action",
    description=(
        "Invoke a secondary accessibility action listed for an element in "
        "the tree (e.g. 'Raise', 'Expand', 'Increment', 'zoom the window')."
    ),
    parameters={
        "app": _APP_PARAM,
        "element_index": ToolParameter(
            type="string",
            description="Element index from the latest app state.",
        ),
        "action": ToolParameter(
            type="string",
            description="Action name exactly as listed in the tree.",
        ),
    },
    required=["app", "element_index", "action"],
)

MCP_TOOL_DEFS = [
    _LIST_APPS_DEF,
    _GET_APP_STATE_DEF,
    _CLICK_DEF,
    _DRAG_DEF,
    _SCROLL_DEF,
    _TYPE_TEXT_DEF,
    _PRESS_KEY_DEF,
    _SET_VALUE_DEF,
    _SECONDARY_ACTION_DEF,
]

# --- Local loop-control tool definitions ---

_WAIT_DEF = ToolDefinition(
    name="wait",
    description=(
        "Wait for a given number of seconds (0.1-10). "
        "Use after actions that trigger loading or transitions."
    ),
    parameters={
        "seconds": ToolParameter(type="number", description="Seconds to wait."),
    },
    required=["seconds"],
)

_DONE_DEF = ToolDefinition(
    name="done",
    description="Signal that the GUI task has been completed successfully.",
    parameters={
        "summary": ToolParameter(
            type="string",
            description="Brief summary of what was accomplished.",
        ),
        "report": ToolParameter(
            type="string",
            description="Detailed report of findings or results for the caller.",
        ),
    },
    required=["summary"],
)

_FAIL_DEF = ToolDefinition(
    name="fail",
    description="Signal that the GUI task could not be completed.",
    parameters={
        "reason": ToolParameter(
            type="string",
            description="Why the task failed.",
        ),
        "report": ToolParameter(
            type="string",
            description="Detailed report of what was attempted before failure.",
        ),
    },
    required=["reason"],
)

_REPORT_PROBLEM_DEF = ToolDefinition(
    name="report_problem",
    description=(
        "Report an obstacle that prevents progress and return control to the caller. "
        "Use this when you cannot find a target after 2-3 attempts, "
        "encounter an unexpected state, or need different instructions. "
        "The caller may provide corrected instructions and retry."
    ),
    parameters={
        "problem": ToolParameter(
            type="string",
            description="What went wrong and what you tried.",
        ),
        "report": ToolParameter(
            type="string",
            description="Detailed context for the caller.",
        ),
    },
    required=["problem"],
)

MANAGER_TOOLS = MCP_TOOL_DEFS + [
    _WAIT_DEF,
    _DONE_DEF,
    _FAIL_DEF,
    _REPORT_PROBLEM_DEF,
]

_MCP_TOOL_NAMES = {t.name for t in MCP_TOOL_DEFS}


class GUITaskResult(BaseModel):
    """Result of a GUI task execution."""

    success: bool
    summary: str
    report: str = ""
    needs_input: bool = False
    session_id: str = ""
    steps_used: int
    elapsed_sec: float = 0.0
    screenshot_path: str = ""


class _LoopTermination(BaseModel):
    """Internal signal to stop the agentic loop."""

    success: bool
    summary: str
    report: str = ""
    needs_input: bool = False


class _GUICommandCancelled(Exception):
    """Raised when a GUI task is cancelled by the user."""


class GUIManager:
    """Orchestrates GUI automation via an agentic tool loop.

    The manager LLM calls MCP-backed AX tools (get_app_state, click, ...)
    plus local loop-control tools, and loops until done/fail or max_steps.
    An MCP server subprocess is spawned per task and closed afterwards.
    """

    def __init__(
        self,
        client: LLMClient,
        mcp_factory: Callable[[], MCPStdioClient],
        system_prompt: str,
        max_steps: int = _MAX_STEPS,
        session_store: GUISessionStore | None = None,
        on_step: GUIStepCallback | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
        allow_wait_tool: bool = True,
        step_delay_min: float = 0.0,
        step_delay_max: float = 0.0,
        keep_full_states: int = 2,
        stale_text_max_chars: int = 2000,
        max_tree_nodes: int | None = None,
        max_tree_depth: int | None = None,
        tool_timeout: float = _DEFAULT_TOOL_TIMEOUT,
        cache_control: dict[str, str] | None = None,
    ):
        self.client = client
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.session_store = session_store
        self.on_step = on_step
        self._mcp_factory = mcp_factory
        self._mcp: MCPStdioClient | None = None
        self._is_cancel_requested = is_cancel_requested
        self._step_delay_min = step_delay_min
        self._step_delay_max = max(step_delay_max, step_delay_min)
        self._keep_full_states = max(keep_full_states, 1)
        self._stale_text_max_chars = max(stale_text_max_chars, 200)
        self._max_tree_nodes = max_tree_nodes
        self._max_tree_depth = max_tree_depth
        self._tool_timeout = tool_timeout
        self._cache_control = cache_control
        self._last_screenshot = None
        self._capture_temp = os.path.join(
            tempfile.gettempdir(), f"lincy_capture_{os.getpid()}.png",
        )
        tools = list(MANAGER_TOOLS)
        if not allow_wait_tool:
            tools = [t for t in tools if t.name != "wait"]
        self._tools: list[ToolDefinition] = tools

    @property
    def capture_dir(self) -> str:
        """Directory holding the final-screenshot file of GUI tasks."""
        return os.path.dirname(self._capture_temp)

    def execute_task(
        self,
        intent: str,
        session_id: str | None = None,
        app_prompt_text: str | None = None,
    ) -> GUITaskResult:
        """Execute a GUI task. Brain calls this once; runs full loop internally.

        If session_id is given and session_store is available, resumes from
        the previous session's recorded steps.

        If app_prompt_text is provided, it is appended to the system prompt
        as app-specific context for this execution only.
        """
        from .session import GUIStepRecord

        # Session handling
        gui_session_id = ""
        resume_context = ""
        resume_last_app = ""
        if self.session_store is not None:
            if session_id:
                session_data = self.session_store.load(session_id)
                gui_session_id = session_data.session_id
                resume_context = self.session_store.format_steps_as_context(session_data)
                resume_last_app = session_data.last_active_app
            else:
                session_data = self.session_store.create(intent)
                gui_session_id = session_data.session_id

        system_content = self.system_prompt
        if app_prompt_text:
            system_content += "\n\n## App-Specific Guide\n\n" + app_prompt_text

        messages = [Message(
            role="system",
            content=system_content,
            cache_control=self._cache_control,
        )]

        if resume_context:
            if resume_last_app:
                try:
                    activate_app(resume_last_app)
                    time.sleep(0.5)
                except Exception:
                    logger.warning("Failed to re-activate: %s", resume_last_app)
            messages.append(Message(
                role="user",
                content=(
                    f"{resume_context}\n\n"
                    "You are resuming a previous task. "
                    "Do NOT repeat already-completed steps. "
                    "Call get_app_state first to observe the current state.\n\n"
                    f"New instruction: {intent}"
                ),
            ))
        else:
            messages.append(Message(role="user", content=f"GUI TASK: {intent}"))

        steps = 0
        task_start = time.monotonic()
        step_start = time.monotonic()
        self._last_screenshot = None
        try:
            self._mcp = self._mcp_factory()
            self._mcp.initialize()
        except MCPError as e:
            logger.error("MCP server unavailable: %s", e)
            self._close_mcp()
            return self._finalize_result(
                gui_session_id=gui_session_id,
                task_start=task_start,
                steps=0,
                success=False,
                summary=f"GUI backend unavailable: {e}",
            )

        registry = self._build_registry()
        try:
            self._raise_if_cancel_requested()
            response = self.client.chat_with_tools(
                advance_cache_breakpoint(messages),
                self._tools,
            )
            self._raise_if_cancel_requested()

            while response.has_tool_calls() and steps < self.max_steps:
                self._raise_if_cancel_requested()
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                termination: _LoopTermination | None = None
                for tool_call in response.tool_calls:
                    self._raise_if_cancel_requested()
                    term = self._check_terminal(tool_call)
                    if term is not None:
                        termination = term
                        elapsed = time.monotonic() - step_start
                        total = time.monotonic() - task_start
                        self._notify_step(
                            tool_call, term.summary, steps + 1, elapsed, total,
                        )
                        messages.append(make_tool_result_message(
                            tool_call_id=tool_call.id,
                            name=tool_call.name,
                            content=term.summary,
                        ))
                        continue

                    tool_result = self._execute_tool(registry, tool_call)
                    content = tool_result.content
                    elapsed = time.monotonic() - step_start
                    total = time.monotonic() - task_start

                    if isinstance(content, list):
                        result_str = _first_text_line(content)
                        messages.append(make_tool_result_message(
                            tool_call_id=tool_call.id,
                            name=tool_call.name,
                            content=content,
                        ))
                    else:
                        result_str = str(content)
                        messages.append(make_tool_result_message(
                            tool_call_id=tool_call.id,
                            name=tool_call.name,
                            content=result_str,
                        ))
                    steps += 1
                    self._notify_step(tool_call, result_str, steps, elapsed, total)

                    if self.session_store is not None:
                        step_record = GUIStepRecord(
                            tool=tool_call.name,
                            args=tool_call.arguments,
                            result=result_str,
                        )
                        try:
                            self.session_store.append_step(gui_session_id, step_record)
                        except Exception:
                            logger.warning("Failed to record GUI step")

                    self._raise_if_cancel_requested()
                    self._sleep_after_step()
                    step_start = time.monotonic()

                if termination is not None:
                    return self._finalize_result(
                        gui_session_id=gui_session_id,
                        task_start=task_start,
                        steps=steps,
                        success=termination.success,
                        summary=termination.summary,
                        report=termination.report,
                        needs_input=termination.needs_input,
                    )
                self._collapse_stale_states(messages)
                self._raise_if_cancel_requested()
                response = self.client.chat_with_tools(
                    advance_cache_breakpoint(messages),
                    self._tools,
                )
                self._raise_if_cancel_requested()

            # Loop ended without done/fail
            summary: str
            report = ""
            if steps >= self.max_steps:
                summary = f"Exceeded max steps ({self.max_steps})"
                report = self._request_situation_report(messages)
            else:
                summary = response.content or "Task ended without explicit completion signal."
            return self._finalize_result(
                gui_session_id=gui_session_id,
                task_start=task_start,
                steps=steps,
                success=False,
                summary=summary,
                report=report,
            )
        except _GUICommandCancelled:
            return self._finalize_result(
                gui_session_id=gui_session_id,
                task_start=task_start,
                steps=steps,
                success=False,
                summary="Cancelled by user.",
            )
        finally:
            self._close_mcp()

    # --- context management ---

    def _collapse_stale_states(self, messages: list[Message]) -> None:
        """Prune all but the newest K multimodal tool results.

        Screenshots and oversized trees dominate token usage, but text the
        agent already observed may still matter for read/report tasks, so
        stale states drop their image and keep text up to a cap instead of
        being reduced to one line. Only the message that ages out changes
        each turn, so the already-pruned prefix stays byte-stable for
        prompt caching.
        """
        state_indexes = [
            i for i, m in enumerate(messages)
            if m.role == "tool" and isinstance(m.content, list)
        ]
        for i in state_indexes[:-self._keep_full_states]:
            text = "\n".join(
                part.text for part in messages[i].content
                if part.type == "text" and part.text
            )
            if len(text) > self._stale_text_max_chars:
                text = text[:self._stale_text_max_chars] + "\n...[text truncated]"
            messages[i].content = f"{text}\n{_STALE_STUB_SUFFIX}"

    # --- helpers (unchanged loop mechanics) ---

    def _close_mcp(self) -> None:
        if self._mcp is not None:
            try:
                self._mcp.close()
            except Exception:
                logger.warning("Failed to close MCP client", exc_info=True)
            self._mcp = None

    def _notify_step(
        self,
        tool_call: ToolCall,
        result: str,
        step: int,
        elapsed_sec: float,
        total_elapsed_sec: float,
    ) -> None:
        """Invoke on_step callback, swallowing any exceptions."""
        if self.on_step is None:
            return
        try:
            self.on_step(
                tool_call, result, step, self.max_steps,
                elapsed_sec, total_elapsed_sec, None,
            )
        except Exception:
            logger.warning("on_step callback failed for step %d", step)

    def _raise_if_cancel_requested(self) -> None:
        """Abort GUI loop when a user interrupt request is pending."""
        if self._is_cancel_requested is not None and self._is_cancel_requested():
            raise _GUICommandCancelled

    def _sleep_with_cancel(self, seconds: float) -> None:
        """Sleep while remaining responsive to cancellation."""
        if seconds <= 0:
            return
        if self._is_cancel_requested is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while True:
            self._raise_if_cancel_requested()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(_WAIT_CANCEL_POLL_SECONDS, remaining))

    def _sleep_after_step(self) -> None:
        """Apply the configured pacing delay after each non-terminal tool step."""
        if self._step_delay_max <= 0:
            return
        self._sleep_with_cancel(
            random.uniform(self._step_delay_min, self._step_delay_max),
        )

    def _request_situation_report(self, messages: list[Message]) -> str:
        """One extra LLM call (no tools) to get a situation report on max-steps."""
        try:
            self._raise_if_cancel_requested()
            self._collapse_stale_states(messages)
            messages.append(Message(
                role="user",
                content=_MAX_STEPS_REPORT_PROMPT,
            ))
            response = self.client.chat_with_tools(
                advance_cache_breakpoint(messages),
                [],
            )
            self._raise_if_cancel_requested()
            return response.content or ""
        except _GUICommandCancelled:
            raise
        except Exception:
            logger.warning("Failed to get max-steps situation report")
            return ""

    def _finalize_result(
        self,
        *,
        gui_session_id: str,
        task_start: float,
        steps: int,
        success: bool,
        summary: str,
        report: str = "",
        needs_input: bool = False,
    ) -> GUITaskResult:
        """Finalize GUI session persistence and build a stable result object."""
        if self.session_store is not None:
            try:
                self.session_store.finalize(
                    gui_session_id,
                    success=success,
                    summary=summary,
                    report=report,
                )
            except Exception:
                logger.warning("Failed to finalize GUI session")
        return GUITaskResult(
            success=success,
            summary=summary,
            report=report,
            needs_input=needs_input,
            session_id=gui_session_id,
            steps_used=steps,
            elapsed_sec=time.monotonic() - task_start,
            screenshot_path=self._save_last_screenshot(),
        )

    def _save_last_screenshot(self) -> str:
        """Persist the newest MCP screenshot so the caller can read it."""
        if self._last_screenshot is None:
            return ""
        try:
            with open(self._capture_temp, "wb") as f:
                f.write(base64.b64decode(self._last_screenshot.data))
            return self._capture_temp
        except Exception:
            logger.warning("Failed to save final GUI screenshot", exc_info=True)
            return ""

    def _check_terminal(self, tool_call: ToolCall) -> _LoopTermination | None:
        """Check if a tool call is a termination signal (done/fail/report_problem)."""
        if tool_call.name == "done":
            return _LoopTermination(
                success=True,
                summary=tool_call.arguments.get("summary", "Task completed."),
                report=tool_call.arguments.get("report", ""),
            )
        if tool_call.name == "fail":
            return _LoopTermination(
                success=False,
                summary=tool_call.arguments.get("reason", "Task failed."),
                report=tool_call.arguments.get("report", ""),
            )
        if tool_call.name == "report_problem":
            return _LoopTermination(
                success=False,
                summary=tool_call.arguments.get("problem", "Problem reported."),
                report=tool_call.arguments.get("report", ""),
                needs_input=True,
            )
        return None

    def _execute_tool(
        self,
        registry: ToolRegistry,
        tool_call: ToolCall,
    ) -> ToolResult:
        """Execute a non-terminal tool call, catching errors."""
        try:
            return registry.execute(tool_call)
        except _GUICommandCancelled:
            raise
        except Exception as e:
            logger.warning("GUI tool %s failed: %s", tool_call.name, e)
            return ToolResult(f"Error: {e}", is_error=True)

    def _call_mcp_tool(self, name: str, arguments: dict[str, Any]) -> str | list[ContentPart]:
        """Dispatch one tool call to the MCP server, mapping the response."""
        if self._mcp is None:
            return "Error: GUI backend is not running."
        # Drop None args; empty strings are meaningful (e.g. set_value clearing
        # a field) except for element_index, where "" means "not using index".
        args = {k: v for k, v in arguments.items() if v is not None}
        if args.get("element_index") == "":
            del args["element_index"]
        if name == "get_app_state":
            if self._max_tree_nodes is not None:
                args.setdefault("max_tree_nodes", self._max_tree_nodes)
            if self._max_tree_depth is not None:
                args.setdefault("max_tree_depth", self._max_tree_depth)
        try:
            text, images, is_error = self._mcp.call_tool(
                name, args, timeout=self._tool_timeout,
            )
        except MCPError as e:
            return f"Error: {e}"
        if is_error:
            return f"Error: {text or 'tool failed'}"
        if images:
            self._last_screenshot = images[-1]
        if not images:
            return text
        parts: list[ContentPart] = []
        if text:
            parts.append(ContentPart(type="text", text=text))
        for img in images:
            parts.append(ContentPart(
                type="image",
                media_type=img.mime_type,
                data=img.data,
            ))
        return parts

    def _build_registry(self) -> ToolRegistry:
        """Build the internal tool registry (excludes done/fail/report_problem)."""
        registry = ToolRegistry()

        def _make_mcp_fn(tool_name: str):
            def mcp_fn(**kwargs: Any) -> str | list[ContentPart]:
                return self._call_mcp_tool(tool_name, kwargs)
            return mcp_fn

        for tool_def in MCP_TOOL_DEFS:
            registry.register(tool_def.name, _make_mcp_fn(tool_def.name), tool_def)

        def wait_fn(seconds: float = 1.0, **kwargs: Any) -> str:
            seconds = min(max(seconds, 0.1), 10.0)
            self._sleep_with_cancel(seconds)
            return f"Waited {seconds:.1f}s"

        registry.register("wait", wait_fn, _WAIT_DEF)
        return registry


def _first_text_line(parts: list[ContentPart] | str) -> str:
    """First text line of a multimodal tool result, for logs and stubs."""
    if isinstance(parts, str):
        return parts.splitlines()[0] if parts else ""
    for part in parts:
        if part.type == "text" and part.text:
            return part.text.splitlines()[0]
    return "(image)"
