"""Tests for gui/permissions.py: record file and messages (checks are faked)."""

import json
import os
import subprocess
import sys

import pytest

from lincy.gui import permissions


@pytest.fixture()
def granted(monkeypatch):
    state = {"ax": True, "screen": True}
    monkeypatch.setattr(permissions, "accessibility_granted", lambda **kw: state["ax"])
    monkeypatch.setattr(permissions, "screen_recording_granted", lambda **kw: state["screen"])
    return state


def test_all_granted_records_executable(granted, tmp_path):
    state_dir = tmp_path / "state"
    assert permissions.check_gui_permissions(state_dir) == []
    record = json.loads((state_dir / "gui_permissions.json").read_text())
    assert record == {"executable": os.path.realpath(sys.executable)}


def test_missing_permissions_messages(granted, tmp_path):
    granted["ax"] = False
    granted["screen"] = False
    problems = permissions.check_gui_permissions(tmp_path)
    assert len(problems) == 2
    assert "Accessibility permission is not granted" in problems[0]
    assert "System Settings > Privacy & Security > Accessibility" in problems[0]
    assert "Screen Recording permission is not granted" in problems[1]
    assert "Privacy & Security > Screen & System Audio Recording" in problems[1]
    for problem in problems:
        assert os.path.realpath(sys.executable) in problem
        assert "Terminal" in problem
        assert "launchd" in problem
        assert "last granted" not in problem
    assert not (tmp_path / "gui_permissions.json").exists()


def test_changed_executable_is_called_out(granted, tmp_path):
    (tmp_path / "gui_permissions.json").write_text(json.dumps({"executable": "/old/venv/bin/python"}))
    granted["screen"] = False
    problems = permissions.check_gui_permissions(tmp_path)
    assert len(problems) == 1
    assert "last granted to /old/venv/bin/python" in problems[0]
    assert "grant them again" in problems[0]


def test_same_executable_has_no_changed_note(granted, tmp_path):
    (tmp_path / "gui_permissions.json").write_text(json.dumps({"executable": os.path.realpath(sys.executable)}))
    granted["ax"] = False
    problems = permissions.check_gui_permissions(tmp_path)
    assert "last granted" not in problems[0]


@pytest.mark.parametrize(("returncode", "stdout", "expected"), [
    (0, "2\n", True), (0, "3\n", True), (0, "0\n", False), (1, "", False),
])
def test_keyboard_navigation(monkeypatch, returncode, stdout, expected):
    def fake_run(cmd, **kwargs):
        assert cmd == ["defaults", "read", "-g", "AppleKeyboardUIMode"]
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(permissions.subprocess, "run", fake_run)
    assert permissions.keyboard_navigation_enabled() is expected
