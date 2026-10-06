"""GUI desktop automation module.

- Brain/worker call gui_task (tool_adapter.py)
- GUIManager runs a governed tool loop (manager.py) over DesktopBackend
  (desktop.py): AX tree with screen bboxes (ax.py), screenshots
  (capture.py), real mouse/keyboard input (input.py, input_source.py)
- GUISessionStore persists sessions for resume (session.py)
- GUIWorker remains only as the vision describer behind
  screenshot_by_subagent (worker.py)
"""

from .ax import Snapshot
from .desktop import DesktopBackend
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
    "DesktopBackend",
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
    "Snapshot",
    "WorkerObservation",
    "create_gui_task",
    "create_screenshot",
    "create_screenshot_adaptive",
    "create_screenshot_by_subagent",
    "format_gui_result",
]
