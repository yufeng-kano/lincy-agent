"""Tests for GUI lock mechanism in gui_task tool."""

import threading
from unittest.mock import MagicMock

from lincy.gui.manager import GUITaskResult
from lincy.gui.tool_adapter import create_gui_task

_RESULT = GUITaskResult(
    status="success", summary="Done", steps_used=1, session_id="s1", elapsed_sec=0.5,
)


def _make_manager():
    mgr = MagicMock()
    mgr.execute_task.return_value = _RESULT
    return mgr


class TestGuiLock:
    def test_gui_task_without_lock(self):
        mgr = _make_manager()
        fn = create_gui_task(mgr, gui_lock=None)
        result = fn(intent="do something")
        assert "SUCCESS" in result
        mgr.execute_task.assert_called_once()

    def test_gui_task_with_lock(self):
        mgr = _make_manager()
        lock = threading.Lock()
        fn = create_gui_task(mgr, gui_lock=lock)
        result = fn(intent="do something")
        assert "SUCCESS" in result
        mgr.execute_task.assert_called_once()

    def test_gui_task_holds_lock_during_execution(self):
        """Verify the lock is held while execute_task runs."""
        lock = threading.Lock()
        lock_was_held = []

        def fake_execute(intent, session_id=None, app=None, app_prompt_text=None):
            lock_was_held.append(lock.locked())
            return _RESULT

        mgr = MagicMock()
        mgr.execute_task.side_effect = fake_execute

        fn = create_gui_task(mgr, gui_lock=lock)
        fn(intent="check lock")

        assert lock_was_held == [True]
        assert not lock.locked()

    def test_gui_task_returns_busy_when_lock_wait_times_out(self):
        mgr = _make_manager()
        lock = threading.Lock()
        lock.acquire()
        try:
            fn = create_gui_task(mgr, gui_lock=lock, lock_wait_seconds=0.1)
            result = fn(intent="do something")
        finally:
            lock.release()
        assert result.startswith("[GUI BUSY]")
        assert "0.1s" in result
        mgr.execute_task.assert_not_called()

    def test_gui_task_releases_lock_on_error(self):
        mgr = MagicMock()
        mgr.execute_task.side_effect = RuntimeError("boom")
        lock = threading.Lock()
        fn = create_gui_task(mgr, gui_lock=lock)
        result = fn(intent="fail")
        assert "GUI task error: boom" in result
        assert not lock.locked()
