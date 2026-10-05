"""Tests for gui/tool_adapter.py: Brain-facing gui_task / screenshot tools."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from lincy.gui.manager import GUITaskResult
from lincy.gui.tool_adapter import (
    _resolve_app_prompt,
    GUI_TASK_DEFINITION,
    SCREENSHOT_BY_SUBAGENT_DEFINITION,
    SCREENSHOT_DEFINITION,
    create_gui_task,
    create_screenshot,
    create_screenshot_by_subagent,
    format_gui_result,
)
from lincy.gui.worker import GUIWorker, ScreenDescription
from lincy.llm.schema import ContentPart


class FakeManager:
    """Manager that returns a fixed result."""

    def __init__(self, result: GUITaskResult):
        self._result = result
        self.last_intent: str | None = None
        self.last_session_id: str | None = None
        self.last_app_prompt_text: str | None = None

    def execute_task(
        self, intent: str, session_id: str | None = None,
        app_prompt_text: str | None = None,
    ) -> GUITaskResult:
        self.last_intent = intent
        self.last_session_id = session_id
        self.last_app_prompt_text = app_prompt_text
        return self._result


class FakeErrorManager:
    """Manager that raises an exception."""

    def execute_task(
        self, intent: str, session_id: str | None = None,
        app_prompt_text: str | None = None,
    ) -> GUITaskResult:
        raise RuntimeError("LLM unavailable")


class TestGuiTaskDefinition:
    def test_name_and_params(self):
        assert GUI_TASK_DEFINITION.name == "gui_task"
        assert "intent" in GUI_TASK_DEFINITION.parameters
        assert "session_id" in GUI_TASK_DEFINITION.parameters
        assert "app_prompt" in GUI_TASK_DEFINITION.parameters
        assert GUI_TASK_DEFINITION.required == ["intent"]


class TestCreateGuiTask:
    def test_success_result(self):
        result = GUITaskResult(
            success=True, summary="Opened Finder.", steps_used=3, session_id="20260215_120000_abc123",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Open Finder")
        assert "[GUI SUCCESS]" in output
        assert "steps: 3" in output
        assert "session: 20260215_120000_abc123" in output
        assert "Opened Finder" in output
        assert manager.last_intent == "Open Finder"

    def test_failure_result(self):
        result = GUITaskResult(success=False, summary="App not found.", steps_used=5, session_id="s1")
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Open nonexistent app")
        assert "[GUI FAILED]" in output
        assert "App not found" in output

    def test_empty_intent_error(self):
        result = GUITaskResult(success=True, summary="ok", steps_used=0)
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="")
        assert "Error" in output

    def test_exception_handled(self):
        manager = FakeErrorManager()
        fn = create_gui_task(manager)
        output = fn(intent="Do something")
        assert "error" in output.lower()
        assert "LLM unavailable" in output

    def test_report_included_in_output(self):
        result = GUITaskResult(
            success=True, summary="Done.", report="Found 3 items.", steps_used=2, session_id="s2",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Check screen")
        assert "Report:" in output
        assert "Found 3 items." in output

    def test_no_report_no_report_section(self):
        result = GUITaskResult(success=True, summary="Done.", steps_used=1, session_id="s3")
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Do task")
        assert "Report:" not in output

    def test_screenshot_path_included_in_output(self):
        result = GUITaskResult(
            success=True, summary="Done.", steps_used=2, session_id="s6",
            screenshot_path="/tmp/capture.png",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Take screenshot")
        assert "Screenshot: /tmp/capture.png" in output

    def test_no_screenshot_path_no_screenshot_section(self):
        result = GUITaskResult(success=True, summary="Done.", steps_used=1, session_id="s7")
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        output = fn(intent="Do task")
        assert "Screenshot:" not in output

    def test_session_id_passed_to_manager(self):
        result = GUITaskResult(success=True, summary="Done.", steps_used=0, session_id="s4")
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        fn(intent="Resume task", session_id="existing_session")
        assert manager.last_session_id == "existing_session"

    def test_empty_session_id_passed_as_none(self):
        result = GUITaskResult(success=True, summary="Done.", steps_used=0, session_id="s5")
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        fn(intent="New task", session_id="")
        assert manager.last_session_id is None


class TestScreenshotTool:
    def test_definition(self):
        assert SCREENSHOT_DEFINITION.name == "screenshot"
        assert "region" in SCREENSHOT_DEFINITION.parameters
        assert SCREENSHOT_DEFINITION.required == []

    @patch("lincy.gui.actions.take_screenshot")
    def test_screenshot_returns_multimodal(self, mock_take):
        fake_ss = ContentPart(
            type="image", media_type="image/jpeg", data="base64data",
        )
        mock_take.return_value = fake_ss

        fn = create_screenshot(max_width=800, quality=90)
        result = fn()

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].type == "image"
        assert result[0].data == "base64data"
        assert result[1].type == "text"
        assert result[1].text == "Screenshot taken."
        mock_take.assert_called_once_with(max_width=800, quality=90, region=None)

    @patch("lincy.gui.actions.take_screenshot")
    def test_screenshot_with_region(self, mock_take):
        fake_ss = ContentPart(
            type="image", media_type="image/jpeg", data="cropped",
        )
        mock_take.return_value = fake_ss

        fn = create_screenshot(max_width=800, quality=90)
        result = fn(region=[100, 200, 300, 400])

        assert result[0].data == "cropped"
        mock_take.assert_called_once_with(
            max_width=800, quality=90, region=(100, 200, 300, 400),
        )

    @patch("lincy.gui.actions.take_screenshot")
    def test_screenshot_ignores_invalid_region(self, mock_take):
        fake_ss = ContentPart(type="image", media_type="image/jpeg", data="full")
        mock_take.return_value = fake_ss

        fn = create_screenshot(max_width=800, quality=90)
        fn(region=[100, 200])  # too short

        mock_take.assert_called_once_with(max_width=800, quality=90, region=None)

    @patch("lincy.gui.actions.take_screenshot")
    def test_screenshot_error_propagates(self, mock_take):
        mock_take.side_effect = RuntimeError("No display")
        fn = create_screenshot()
        try:
            fn()
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "No display" in str(e)


class TestResolveAppPrompt:
    def test_none_returns_none(self):
        assert _resolve_app_prompt(None, Path("/tmp")) is None

    def test_empty_returns_none(self):
        assert _resolve_app_prompt("", Path("/tmp")) is None

    def test_no_agent_os_dir_returns_none(self):
        assert _resolve_app_prompt("some/file.md", None) is None

    def test_absolute_path_rejected(self):
        assert _resolve_app_prompt("/etc/passwd", Path("/tmp")) is None

    def test_path_traversal_rejected(self, tmp_path: Path):
        # Create a file outside agent_os_dir
        outside = tmp_path / "outside.md"
        outside.write_text("secret")
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        assert _resolve_app_prompt("../outside.md", agent_dir) is None

    def test_valid_file_read(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        prompt_file = agent_dir / "memory" / "skills" / "app.md"
        prompt_file.parent.mkdir(parents=True)
        prompt_file.write_text("# App Guide\nDo stuff.")
        result = _resolve_app_prompt("memory/skills/app.md", agent_dir)
        assert result == "# App Guide\nDo stuff."

    def test_missing_file_returns_none(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        assert _resolve_app_prompt("nonexistent.md", agent_dir) is None


class TestAppPromptPassthrough:
    def test_app_prompt_text_passed_to_manager(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        prompt_file = agent_dir / "guide.md"
        prompt_file.write_text("Use LINE tabs.")

        result = GUITaskResult(
            success=True, summary="Done.", steps_used=1, session_id="s1",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager, agent_os_dir=agent_dir)
        fn(intent="Open LINE", app_prompt="guide.md")
        assert manager.last_app_prompt_text == "Use LINE tabs."

    def test_app_prompt_missing_file_passes_none(self, tmp_path: Path):
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()

        result = GUITaskResult(
            success=True, summary="Done.", steps_used=1, session_id="s1",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager, agent_os_dir=agent_dir)
        fn(intent="Open LINE", app_prompt="nonexistent.md")
        assert manager.last_app_prompt_text is None

    def test_app_prompt_empty_passes_none(self):
        result = GUITaskResult(
            success=True, summary="Done.", steps_used=1, session_id="s1",
        )
        manager = FakeManager(result)
        fn = create_gui_task(manager)
        fn(intent="Open app", app_prompt="")
        assert manager.last_app_prompt_text is None


class TestScreenshotBySubagentDefinition:
    def test_name_and_params(self):
        assert SCREENSHOT_BY_SUBAGENT_DEFINITION.name == "screenshot_by_subagent"
        assert "context" in SCREENSHOT_BY_SUBAGENT_DEFINITION.parameters
        assert SCREENSHOT_BY_SUBAGENT_DEFINITION.required == ["context"]

    def test_no_region_param(self):
        assert "region" not in SCREENSHOT_BY_SUBAGENT_DEFINITION.parameters


class TestCreateScreenshotBySubagent:
    def test_empty_context_returns_error(self):
        worker = MagicMock(spec=GUIWorker)
        fn = create_screenshot_by_subagent(worker)
        result = fn(context="")
        assert "Error" in result

    def test_returns_description(self):
        worker = MagicMock(spec=GUIWorker)
        worker.describe_screen.return_value = ScreenDescription(
            description="I see a QR code in the top-right corner.",
        )
        fn = create_screenshot_by_subagent(worker)
        result = fn(context="Find the QR code")
        assert "QR code" in result
        assert "Cropped" not in result
        worker.describe_screen.assert_called_once_with(
            "Find the QR code", save_dir=None,
        )

    def test_returns_crop_path(self):
        worker = MagicMock(spec=GUIWorker)
        worker.describe_screen.return_value = ScreenDescription(
            description="QR code found.",
            crop_path="/tmp/crop_123_qr-code.jpg",
        )
        fn = create_screenshot_by_subagent(worker, save_dir="/tmp/crops")
        result = fn(context="Find and crop the QR code")
        assert "QR code found" in result
        assert "Cropped image saved: /tmp/crop_123_qr-code.jpg" in result
        worker.describe_screen.assert_called_once_with(
            "Find and crop the QR code", save_dir="/tmp/crops",
        )

    def test_exception_handled(self):
        worker = MagicMock(spec=GUIWorker)
        worker.describe_screen.side_effect = RuntimeError("No display")
        fn = create_screenshot_by_subagent(worker)
        result = fn(context="Check screen")
        assert "error" in result.lower()
        assert "No display" in result


class TestFormatGuiResult:
    def test_success(self):
        result = GUITaskResult(
            success=True, summary="Done.", steps_used=3,
            session_id="s1", elapsed_sec=2.5,
        )
        output = format_gui_result(result)
        assert "[GUI SUCCESS]" in output
        assert "steps: 3" in output
        assert "time: 2.5s" in output
        assert "session: s1" in output
        assert "Done." in output

    def test_failed(self):
        result = GUITaskResult(
            success=False, summary="Not found.", steps_used=5,
            session_id="s2",
        )
        output = format_gui_result(result)
        assert "[GUI FAILED]" in output

    def test_blocked(self):
        result = GUITaskResult(
            success=False, summary="Login needed.", steps_used=2,
            session_id="s3", needs_input=True,
        )
        output = format_gui_result(result)
        assert "[GUI BLOCKED]" in output
        assert "adjusted instructions" in output
