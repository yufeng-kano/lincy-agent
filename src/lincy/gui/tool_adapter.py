"""Brain-facing gui_task / screenshot tool definitions and factories."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..llm.schema import ContentPart, ToolDefinition, ToolParameter
from .manager import GUIManager, GUITaskResult

if TYPE_CHECKING:
    from .worker import GUIWorker

logger = logging.getLogger(__name__)


def _resolve_app_prompt(
    app_prompt: str | None,
    agent_os_dir: Path | None,
) -> str | None:
    """Read an app-specific prompt file, returning its content or None.

    Path must be relative and stay within agent_os_dir.
    """
    if not app_prompt or agent_os_dir is None:
        return None
    # Reject absolute paths
    if Path(app_prompt).is_absolute():
        logger.warning("app_prompt must be relative: %s", app_prompt)
        return None
    resolved = (agent_os_dir / app_prompt).resolve()
    # Path traversal guard
    if not str(resolved).startswith(str(agent_os_dir.resolve())):
        logger.warning("app_prompt escapes agent_os_dir: %s", app_prompt)
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("app_prompt file not found: %s", resolved)
        return None
    except Exception:
        logger.warning("Failed to read app_prompt: %s", resolved)
        return None

GUI_TASK_DEFINITION = ToolDefinition(
    name="gui_task",
    description=(
        "Delegate a desktop GUI task to an autonomous GUI agent that works "
        "like a person at the keyboard. It can only look (accessibility "
        "tree + screenshot) and use the real mouse and keyboard. It has no "
        "shell, cannot save or read files on its own, cannot paste file "
        "paths, and cannot see your files or this conversation. Do the "
        "preparation (put files on the Desktop, compute the values, open "
        "the page) and the verification afterwards yourself.\n"
        "\n"
        "The intent must be self-contained and use exactly these five "
        "fields, without operating steps (the GUI agent decides how):\n"
        "Goal: what to achieve.\n"
        "Success criteria: what the screen shows when it is done.\n"
        "Values to enter: every value, one per line, exactly as typed.\n"
        "Already prepared: where files are, which app/page is already open.\n"
        "Forbidden: actions it must not take (e.g. do not submit, do not "
        "delete).\n"
        "\n"
        "Results start with [GUI SUCCESS], [GUI FAILED], [GUI BLOCKED] or "
        "[GUI PAUSED]. BLOCKED means it needs different instructions; "
        "PAUSED means this call's step budget ran out or it was stopped "
        "for repeating itself. Both can continue the same session with "
        "session_id and a new instruction."
    ),
    parameters={
        "intent": ToolParameter(
            type="string",
            description=(
                "Self-contained task in the five-field template (Goal, "
                "Success criteria, Values to enter, Already prepared, "
                "Forbidden). Describe WHAT to achieve, not HOW to operate."
            ),
        ),
        "app": ToolParameter(
            type="string",
            description=(
                "Optional app to bring to the front and maximize before the "
                "agent starts: English app name or bundle id "
                "(e.g. 'Google Chrome', 'com.apple.TextEdit')."
            ),
        ),
        "session_id": ToolParameter(
            type="string",
            description=(
                "Optional session ID from a previous [GUI BLOCKED] or "
                "[GUI PAUSED] result. The agent resumes with that session's "
                "report and last screen state; the intent is the new "
                "instruction."
            ),
        ),
        "app_prompt": ToolParameter(
            type="string",
            description=(
                "Optional path to an app-specific .md guide file, "
                "relative to the agent workspace directory. "
                "The file content is injected into the GUI manager's "
                "system prompt as app-specific context. "
                "Example: 'personal-skills/gui-control/references/line-operation.md'"
            ),
        ),
    },
    required=["intent"],
)


def format_gui_result(result: GUITaskResult) -> str:
    """Format a GUITaskResult into the text the calling agent reads."""
    parts = [
        f"[GUI {result.status.upper()}] (steps: {result.steps_used}, "
        f"time: {result.elapsed_sec:.1f}s, session: {result.session_id})",
        result.summary,
    ]
    if result.screenshot_path:
        parts.append(f"\nScreenshot: {result.screenshot_path}")
    if result.report:
        parts.append(f"\nReport:\n{result.report}")
    if result.status in ("blocked", "paused"):
        parts.append(
            f"\nCall gui_task again with session_id={result.session_id} "
            "and a new instruction to continue."
        )
    return "\n".join(parts)


def create_gui_task(
    manager: GUIManager,
    gui_lock: threading.Lock | None = None,
    agent_os_dir: Path | None = None,
    lock_wait_seconds: float = 300,
) -> Callable[..., str]:
    """Create gui_task tool function bound to a GUIManager instance.

    The task runs synchronously. *gui_lock* prevents concurrent GUI access;
    a caller that cannot get it within *lock_wait_seconds* gets a
    ``[GUI BUSY]`` result instead of blocking indefinitely.
    """

    def gui_task(
        intent: str = "", session_id: str = "", app: str = "",
        app_prompt: str = "", **kwargs: Any,
    ) -> str:
        if not intent:
            return "Error: intent is required."
        app_prompt_text = _resolve_app_prompt(app_prompt or None, agent_os_dir)
        if gui_lock is not None and not gui_lock.acquire(timeout=lock_wait_seconds):
            return (
                f"[GUI BUSY] Another GUI task is still running after "
                f"{lock_wait_seconds:g}s; retry later or report back."
            )
        try:
            result = manager.execute_task(
                intent,
                session_id=session_id or None,
                app=app or None,
                app_prompt_text=app_prompt_text,
            )
        except Exception as e:
            logger.error("GUI task error: %s", e)
            return f"GUI task error: {e}"
        finally:
            if gui_lock is not None:
                gui_lock.release()
        return format_gui_result(result)

    return gui_task


SCREENSHOT_DEFINITION = ToolDefinition(
    name="screenshot",
    description=(
        "Take a screenshot of the current screen and return it for visual analysis. "
        "Use this to see what is currently displayed on the desktop. "
        "Optionally crop to a specific region for better detail on small UI areas."
    ),
    parameters={
        "region": ToolParameter(
            type="array",
            description=(
                "Optional crop region [x, y, width, height] in logical pixels. "
                "Omit to capture the full screen."
            ),
            json_schema={
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
            },
        ),
    },
    required=[],
)


def create_screenshot(
    *,
    max_width: int | None = 1280,
    quality: int = 80,
) -> Callable[..., list[ContentPart]]:
    """Create screenshot tool that returns multimodal content."""

    def screenshot(region: list[int] | None = None, **kwargs: Any) -> list[ContentPart]:
        from .capture import take_screenshot

        rgn: tuple[int, int, int, int] | None = None
        if region and len(region) == 4:
            rgn = (region[0], region[1], region[2], region[3])
        ss = take_screenshot(max_width=max_width, quality=quality, region=rgn)
        return [ss, ContentPart(type="text", text="Screenshot taken.")]

    return screenshot


def create_screenshot_adaptive(
    *,
    max_width: int | None = 1280,
    quality: int = 80,
    own_vision_active: Callable[[], bool],
) -> Callable[..., str | list[ContentPart]]:
    """Own-vision screenshot when active; otherwise steer to sub-agent tool."""

    own = create_screenshot(max_width=max_width, quality=quality)

    def screenshot(
        region: list[int] | None = None, **kwargs: Any
    ) -> str | list[ContentPart]:
        if own_vision_active():
            return own(region=region, **kwargs)
        return (
            "Error: current model lacks vision; use screenshot_by_subagent "
            "with a complete context description."
        )

    return screenshot


SCREENSHOT_BY_SUBAGENT_DEFINITION = ToolDefinition(
    name="screenshot_by_subagent",
    description=(
        "Take a screenshot and delegate visual analysis to a vision sub-agent. "
        "The sub-agent has NO access to our conversation context, "
        "so the 'context' parameter must completely describe what to look for "
        "or analyze on screen. "
        "If you ask the sub-agent to locate a specific visual element "
        "(e.g. 'find the QR code on screen'), it may crop and save that region "
        "as a file, returning the file path along with a text description."
    ),
    parameters={
        "context": ToolParameter(
            type="string",
            description=(
                "Complete instructions for the vision sub-agent. "
                "Describe what to look for, what to analyze, or what information "
                "to extract from the current screen. "
                "Include all relevant context since the sub-agent cannot see "
                "our conversation."
            ),
        ),
    },
    required=["context"],
)


def create_screenshot_by_subagent(
    worker: "GUIWorker",
    *,
    save_dir: str | None = None,
    gui_lock: threading.Lock | None = None,
) -> Callable[..., str]:
    """Create screenshot_by_subagent tool that delegates to GUIWorker."""

    def screenshot_by_subagent(context: str = "", **kwargs: Any) -> str:
        if not context:
            return "Error: context is required."
        try:
            if gui_lock is not None:
                with gui_lock:
                    result = worker.describe_screen(context, save_dir=save_dir)
            else:
                result = worker.describe_screen(context, save_dir=save_dir)
        except Exception as e:
            logger.error("screenshot_by_subagent error: %s", e)
            return f"Screenshot analysis error: {e}"
        parts = [result.description]
        if result.crop_path:
            parts.append(f"\nCropped image saved: {result.crop_path}")
        return "\n".join(parts)

    return screenshot_by_subagent
