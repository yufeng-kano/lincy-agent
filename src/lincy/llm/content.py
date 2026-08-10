"""Utilities for multimodal content handling."""

from .schema import ContentPart


def content_to_text(content: str | list[ContentPart] | None) -> str:
    """Extract plain text from content, skipping image parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content if part.type == "text" and part.text)
