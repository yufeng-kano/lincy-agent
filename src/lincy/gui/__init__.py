"""GUI desktop automation module.

AX-first architecture:
- Brain calls gui_task (tool_adapter.py)
- GUIManager runs an agentic loop over the OpenComputerUse MCP server
  (accessibility tree + screenshot + background input) and sees tool
  results directly (manager.py, mcp_client.py, ax_runtime.py)
- GUIWorker remains only as the vision describer behind
  screenshot_by_subagent (worker.py)
"""

from .manager import GUIManager, GUIStepCallback, GUITaskResult
from .session import GUISessionData, GUISessionStore, GUIStepRecord
from .tool_adapter import (
    GUI_TASK_DEFINITION,
    SCREENSHOT_BY_SUBAGENT_DEFINITION,
    SCREENSHOT_DEFINITION,
    create_gui_task,
    create_screenshot,
    create_screenshot_adaptive,
    create_screenshot_by_subagent,
    format_gui_result,
)
from .worker import GUIWorker, ScreenDescription, WorkerObservation

__all__ = [
    "GUI_TASK_DEFINITION",
    "GUIManager",
    "GUIStepCallback",
    "GUISessionData",
    "GUISessionStore",
    "GUIStepRecord",
    "GUITaskResult",
    "GUIWorker",
    "SCREENSHOT_BY_SUBAGENT_DEFINITION",
    "SCREENSHOT_DEFINITION",
    "ScreenDescription",
    "WorkerObservation",
    "create_gui_task",
    "create_screenshot",
    "create_screenshot_adaptive",
    "create_screenshot_by_subagent",
    "format_gui_result",
]
