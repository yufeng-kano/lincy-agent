"""Tests for gui/session.py: GUISessionStore persistence."""

import base64
import json
from pathlib import Path

import pytest

from lincy.gui.session import GUISessionStore, GUIStepRecord
from lincy.llm.schema import ContentPart


def test_create_and_load(tmp_path: Path):
    store = GUISessionStore(tmp_path / "gui")
    data = store.create("Open Finder", app="Finder")
    assert (tmp_path / "gui" / f"{data.session_id}.json").exists()
    loaded = store.load(data.session_id)
    assert loaded.intent == "Open Finder"
    assert loaded.app == "Finder"
    assert loaded.status == "active"
    assert loaded.steps_used == 0
    assert not (tmp_path / "gui" / f"{data.session_id}.steps.jsonl").exists()


def test_load_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        GUISessionStore(tmp_path / "gui").load("nonexistent_id")


def test_append_step_is_append_only_jsonl(tmp_path: Path):
    store = GUISessionStore(tmp_path / "gui")
    data = store.create("Type hello")
    full_state = "Window: TextEdit\n1 textfield \"\" = \"hello\" (focused) [10,20,300,40]"
    store.append_step(data.session_id, GUIStepRecord(tool="type_text", args={"text": "hello"}, result=full_state))
    store.append_step(data.session_id, GUIStepRecord(tool="press_key", args={"key": "Return"}, result="Error: x"))

    lines = (tmp_path / "gui" / f"{data.session_id}.steps.jsonl").read_text().splitlines()
    assert [json.loads(line)["tool"] for line in lines] == ["type_text", "press_key"]
    assert json.loads(lines[0])["result"] == full_state
    # The session file is only written at finalize.
    assert store.load(data.session_id).steps_used == 0
    assert "steps" not in json.loads((tmp_path / "gui" / f"{data.session_id}.json").read_text())


def test_finalize_writes_state_and_screenshot(tmp_path: Path):
    store = GUISessionStore(tmp_path / "gui")
    data = store.create("Open app")
    shot = ContentPart(type="image", media_type="image/jpeg", data=base64.b64encode(b"JPEG").decode())
    final = store.finalize(
        data.session_id, status="paused", steps=50, summary="Budget used", report="Half done",
        last_state_text="Window: X", screenshot=shot,
    )
    jpg = tmp_path / "gui" / f"{data.session_id}.jpg"
    assert final.last_screenshot_path == str(jpg)
    assert jpg.read_bytes() == b"JPEG"
    loaded = store.load(data.session_id)
    assert (loaded.status, loaded.summary, loaded.report) == ("paused", "Budget used", "Half done")
    assert loaded.last_state_text == "Window: X"


def test_finalize_without_state_keeps_previous(tmp_path: Path):
    store = GUISessionStore(tmp_path / "gui")
    data = store.create("Open app")
    store.finalize(data.session_id, status="blocked", steps=3, summary="s", report="r",
                   last_state_text="Window: old", screenshot=None)
    store.finalize(data.session_id, status="failed", steps=0, summary="prepare failed", report="",
                   last_state_text="", screenshot=None)
    loaded = store.load(data.session_id)
    assert loaded.status == "failed"
    assert loaded.steps_used == 3
    assert loaded.last_state_text == "Window: old"
    assert loaded.last_screenshot_path == ""


def test_dir_created_on_init(tmp_path: Path):
    gui_dir = tmp_path / "session" / "gui"
    GUISessionStore(gui_dir)
    assert gui_dir.is_dir()
