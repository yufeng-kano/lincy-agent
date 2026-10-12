"""Vision sub-agent: describes images using a vision-capable LLM."""

from __future__ import annotations

from pathlib import Path

from ...llm.base import LLMClient
from .media_agent import MediaDescribeAgent


class VisionAgent(MediaDescribeAgent):
    """Sub-agent that sends images to a vision LLM and returns text descriptions."""

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
            session_label="vision",
            cache_dir=cache_dir,
            model_fingerprint=model_fingerprint,
        )
