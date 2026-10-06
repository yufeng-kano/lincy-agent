"""GUI Manager: computer-use agent loop over the native desktop backend.

The manager LLM sees the focused window as an indexed AX tree with
screen-point bboxes plus a screenshot, and acts through real mouse and
keyboard input (DesktopBackend). Loop governance - one action per
response, repeat detection, per-call step budget - is enforced here in
code rather than left to the prompt.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
from ..llm.session import llm_session
from .capture import coarse_fingerprint
from .desktop import StaleIndexError, StateReadError
from .session import GUISessionData, GUISessionStore, GUIStepRecord

if TYPE_CHECKING:
    from .ax import Snapshot
    from .desktop import DesktopBackend

logger = logging.getLogger(__name__)

_MAX_STEPS = 20
_WAIT_CANCEL_POLL_SECONDS = 0.1
_STALE_STUB_SUFFIX = (
    "[stale state: screenshot removed; element indexes no longer valid; "
    "call get_state for fresh state]"
)
_ONE_TOOL_ERROR = (
    "Error: one tool per response; read the returned state before the next action"
)
_SITUATION_REPORT_PROMPT = (
    "{reason} No tools are available. "
    "Respond with TEXT ONLY (no tool calls).\n\n"
    "Write a concise situation report covering:\n"
    "1. What was accomplished so far.\n"
    "2. What remains to be done.\n"
    "3. Whether the task seems feasible with more steps, "
    "or if the current approach is fundamentally wrong.\n\n"
    "Be specific and factual. Reference the last state you observed."
)

# Callback: (tool_call, result_line, step, max_steps, step_elapsed, total_elapsed)
GUIStepCallback = Callable[[ToolCall, str, int, int, float, float], None]

GUITaskStatus = Literal["success", "failed", "blocked", "paused"]

# --- Tool definitions ---

_INDEX_PARAM = ToolParameter(
    type="integer",
    description="Element index from the MOST RECENT state. Requires state.",
)
_STATE_PARAM = ToolParameter(
    type="integer",
    description=(
        "Number of the state the index was read from (the 'State N' line). "
        "Required with index; must be the most recent state."
    ),
)
_X_PARAM = ToolParameter(
    type="integer",
    description="Screen X in points (same space as the [x,y,w,h] bboxes).",
)
_Y_PARAM = ToolParameter(
    type="integer",
    description="Screen Y in points (same space as the [x,y,w,h] bboxes).",
)

_GET_STATE_DEF = ToolDefinition(
    name="get_state",
    description=(
        "Return the focused window as an indexed element tree with screen "
        "bboxes, plus a screenshot. Every action already returns a fresh "
        "state; call this only to re-observe without acting."
    ),
    parameters={},
    required=[],
)

_OPEN_APP_DEF = ToolDefinition(
    name="open_app",
    description=(
        "Launch or activate an app, bring its window to the front and "
        "maximize it. Returns fresh state."
    ),
    parameters={
        "name": ToolParameter(
            type="string",
            description="English app name or bundle id (e.g. 'Calculator', 'com.google.Chrome').",
        ),
    },
    required=["name"],
)

_CLICK_DEF = ToolDefinition(
    name="click",
    description=(
        "Move the real mouse and click. Give either index with state "
        "(clicks the element's bbox center) or x and y. Returns fresh state."
    ),
    parameters={
        "index": _INDEX_PARAM,
        "state": _STATE_PARAM,
        "x": _X_PARAM,
        "y": _Y_PARAM,
        "button": ToolParameter(
            type="string",
            description="'left' (default) or 'right' for context menus.",
            enum=["left", "right"],
        ),
        "count": ToolParameter(
            type="integer",
            description="1 (default) or 2 for double-click.",
        ),
    },
    required=[],
)

_DRAG_DEF = ToolDefinition(
    name="drag",
    description="Drag with the left mouse button between two screen points. Returns fresh state.",
    parameters={
        "x1": ToolParameter(type="integer", description="Start X (screen points)."),
        "y1": ToolParameter(type="integer", description="Start Y (screen points)."),
        "x2": ToolParameter(type="integer", description="End X (screen points)."),
        "y2": ToolParameter(type="integer", description="End Y (screen points)."),
    },
    required=["x1", "y1", "x2", "y2"],
)

_SCROLL_DEF = ToolDefinition(
    name="scroll",
    description=(
        "Move the mouse over an element (index) or point (x, y) and scroll "
        "the wheel. Returns fresh state."
    ),
    parameters={
        "index": _INDEX_PARAM,
        "state": _STATE_PARAM,
        "x": _X_PARAM,
        "y": _Y_PARAM,
        "direction": ToolParameter(
            type="string",
            description="Scroll direction.",
            enum=["up", "down", "left", "right"],
        ),
        "amount": ToolParameter(
            type="integer",
            description="Wheel lines to scroll (default 3).",
        ),
    },
    required=["direction"],
)

_TYPE_TEXT_DEF = ToolDefinition(
    name="type_text",
    description=(
        "Type text at the current keyboard focus with real key events. "
        "Click the field first; press Command+A before typing to replace "
        "existing content. Newlines are sent as Return. Returns fresh state."
    ),
    parameters={
        "text": ToolParameter(type="string", description="Text to type."),
    },
    required=["text"],
)

_PRESS_KEY_DEF = ToolDefinition(
    name="press_key",
    description=(
        "Press a key or key combination, e.g. 'Return', 'Escape', 'Tab', "
        "'Down', 'Command+A', 'Command+Shift+G'. Returns fresh state."
    ),
    parameters={
        "key": ToolParameter(type="string", description="Key or combination."),
    },
    required=["key"],
)

_SET_INPUT_SOURCE_DEF = ToolDefinition(
    name="set_input_source",
    description=(
        "Switch the keyboard input source. The current source id is shown "
        "in every state. Returns fresh state."
    ),
    parameters={
        "source_id": ToolParameter(
            type="string",
            description="Input source id, e.g. 'com.apple.keylayout.ABC'.",
        ),
    },
    required=["source_id"],
)

_WAIT_DEF = ToolDefinition(
    name="wait",
    description=(
        "Wait 0.1-10 seconds for loading or transitions, then return fresh state."
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
        "The caller may provide corrected instructions and resume this session."
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

MANAGER_TOOLS = [
    _GET_STATE_DEF,
    _OPEN_APP_DEF,
    _CLICK_DEF,
    _DRAG_DEF,
    _SCROLL_DEF,
    _TYPE_TEXT_DEF,
    _PRESS_KEY_DEF,
    _SET_INPUT_SOURCE_DEF,
    _WAIT_DEF,
    _DONE_DEF,
    _FAIL_DEF,
    _REPORT_PROBLEM_DEF,
]


class GUITaskResult(BaseModel):
    """Result of one gui_task call."""

    status: GUITaskStatus
    summary: str
    report: str = ""
    session_id: str
    steps_used: int
    elapsed_sec: float
    screenshot_path: str = ""


class _LoopTermination(BaseModel):
    """A terminal tool call (done/fail/report_problem) mapped to a status."""

    status: GUITaskStatus
    summary: str
    report: str = ""


class _GUICommandCancelled(Exception):
    """Raised when a GUI task is cancelled by the user."""


class GUIManager:
    """Runs one GUI task per execute_task call as a governed tool loop."""

    def __init__(
        self,
        client: LLMClient,
        backend: DesktopBackend,
        system_prompt: str,
        *,
        session_store: GUISessionStore,
        max_steps: int = _MAX_STEPS,
        on_step: GUIStepCallback | None = None,
        is_cancel_requested: Callable[[], bool] | None = None,
        allow_wait_tool: bool = True,
        step_delay_min: float = 0.0,
        step_delay_max: float = 0.0,
        keep_full_states: int = 2,
        stale_text_max_chars: int = 2000,
        repeat_limit: int = 3,
        cache_control: dict[str, str] | None = None,
    ):
        self.client = client
        self.backend = backend
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.session_store = session_store
        self.on_step = on_step
        self._is_cancel_requested = is_cancel_requested
        self._step_delay_min = step_delay_min
        self._step_delay_max = max(step_delay_max, step_delay_min)
        self._keep_full_states = max(keep_full_states, 1)
        self._stale_text_max_chars = max(stale_text_max_chars, 200)
        self._repeat_limit = repeat_limit
        self._cache_control = cache_control
        self._allow_wait = allow_wait_tool
        self._tools = [
            t for t in MANAGER_TOOLS if allow_wait_tool or t.name != "wait"
        ]

    def execute_task(
        self,
        intent: str,
        session_id: str | None = None,
        app: str | None = None,
        app_prompt_text: str | None = None,
    ) -> GUITaskResult:
        """Run one GUI task call; with session_id, resume that session.

        app_prompt_text is appended to the system prompt for this call only.
        """
        previous: GUISessionData | None = None
        if session_id:
            previous = self.session_store.load(session_id)
            app = app or previous.app
        else:
            session_id = self.session_store.create(intent, app).session_id

        with llm_session("gui_manager", session_id):
            return self._run(
                intent=intent,
                session_id=session_id,
                app=app,
                previous=previous,
                app_prompt_text=app_prompt_text,
            )

    def _run(
        self,
        *,
        intent: str,
        session_id: str,
        app: str | None,
        previous: GUISessionData | None,
        app_prompt_text: str | None,
    ) -> GUITaskResult:
        system_content = self.system_prompt
        if app_prompt_text:
            system_content += "\n\n## App-Specific Guide\n\n" + app_prompt_text
        messages = [Message(
            role="system",
            content=system_content,
            cache_control=self._cache_control,
        )]

        task_start = time.monotonic()
        steps = 0
        snapshot: Snapshot | None = None

        def finish(status: GUITaskStatus, summary: str, report: str = "") -> GUITaskResult:
            return self._finish(
                session_id=session_id,
                task_start=task_start,
                steps=steps,
                snapshot=snapshot,
                status=status,
                summary=summary,
                report=report,
            )

        try:
            self._raise_if_cancel_requested()
            try:
                snapshot = self.backend.prepare(app)
            except Exception as e:
                logger.error("GUI preparation failed: %s", e, exc_info=True)
                return finish("failed", f"GUI preparation failed: {e}")
            messages.append(Message(
                role="user",
                content=self._opening_content(intent, session_id, previous, snapshot),
            ))

            repeat_key: tuple[str, str, str, bytes] | None = None
            repeat_count = 0
            step_start = time.monotonic()
            while True:
                self._raise_if_cancel_requested()
                response = self.client.chat_with_tools(
                    advance_cache_breakpoint(messages),
                    self._tools,
                )
                self._raise_if_cancel_requested()
                if not response.has_tool_calls():
                    return finish(
                        "failed",
                        response.content or "Task ended without explicit completion signal.",
                    )
                messages.append(Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                ))

                # Only the first call of a response is acted on: later calls
                # (including a done/fail) were chosen without seeing the
                # state this action produces.
                first, *rest = response.tool_calls
                termination = self._check_terminal(first)
                if termination is not None:
                    self._notify_step(
                        first, termination.summary, steps + 1,
                        time.monotonic() - step_start,
                        time.monotonic() - task_start,
                    )
                    return finish(termination.status, termination.summary, termination.report)

                text, content, new_snapshot = self._execute(first)
                if new_snapshot is not None:
                    snapshot = new_snapshot
                steps += 1
                messages.append(make_tool_result_message(
                    tool_call_id=first.id, name=first.name, content=content,
                ))
                for extra in rest:
                    messages.append(make_tool_result_message(
                        tool_call_id=extra.id, name=extra.name, content=_ONE_TOOL_ERROR,
                    ))
                self._notify_step(
                    first, text.splitlines()[0] if text else "", steps,
                    time.monotonic() - step_start,
                    time.monotonic() - task_start,
                )
                try:
                    self.session_store.append_step(
                        session_id,
                        GUIStepRecord(tool=first.name, args=first.arguments, result=text),
                    )
                except Exception:
                    logger.warning("Failed to record GUI step", exc_info=True)

                # Compare what the model sees: the AX text and a coarse
                # screenshot, so canvas-like UIs that change only visually
                # are not mistaken for "nothing happened".
                if new_snapshot is not None:
                    seen = new_snapshot.render()
                    shot = new_snapshot.screenshot
                    fingerprint = coarse_fingerprint(shot) if shot is not None else b""
                else:
                    seen, fingerprint = text, b""
                # `state` names the snapshot an index was read from and
                # changes every step, so it must not count as a different
                # action.
                args = {k: v for k, v in first.arguments.items() if k != "state"}
                key = (
                    first.name,
                    json.dumps(args, sort_keys=True, ensure_ascii=False),
                    seen,
                    fingerprint,
                )
                repeat_count = repeat_count + 1 if key == repeat_key else 1
                repeat_key = key
                if repeat_count >= self._repeat_limit:
                    report = self._request_situation_report(
                        messages,
                        f"You called {first.name} with the same arguments "
                        f"{repeat_count} times in a row and the state did not change, "
                        "so the loop was stopped.",
                    )
                    return finish(
                        "paused",
                        f"Stopped: the same {first.name} call repeated "
                        f"{repeat_count} times without changing the state.",
                        report,
                    )
                if steps >= self.max_steps:
                    report = self._request_situation_report(
                        messages, "You have used the step budget for this call.",
                    )
                    return finish(
                        "paused",
                        f"Step budget for this call used up ({self.max_steps} steps).",
                        report,
                    )

                self._raise_if_cancel_requested()
                self._sleep_after_step()
                self._collapse_stale_states(messages)
                step_start = time.monotonic()
        except _GUICommandCancelled:
            return finish("failed", "Cancelled by user.")
        except Exception as e:
            # An LLM or runtime failure mid-task: keep the session resumable
            # with its last state instead of leaving it active and empty.
            logger.error("GUI loop failed: %s", e, exc_info=True)
            return finish(
                "paused",
                f"GUI loop error after {steps} steps: {e}. The session can be resumed.",
            )

    def _opening_content(
        self,
        intent: str,
        session_id: str,
        previous: GUISessionData | None,
        snapshot: Snapshot,
    ) -> list[ContentPart]:
        """Intent (or resume context) followed by the prepared current state."""
        parts: list[ContentPart] = []
        if previous is None:
            parts.append(ContentPart(type="text", text=f"GUI TASK: {intent}"))
        else:
            # Resume carries the previous outcome and last observation, not a
            # step-by-step log: the report already says what was done.
            parts.append(ContentPart(type="text", text=(
                f"Resuming GUI session {session_id}.\n"
                f"Previous report:\n{previous.report or previous.summary}\n\n"
                f"Last observed state:\n{previous.last_state_text}\n\n"
                f"New instruction: {intent}"
            )))
            last_image = Path(previous.last_screenshot_path) if previous.last_screenshot_path else None
            if last_image is not None and last_image.is_file():
                parts.append(ContentPart(
                    type="image",
                    media_type="image/jpeg",
                    data=base64.b64encode(last_image.read_bytes()).decode("ascii"),
                ))
        parts.append(ContentPart(type="text", text=(
            f"Default input source (already selected): {self.backend.default_input_source}\n\n"
            f"Current state:\n{_state_text(snapshot)}"
        )))
        if snapshot.screenshot is not None:
            parts.append(snapshot.screenshot)
        return parts

    # --- tool execution ---

    def _execute(
        self, tool_call: ToolCall,
    ) -> tuple[str, str | list[ContentPart], Snapshot | None]:
        """Run one action; return (full text, tool content, new snapshot)."""
        args = tool_call.arguments
        try:
            snapshot = self._run_action(tool_call.name, args)
        except _GUICommandCancelled:
            raise
        except StaleIndexError as e:
            text = (
                f"Error: {e}. Nothing was clicked. Use an index and state number "
                "from the most recent state."
            )
            return text, text, None
        except StateReadError as e:
            text = (
                f"Error: the {tool_call.name} action WAS performed, but reading the "
                f"new state failed ({e}). Do not repeat the action; call get_state first."
            )
            return text, text, None
        except KeyError as e:
            text = f"Error: missing required argument {e.args[0]!r}"
            return text, text, None
        except Exception as e:
            logger.warning("GUI tool %s failed: %s", tool_call.name, e)
            text = f"Error: {e}"
            return text, text, None
        text = _state_text(snapshot)
        content = [ContentPart(type="text", text=text)]
        if snapshot.screenshot is not None:
            content.append(snapshot.screenshot)
        return text, content, snapshot

    def _run_action(self, name: str, args: dict[str, Any]) -> Snapshot:
        backend = self.backend
        if name == "get_state":
            return backend.get_state()
        if name == "open_app":
            return backend.open_app(str(args["name"]))
        if name == "click":
            return backend.click(
                index=_opt_int(args.get("index")),
                state=_opt_int(args.get("state")),
                x=_opt_int(args.get("x")),
                y=_opt_int(args.get("y")),
                button=args.get("button") or "left",
                count=int(args.get("count") or 1),
            )
        if name == "drag":
            return backend.drag(
                int(args["x1"]), int(args["y1"]), int(args["x2"]), int(args["y2"]),
            )
        if name == "scroll":
            return backend.scroll(
                index=_opt_int(args.get("index")),
                state=_opt_int(args.get("state")),
                x=_opt_int(args.get("x")),
                y=_opt_int(args.get("y")),
                direction=args["direction"],
                amount=int(args.get("amount") or 3),
            )
        if name == "type_text":
            return backend.type_text(str(args["text"]))
        if name == "press_key":
            return backend.press_key(str(args["key"]))
        if name == "set_input_source":
            return backend.set_input_source(str(args["source_id"]))
        if name == "wait" and self._allow_wait:
            self._sleep_with_cancel(min(max(float(args["seconds"]), 0.1), 10.0))
            return backend.get_state()
        raise ValueError(f"unknown tool: {name}")

    def _check_terminal(self, tool_call: ToolCall) -> _LoopTermination | None:
        args = tool_call.arguments
        if tool_call.name == "done":
            return _LoopTermination(
                status="success",
                summary=args.get("summary", "Task completed."),
                report=args.get("report", ""),
            )
        if tool_call.name == "fail":
            return _LoopTermination(
                status="failed",
                summary=args.get("reason", "Task failed."),
                report=args.get("report", ""),
            )
        if tool_call.name == "report_problem":
            return _LoopTermination(
                status="blocked",
                summary=args.get("problem", "Problem reported."),
                report=args.get("report", ""),
            )
        return None

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

    def _request_situation_report(self, messages: list[Message], reason: str) -> str:
        """One extra LLM call (no tools) so the caller can resume with context."""
        try:
            self._raise_if_cancel_requested()
            self._collapse_stale_states(messages)
            messages.append(Message(
                role="user",
                content=_SITUATION_REPORT_PROMPT.format(reason=reason),
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
            logger.warning("Failed to get GUI situation report", exc_info=True)
            return ""

    # --- finalization and pacing ---

    def _finish(
        self,
        *,
        session_id: str,
        task_start: float,
        steps: int,
        snapshot: Snapshot | None,
        status: GUITaskStatus,
        summary: str,
        report: str,
    ) -> GUITaskResult:
        screenshot_path = ""
        try:
            data = self.session_store.finalize(
                session_id,
                status="completed" if status == "success" else status,
                steps=steps,
                summary=summary,
                report=report,
                last_state_text=_state_text(snapshot) if snapshot is not None else "",
                screenshot=snapshot.screenshot if snapshot is not None else None,
            )
            screenshot_path = data.last_screenshot_path
        except Exception:
            logger.warning("Failed to finalize GUI session", exc_info=True)
        return GUITaskResult(
            status=status,
            summary=summary,
            report=report,
            session_id=session_id,
            steps_used=steps,
            elapsed_sec=time.monotonic() - task_start,
            screenshot_path=screenshot_path,
        )

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
                elapsed_sec, total_elapsed_sec,
            )
        except Exception:
            logger.warning("on_step callback failed for step %d", step)

    def _raise_if_cancel_requested(self) -> None:
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


def _opt_int(value: Any) -> int | None:
    """Models sometimes send numbers as strings or floats; empty means unset."""
    if value is None or value == "":
        return None
    return int(value)


def _state_text(snapshot: Snapshot) -> str:
    """Tool-facing state: the render plus the state number indexes belong to."""
    return f"State {snapshot.snapshot_id}\n{snapshot.render()}"
