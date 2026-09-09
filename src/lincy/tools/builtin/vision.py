"""Vision sub-agent: describes images using a vision-capable LLM."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...llm.session import llm_session
from ...llm.base import LLMClient
from ...llm.schema import ContentPart, Message

_VISION_CACHE_VERSION = "1"


class VisionAgent:
    """Sub-agent that sends images to a vision LLM and returns text descriptions."""

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        *,
        cache_dir: Path | None = None,
        model_fingerprint: str | None = None,
    ):
        self.client = client
        self.system_prompt = system_prompt
        self._cache_dir = cache_dir
        self._model_fingerprint = model_fingerprint or ""
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    @llm_session("vision")
    def describe(self, image_parts: list[ContentPart]) -> str:
        """Send image parts to vision LLM and return text description."""
        cache_path = self._cache_path(image_parts)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=image_parts),
        ]
        result = self.client.chat(messages)
        self._write_cache(cache_path, result)
        return result

    def _cache_path(self, image_parts: list[ContentPart]) -> Path | None:
        """Return the cache file path for a vision request."""
        if self._cache_dir is None:
            return None
        payload = {
            "version": _VISION_CACHE_VERSION,
            "model_fingerprint": self._model_fingerprint,
            "system_prompt": self.system_prompt,
            "parts": [part.model_dump(mode="json", exclude_none=True) for part in image_parts],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self._cache_dir / f"{digest}.json"

    @staticmethod
    def _read_cache(path: Path | None) -> str | None:
        """Load a cached vision result when present."""
        if path is None or not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        content = payload.get("content")
        return content if isinstance(content, str) else None

    @staticmethod
    def _write_cache(path: Path | None, content: str) -> None:
        """Persist a vision result."""
        if path is None:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": _VISION_CACHE_VERSION,
                    "content": content,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
