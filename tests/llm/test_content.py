"""Tests for llm/content.py."""

from lincy.llm.content import content_to_text
from lincy.llm.schema import ContentPart


def test_content_to_text_handles_text_and_images():
    parts = [
        ContentPart(type="text", text="before"),
        ContentPart(type="image", media_type="image/png", data="abc"),
        ContentPart(type="text", text=" after"),
    ]

    assert content_to_text(None) == ""
    assert content_to_text("hello") == "hello"
    assert content_to_text(parts) == "before after"
