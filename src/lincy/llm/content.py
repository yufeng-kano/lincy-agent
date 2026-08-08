"""Utilities for multimodal content handling."""

import json
import math
from typing import Any

from .schema import ContentPart


def content_to_text(content: str | list[ContentPart] | None) -> str:
    """Extract plain text from content, skipping image parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if part.type == "text" and part.text:
            parts.append(part.text)
    return "".join(parts)


def content_char_estimate(
    content: str | list[ContentPart] | None,
    provider: str = "openai",
) -> int:
    """Estimate character cost of content with provider-aware image sizing."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content:
        if part.type == "text" and part.text:
            total += len(part.text)
        elif part.type == "image":
            total += _image_char_estimate(
                part.width or 0, part.height or 0, provider,
            )
    return total


def reasoning_details_char_estimate(
    details: list[dict[str, Any]] | None,
) -> int:
    """Estimate character cost of reasoning_details for truncation budgeting."""
    if not details:
        return 0
    # Reasoning details are serialized as JSON in the API payload.
    # Use json.dumps length as a conservative estimate.
    return len(json.dumps(details, ensure_ascii=False))


def _image_char_estimate(width: int, height: int, provider: str) -> int:
    """Estimate character cost of an image by provider formula.

    Each provider prices image tokens differently:
    - OpenAI/compatible: tile-based (170 * tiles + 85) * 4 chars/token
    - Anthropic: pixel area / 750 * 4
    - Gemini: flat 258 tokens * 4
    """
    if provider in ("anthropic", "heyroute"):
        if width <= 0 or height <= 0:
            return 1000
        return (width * height) // 750 * 4
    if provider in ("gemini",):
        return 258 * 4
    # OpenAI / copilot / openrouter / ollama: tile-based
    if width <= 0 or height <= 0:
        return 1000
    tiles_w = math.ceil(width / 512)
    tiles_h = math.ceil(height / 512)
    return (170 * tiles_w * tiles_h + 85) * 4
