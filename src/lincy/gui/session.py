"""GUI session persistence: summary file, append-only step log, last state."""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ..llm.schema import ContentPart
from ..timezone_utils import now as tz_now
from .capture import save_screenshot

GUISessionStatus = Literal["active", "completed", "failed", "blocked", "paused"]


class GUIStepRecord(BaseModel):
    """One executed tool call; result is the full tool text without images."""

    tool: str
    args: dict[str, Any]
    result: str


class GUISessionData(BaseModel):
    """Persistent state for a single GUI task session."""

    session_id: str
    intent: str
    app: str | None = None
    status: GUISessionStatus = "active"
    summary: str = ""
    report: str = ""
    steps_used: int = 0
    last_state_text: str = ""
    last_screenshot_path: str = ""
    created_at: datetime
    updated_at: datetime


def _generate_gui_session_id() -> str:
    """Generate a time-sortable session ID: YYYYMMDD_HHMMSS_<6-hex>."""
    timestamp = tz_now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{os.urandom(3).hex()}"


class GUISessionStore:
    """Manages GUI session files under session/gui/.

    Per session: <id>.json (summary), <id>.steps.jsonl (append-only steps),
    <id>.jpg (last screenshot, written on finalize).
    """

    def __init__(self, gui_sessions_dir: Path) -> None:
        self._dir = gui_sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def create(self, intent: str, app: str | None = None) -> GUISessionData:
        now = tz_now()
        data = GUISessionData(
            session_id=_generate_gui_session_id(),
            intent=intent,
            app=app,
            created_at=now,
            updated_at=now,
        )
        self._save(data)
        return data

    def load(self, session_id: str) -> GUISessionData:
        path = self._dir / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"GUI session not found: {session_id}")
        return GUISessionData.model_validate_json(path.read_text(encoding="utf-8"))

    def append_step(self, session_id: str, step: GUIStepRecord) -> None:
        # Append-only debug log with the full state text per step; the
        # session file itself is written once, at finalize.
        with (self._dir / f"{session_id}.steps.jsonl").open("a", encoding="utf-8") as f:
            f.write(step.model_dump_json() + "\n")

    def finalize(
        self,
        session_id: str,
        *,
        status: GUISessionStatus,
        steps: int,
        summary: str,
        report: str,
        last_state_text: str,
        screenshot: ContentPart | None,
    ) -> GUISessionData:
        """Record the outcome plus the last state so a later call can resume."""
        data = self.load(session_id)
        data.status = status
        data.steps_used += steps
        data.summary = summary
        data.report = report
        if last_state_text:
            data.last_state_text = last_state_text
        if screenshot is not None:
            path = self._dir / f"{session_id}.jpg"
            save_screenshot(screenshot, str(path))
            data.last_screenshot_path = str(path)
        data.updated_at = tz_now()
        self._save(data)
        return data

    def _save(self, data: GUISessionData) -> None:
        path = self._dir / f"{data.session_id}.json"
        path.write_text(data.model_dump_json(indent=2) + "\n", encoding="utf-8")
