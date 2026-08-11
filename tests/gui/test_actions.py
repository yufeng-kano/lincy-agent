"""Tests for gui/actions.py: coordinate conversion and desktop primitives."""

import sys
from unittest.mock import MagicMock, call as mock_call, patch

import pytest

from lincy.llm.schema import ContentPart


@pytest.fixture()
def mock_pyautogui():
    """Inject a mock pyautogui module so lazy imports resolve."""
    mock = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": mock}):
        yield mock


class TestTakeScreenshot:
    def test_returns_content_part(self, mock_pyautogui):
        from PIL import Image

        from lincy.gui.actions import take_screenshot

        img = Image.new("RGB", (100, 50), color="red")
        mock_pyautogui.screenshot.return_value = img

        result = take_screenshot()
        assert isinstance(result, ContentPart)
        assert result.type == "image"
        assert result.media_type == "image/jpeg"
        assert result.data is not None
        assert result.width == 100
        assert result.height == 50

    def test_resize_when_wider_than_max(self, mock_pyautogui):
        from PIL import Image

        from lincy.gui.actions import take_screenshot

        img = Image.new("RGB", (2000, 1000), color="blue")
        mock_pyautogui.screenshot.return_value = img

        result = take_screenshot(max_width=1000, quality=85)
        assert result.width == 1000
        assert result.height == 500
        assert result.media_type == "image/jpeg"

    def test_no_resize_when_within_max(self, mock_pyautogui):
        from PIL import Image

        from lincy.gui.actions import take_screenshot

        img = Image.new("RGB", (800, 600), color="green")
        mock_pyautogui.screenshot.return_value = img

        result = take_screenshot(max_width=1280)
        assert result.width == 800
        assert result.height == 600


class TestTypeText:
    @patch("time.sleep")
    @patch("subprocess.run")
    def test_always_uses_clipboard(self, mock_run, mock_sleep, mock_pyautogui):
        from lincy.gui import actions

        result = actions.type_text("hello")
        mock_run.assert_called_once_with(
            ["pbcopy"], input=b"hello", check=True,
        )
        mock_pyautogui.hotkey.assert_called_once_with(
            "command", "v", interval=actions._PASTE_HOTKEY_INTERVAL_SECONDS,
        )
        assert mock_sleep.call_args_list == [
            mock_call(actions._CLIPBOARD_SETTLE_SECONDS),
            mock_call(actions._PASTE_SETTLE_SECONDS),
        ]
        assert "hello" in result

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_unicode_uses_clipboard(self, mock_run, mock_sleep, mock_pyautogui):
        from lincy.gui import actions

        actions.type_text("\u4f60\u597d")
        mock_run.assert_called_once_with(
            ["pbcopy"], input="\u4f60\u597d".encode("utf-8"), check=True,
        )
        mock_pyautogui.hotkey.assert_called_once_with(
            "command", "v", interval=actions._PASTE_HOTKEY_INTERVAL_SECONDS,
        )
        assert mock_sleep.call_args_list == [
            mock_call(actions._CLIPBOARD_SETTLE_SECONDS),
            mock_call(actions._PASTE_SETTLE_SECONDS),
        ]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
class TestActivateApp:
    @patch("subprocess.run")
    def test_macos_single_match(self, mock_run, mock_pyautogui):
        from lincy.gui.actions import activate_app

        mock_run.side_effect = [
            # mdfind returns one match
            MagicMock(stdout="/Applications/Utilities/Terminal.app\n", returncode=0),
            # open the app
            MagicMock(returncode=0),
        ]
        result = activate_app("Terminal")
        assert "Terminal.app" in result
        assert mock_run.call_count == 2
        # Verify mdfind uses query expression, not -name flag
        mdfind_call = mock_run.call_args_list[0]
        assert "mdfind" in mdfind_call.args[0]
        assert any("kMDItemFSName" in a for a in mdfind_call.args[0])

    @patch("subprocess.run")
    def test_macos_exact_match_filters_substring(self, mock_run, mock_pyautogui):
        """Exact name match wins over substring matches (e.g. LINE vs Trampoline)."""
        from lincy.gui.actions import activate_app

        mock_run.side_effect = [
            MagicMock(
                stdout=(
                    "/System/Library/GameTrampoline.app\n"
                    "/Applications/LINE.app\n"
                    "/System/Library/MDMMigrationTrampoline.app\n"
                ),
                returncode=0,
            ),
            MagicMock(returncode=0),  # open
        ]
        result = activate_app("LINE")
        assert "Activated" in result
        assert "LINE.app" in result
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_macos_multiple_matches_no_exact(self, mock_run, mock_pyautogui):
        from lincy.gui.actions import activate_app

        mock_run.return_value = MagicMock(
            stdout="/Applications/TermHere.app\n/Applications/TerminalPlus.app\n",
            returncode=0,
        )
        result = activate_app("Term")
        assert "Multiple" in result
        assert "TermHere.app" in result
        assert "TerminalPlus.app" in result

    @patch("subprocess.run")
    def test_macos_no_match(self, mock_run, mock_pyautogui):
        from lincy.gui.actions import activate_app

        mock_run.return_value = MagicMock(stdout="", returncode=0)
        result = activate_app("NonExistentApp")
        assert "No application" in result


class TestWait:
    def test_wait_clamps_and_sleeps(self):
        from lincy.gui.actions import wait

        with patch("time.sleep") as mock_sleep:
            result = wait(2.0)
            mock_sleep.assert_called_once_with(2.0)
            assert "2.0s" in result

    def test_wait_clamps_minimum(self):
        from lincy.gui.actions import wait

        with patch("time.sleep") as mock_sleep:
            wait(0.01)
            mock_sleep.assert_called_once_with(0.1)

    def test_wait_clamps_maximum(self):
        from lincy.gui.actions import wait

        with patch("time.sleep") as mock_sleep:
            wait(99.0)
            mock_sleep.assert_called_once_with(10.0)


class TestScrollAtPixel:
    def test_scroll_down(self, mock_pyautogui):
        from lincy.gui.actions import scroll_at_pixel

        result = scroll_at_pixel(500.0, 300.0, "down", 3)
        mock_pyautogui.moveTo.assert_called_once_with(500.0, 300.0)
        assert mock_pyautogui.scroll.call_count == 3
        for call in mock_pyautogui.scroll.call_args_list:
            assert call == ((-1,), {})
        assert "down" in result
        assert "3 clicks" in result

    def test_scroll_up(self, mock_pyautogui):
        from lincy.gui.actions import scroll_at_pixel

        result = scroll_at_pixel(100.0, 200.0, "up", 5)
        mock_pyautogui.moveTo.assert_called_once_with(100.0, 200.0)
        assert mock_pyautogui.scroll.call_count == 5
        for call in mock_pyautogui.scroll.call_args_list:
            assert call == ((1,), {})
        assert "up" in result

    def test_scroll_invert_down(self, mock_pyautogui):
        from lincy.gui.actions import scroll_at_pixel

        result = scroll_at_pixel(500.0, 500.0, "down", 2, invert=True)
        assert mock_pyautogui.scroll.call_count == 2
        for call in mock_pyautogui.scroll.call_args_list:
            assert call == ((1,), {})
        assert "down" in result

    def test_scroll_invert_up(self, mock_pyautogui):
        from lincy.gui.actions import scroll_at_pixel

        result = scroll_at_pixel(500.0, 500.0, "up", 2, invert=True)
        assert mock_pyautogui.scroll.call_count == 2
        for call in mock_pyautogui.scroll.call_args_list:
            assert call == ((-1,), {})
        assert "up" in result
