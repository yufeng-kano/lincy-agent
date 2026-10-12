"""Shared sub-agent that turns one media file into a text description."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...llm.base import LLMClient
from ...llm.schema import ContentPart, Message
from ...llm.session import llm_session

_CACHE_VERSION = "1"


class MediaDescribeAgent:
    """Send media parts to a multimodal LLM and return its text description.

    The agent has no conversation context: every call must carry its own
    instructions as a text part. Results are cached on disk by request digest
    so re-reading the same file with the same instructions costs nothing.
    """

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        *,
        session_label: str,
        cache_dir: Path | None = None,
        model_fingerprint: str | None = None,
    ):
        self.client = client
        self.system_prompt = system_prompt
        self._session_label = session_label
        self._cache_dir = cache_dir
        self._model_fingerprint = model_fingerprint or ""
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def describe(self, parts: list[ContentPart]) -> str:
        """Send media parts to the LLM and return the text description."""
        with llm_session(self._session_label):
            cache_path = self._cache_path(parts)
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached
            messages = [
                Message(role="system", content=self.system_prompt),
                Message(role="user", content=parts),
            ]
            result = self.client.chat(messages)
            self._write_cache(cache_path, result)
            return result

    def _cache_path(self, parts: list[ContentPart]) -> Path | None:
        """Return the cache file path for a describe request."""
        if self._cache_dir is None:
            return None
        payload = {
            "version": _CACHE_VERSION,
            "model_fingerprint": self._model_fingerprint,
            "system_prompt": self.system_prompt,
            "parts": [part.model_dump(mode="json", exclude_none=True) for part in parts],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self._cache_dir / f"{digest}.json"

    @staticmethod
    def _read_cache(path: Path | None) -> str | None:
        """Load a cached result when present."""
        if path is None or not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        content = payload.get("content")
        return content if isinstance(content, str) else None

    @staticmethod
    def _write_cache(path: Path | None, content: str) -> None:
        """Persist a result. An empty reply is a failure, not an answer."""
        if path is None or not content:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "content": content,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
