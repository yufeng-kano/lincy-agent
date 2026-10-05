"""Shared free-form notes that workers carry between tasks.

Deliberately loose: no schema and no content validation, only a size cap
enforced by compactor-driven compression.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..llm.base import LLMClient
from ..llm.schema import Message, ToolDefinition, ToolParameter
from ..llm.session import llm_session
from ..timezone_utils import now as tz_now

logger = logging.getLogger(__name__)

_COMPRESS_SYSTEM_PROMPT = (
    "You compress a worker agent's free-form operational notes. "
    "Keep facts about what worked and what failed per target (site, tool, app). "
    "Keep the dates. Merge duplicates. Drop stale entries superseded by newer "
    "ones; when in doubt, keep the newest. Output plain markdown bullet lines "
    "only, at most {max_chars} characters in total, with no headers and no "
    "commentary."
)


class WorkerNotes:
    """One notes file shared by all worker runs in this process."""

    def __init__(self, path: Path, threshold_chars: int, max_chars: int) -> None:
        self.path = path
        self.threshold_chars = threshold_chars
        self.max_chars = max_chars
        self._lock = threading.Lock()

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def append(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise ValueError("note text is empty")
        entry = f"- [{tz_now().strftime('%Y-%m-%d')}] {text}\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            current = self.read()
            # Keep each entry on its own line even if the file was hand-edited.
            prefix = "\n" if current and not current.endswith("\n") else ""
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(prefix + entry)

    def compress(self, summarize: Callable[[str], str]) -> bool:
        """Replace the notes with a summary once they exceed the threshold.

        The LLM call runs outside the lock so concurrent appends are not
        blocked; text appended meanwhile is kept after the summary.
        """
        with self._lock:
            snapshot = self.read()
        if len(snapshot) <= self.threshold_chars:
            return False

        try:
            summary = summarize(snapshot).strip()
        except Exception as exc:
            logger.warning("Worker notes compression failed: %s", exc)
            return False
        if not summary:
            logger.warning("Worker notes compression returned empty text")
            return False

        with self._lock:
            current = self.read()
            # Another run compressed or the file was edited meanwhile; writing
            # our summary would discard that, so skip this round.
            if not current.startswith(snapshot):
                logger.warning("Worker notes changed during compression; skipped")
                return False
            content = summary + "\n" + current[len(snapshot):]
            fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                # mkstemp creates the file 0600; keep the usual readable mode.
                os.chmod(tmp_name, 0o644)
                os.replace(tmp_name, self.path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        logger.info(
            "Worker notes compressed %s -> %s chars", len(snapshot), len(content),
        )
        return True


@llm_session("compactor")
def compress_worker_notes(client: LLMClient, text: str, max_chars: int) -> str:
    """Summarize notes with the compactor client, hard-capped to max_chars."""
    messages = [
        Message(role="system", content=_COMPRESS_SYSTEM_PROMPT.format(max_chars=max_chars)),
        Message(role="user", content=text),
    ]
    return client.chat(messages).strip()[:max_chars]


WORKER_NOTE_DEFINITION = ToolDefinition(
    name="worker_note",
    description=(
        "Append one reusable lesson to the shared worker notes (injected into "
        "every future worker task). Name the target (site, tool, app) and say "
        "what worked or failed and why. One or two lines."
    ),
    parameters={
        "text": ToolParameter(type="string", description="The note text."),
    },
    required=["text"],
)


def create_worker_note(notes: WorkerNotes) -> Callable[..., str]:
    def worker_note(text: str = "", **_kwargs: Any) -> str:
        if not text.strip():
            return "Error: text is required"
        notes.append(text)
        return "Note saved."

    return worker_note
