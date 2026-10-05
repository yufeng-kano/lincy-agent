"""Brain-facing gui_task / screenshot tool definitions and factories."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..llm.schema import ContentPart, ToolDefinition, ToolParameter
from .manager import GUIManager

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
        "Delegate a GUI task to an autonomous desktop agent. "
        "The intent is a self-contained prompt for a subagent - "
        "it must be understandable WITHOUT any conversation context. "
        "Write the intent as a GOAL, not step-by-step instructions. "
        "The GUI agent decides HOW to achieve the goal.\n"
        "\n"
        "Intent guidelines:\n"
        "- State the goal and success criteria clearly.\n"
        "- Include the URL when you know it. Otherwise describe the "
        "destination and give search keywords.\n"
        "- Do NOT include conversation context, nicknames, or "
        "references that only make sense in this chat.\n"
        "- For search tasks, provide alternative names/keywords "
        "the agent can try if the primary name is not found.\n"
        "- Include constraints (save path, app preference) as bullet points.\n"
        "\n"
        "Good: 'Download a photo of the singer Kano from her "
        "Twitter/X page https://x.com/kano_hanano (if it does not load, "
        "search: Kano, kano_hanano, Kano singer). Save to ~/Pictures/kano.jpg.'\n"
        "Bad: 'Save a cute photo of her for honey from that account we "
        "talked about.'"
    ),
    parameters={
        "intent": ToolParameter(
            type="string",
            description=(
                "Self-contained goal description for the GUI subagent. "
                "Must be understandable without conversation context. "
                "Describe WHAT to achieve, not HOW to operate."
            ),
        ),
        "session_id": ToolParameter(
            type="string",
            description="Optional session ID to resume a previous GUI task.",
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


def format_gui_result(result: Any) -> str:
    """Format a GUITaskResult into a human-readable status string."""
    if result.needs_input:
        status = "BLOCKED"
    elif result.success:
        status = "SUCCESS"
    else:
        status = "FAILED"
    parts = [
        f"[GUI {status}] (steps: {result.steps_used}, "
        f"time: {result.elapsed_sec:.1f}s, session: {result.session_id})",
    ]
    parts.append(result.summary)
    if result.screenshot_path:
        parts.append(f"\nScreenshot: {result.screenshot_path}")
    if result.report:
        parts.append(f"\nReport:\n{result.report}")
    if result.needs_input:
        parts.append(
            "\nYou may issue a new gui_task with adjusted instructions to retry."
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

    def _run_sync(
        intent: str,
        session_id: str | None,
        app_prompt: str | None,
    ) -> str:
        app_prompt_text = _resolve_app_prompt(app_prompt, agent_os_dir)
        if gui_lock is not None and not gui_lock.acquire(timeout=lock_wait_seconds):
            return (
                f"[GUI BUSY] Another GUI task is still running after "
                f"{lock_wait_seconds:g}s; retry later or report back."
            )
        try:
            result = manager.execute_task(
                intent, session_id=session_id,
                app_prompt_text=app_prompt_text,
            )
        except Exception as e:
            logger.error("GUI task error: %s", e)
            return f"GUI task error: {e}"
        finally:
            if gui_lock is not None:
                gui_lock.release()
        return format_gui_result(result)

    def gui_task(
        intent: str = "", session_id: str = "",
        app_prompt: str = "", **kwargs: Any,
    ) -> str:
        if not intent:
            return "Error: intent is required."
        return _run_sync(intent, session_id or None, app_prompt or None)

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
        from .actions import take_screenshot

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
