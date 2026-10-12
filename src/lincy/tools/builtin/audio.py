"""Audio sub-agent and the read_audio_by_subagent tool.

The brain's own LLM path (Anthropic Messages format) has no audio content
block, so audio is always delegated to a sub-agent backed by an audio-capable
multimodal model. The sub-agent returns a transcript plus delivery cues (tone,
mood, pace) as text.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...llm.base import LLMClient
from ...llm.schema import ContentPart, ToolDefinition, ToolParameter
from ..security import resolve_allowed_path
from .media_agent import MediaDescribeAgent

logger = logging.getLogger(__name__)

# Gemini accepts at most 20 MB per inline request and base64 inflates the file
# by a third, so the raw file must stay well under that.
MAX_AUDIO_FILE_BYTES = 14 * 1024 * 1024

# Containers the OpenAI-compatible input_audio part can name. Keep in sync with
# audio_format_for_media_type in llm/providers/openai_compat.py.
AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
}
_SUPPORTED_EXTENSIONS = set(AUDIO_MIME_TYPES)


class AudioAgent(MediaDescribeAgent):
    """Sub-agent that sends audio to an audio-capable LLM and returns text."""

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        *,
        cache_dir: Path | None = None,
        model_fingerprint: str | None = None,
    ):
        super().__init__(
            client,
            system_prompt,
            session_label="audio",
            cache_dir=cache_dir,
            model_fingerprint=model_fingerprint,
        )


READ_AUDIO_BY_SUBAGENT_DEFINITION = ToolDefinition(
    name="read_audio_by_subagent",
    description=(
        "Listen to an audio file using an independent audio sub-agent. "
        "It returns a verbatim transcript plus how it was said: tone, mood, "
        "attitude, pace, hesitation, background sounds, number of speakers. "
        "The sub-agent has NO access to our conversation context, so you MUST "
        "provide a complete and clear description in the 'context' parameter of "
        "who is likely speaking, what the conversation is about, and what to "
        "listen for. Supports OGG/Opus, MP3, WAV, M4A, AAC, FLAC, and AIFF."
    ),
    parameters={
        "path": ToolParameter(
            type="string",
            description="Path to the audio file (relative to working directory or absolute).",
        ),
        "context": ToolParameter(
            type="string",
            description=(
                "Complete instructions for the audio agent: speaker, situation, "
                "language expected, and what to pay attention to. Include all "
                "relevant context since the agent cannot see our conversation."
            ),
        ),
    },
    required=["path", "context"],
)


def _read_audio_data(
    path: str,
    allowed_paths: list[str],
    base_dir: Path,
) -> tuple[str, str, int]:
    """Read an audio file and return (base64_data, media_type, size_bytes).

    Raises ValueError on invalid path/format/size, FileNotFoundError if missing.
    """
    target = resolve_allowed_path(path, allowed_paths, base_dir)
    if target is None:
        raise ValueError(f"Path not allowed: {path}")
    if not target.exists():
        raise FileNotFoundError(f"Audio not found: {path}")

    ext = target.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported audio format: {ext}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    size = target.stat().st_size
    if size > MAX_AUDIO_FILE_BYTES:
        raise ValueError(
            f"Audio file too large: {size} bytes "
            f"(limit {MAX_AUDIO_FILE_BYTES} bytes)"
        )

    b64 = base64.b64encode(target.read_bytes()).decode("ascii")
    return b64, AUDIO_MIME_TYPES[ext], size


def create_read_audio_by_subagent(
    allowed_paths: list[str],
    base_dir: Path,
    audio_agent: AudioAgent,
) -> Callable[..., str]:
    """Create read_audio_by_subagent: delegate an audio file to the audio sub-agent."""

    def read_audio_by_subagent(
        path: str = "", context: str = "", **kwargs: Any,
    ) -> str:
        p = path or kwargs.get("file_path", "")
        if not p:
            return "Error: path is required."
        if not context:
            return "Error: context is required."
        try:
            b64, media_type, size = _read_audio_data(p, allowed_paths, base_dir)
        except (ValueError, FileNotFoundError) as e:
            return f"Error: {e}"

        header = f"[Audio: {p} ({media_type}, {size // 1024} KB)]"
        parts = [
            ContentPart(type="text", text=context),
            ContentPart(type="audio", media_type=media_type, data=b64),
        ]
        try:
            description = audio_agent.describe(parts)
            return f"{header}\n{description}"
        except Exception as e:
            logger.warning("Audio sub-agent failed for %s: %s", p, e)
            return f"{header}\n(Audio analysis unavailable: {e})"

    return read_audio_by_subagent
