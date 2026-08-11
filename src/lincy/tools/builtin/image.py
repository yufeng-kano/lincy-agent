"""read_image tool: reads image files and returns multimodal content."""

import base64
import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...llm.schema import ContentPart, ToolDefinition, ToolParameter
from ..security import resolve_allowed_path
from markdownify import markdownify

if TYPE_CHECKING:
    from .vision import VisionAgent

logger = logging.getLogger(__name__)

MAX_LONG_EDGE = 1568  # Anthropic limit; safe for all providers
RESIZE_JPEG_QUALITY = 85

IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
IMAGE_EXTENSIONS_BY_MIME = {mime: ext for ext, mime in IMAGE_MIME_TYPES.items()}
_SUPPORTED_EXTENSIONS = set(IMAGE_MIME_TYPES)


def html_to_markdown(html: str) -> str:
    """Convert HTML into readable Markdown without executable elements."""
    return markdownify(
        html,
        strip=["script", "style", "noscript", "template"],
        heading_style="ATX",
    ).strip()

READ_IMAGE_DEFINITION = ToolDefinition(
    name="read_image",
    description=(
        "Read an image file and return its contents for visual analysis. "
        "Supports PNG, JPEG, GIF, WebP, and BMP formats. "
        "The image will be processed so you can describe and analyze it."
    ),
    parameters={
        "path": ToolParameter(
            type="string",
            description="Path to the image file (relative to working directory or absolute).",
        ),
    },
    required=["path"],
)


def _read_image_data(
    path: str,
    allowed_paths: list[str],
    base_dir: Path,
) -> tuple[str, str, int, int]:
    """Read image file and return (base64_data, media_type, width, height).

    Raises ValueError on invalid path/format, FileNotFoundError if missing.
    """
    target = resolve_allowed_path(path, allowed_paths, base_dir)
    if target is None:
        raise ValueError(f"Path not allowed: {path}")

    if not target.exists():
        raise FileNotFoundError(f"Image not found: {path}")

    ext = target.suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported image format: {ext}. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    media_type = IMAGE_MIME_TYPES[ext]
    raw = target.read_bytes()

    # Get dimensions; resize if too large
    try:
        from PIL import Image

        with Image.open(target) as img:
            width, height = img.size
            long_edge = max(width, height)

            if long_edge > MAX_LONG_EDGE:
                ratio = MAX_LONG_EDGE / long_edge
                new_w = int(width * ratio)
                new_h = int(height * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=RESIZE_JPEG_QUALITY)
                raw = buf.getvalue()
                media_type = "image/jpeg"
                width, height = new_w, new_h
                logger.info("Resized %s: %d -> %dx%d", path, long_edge, new_w, new_h)
    except ImportError:
        logger.warning("Pillow not installed; image dimensions unknown")
        width, height = 0, 0
    except Exception:
        width, height = 0, 0

    b64 = base64.b64encode(raw).decode("ascii")
    return b64, media_type, width, height


def create_read_image_vision(
    allowed_paths: list[str],
    base_dir: Path,
) -> Callable[..., str | list[ContentPart]]:
    """Create read_image tool that returns multimodal content for vision-capable brain."""

    def read_image(path: str = "", **kwargs: Any) -> str | list[ContentPart]:
        p = path or kwargs.get("file_path", "")
        if not p:
            return "Error: path is required."
        try:
            b64, media_type, width, height = _read_image_data(
                p, allowed_paths, base_dir,
            )
        except (ValueError, FileNotFoundError) as e:
            return f"Error: {e}"

        return [
            ContentPart(
                type="text",
                text=f"[Image: {p} ({width}x{height})]",
            ),
            ContentPart(
                type="image",
                media_type=media_type,
                data=b64,
                width=width,
                height=height,
            ),
        ]

    return read_image


def create_read_image_adaptive(
    allowed_paths: list[str],
    base_dir: Path,
    vision_agent: "VisionAgent | None",
    own_vision_active: Callable[[], bool],
) -> Callable[..., str | list[ContentPart]]:
    """Own-vision when the active LLM can see images; otherwise sub-agent text."""

    own = create_read_image_vision(allowed_paths, base_dir)
    sub = (
        create_read_image_with_sub_agent(allowed_paths, base_dir, vision_agent)
        if vision_agent is not None
        else None
    )

    def read_image(path: str = "", **kwargs: Any) -> str | list[ContentPart]:
        if own_vision_active():
            return own(path=path, **kwargs)
        if sub is not None:
            return sub(path=path, **kwargs)
        return (
            "Error: current model lacks vision and no vision sub-agent is available."
        )

    return read_image


def create_read_image_with_sub_agent(
    allowed_paths: list[str],
    base_dir: Path,
    vision_agent: "VisionAgent",
) -> Callable[..., str]:
    """Create read_image tool that uses a vision sub-agent to describe the image."""

    def read_image(path: str = "", **kwargs: Any) -> str:
        p = path or kwargs.get("file_path", "")
        if not p:
            return "Error: path is required."
        try:
            b64, media_type, width, height = _read_image_data(
                p, allowed_paths, base_dir,
            )
        except (ValueError, FileNotFoundError) as e:
            return f"Error: {e}"

        image_parts = [
            ContentPart(
                type="text",
                text=f"Describe this image ({p}, {width}x{height}):",
            ),
            ContentPart(
                type="image",
                media_type=media_type,
                data=b64,
                width=width,
                height=height,
            ),
        ]
        try:
            description = vision_agent.describe(image_parts)
            return f"[Image: {p} ({width}x{height})]\n{description}"
        except Exception as e:
            logger.warning("Vision sub-agent failed for %s: %s", p, e)
            return f"[Image: {p} ({width}x{height})]\n(Vision analysis unavailable: {e})"

    return read_image


READ_IMAGE_BY_SUBAGENT_DEFINITION = ToolDefinition(
    name="read_image_by_subagent",
    description=(
        "Read an image file using an independent vision sub-agent. "
        "The sub-agent has NO access to our conversation context, "
        "so you MUST provide a complete and clear description in the 'context' parameter "
        "of what to look for or analyze in the image. "
        "Supports PNG, JPEG, GIF, WebP, and BMP formats."
    ),
    parameters={
        "path": ToolParameter(
            type="string",
            description="Path to the image file (relative to working directory or absolute).",
        ),
        "context": ToolParameter(
            type="string",
            description=(
                "Complete instructions for the vision agent describing what to analyze. "
                "Include all relevant context since the agent cannot see our conversation."
            ),
        ),
    },
    required=["path", "context"],
)


def create_read_image_by_subagent(
    allowed_paths: list[str],
    base_dir: Path,
    vision_agent: "VisionAgent",
) -> Callable[..., str]:
    """Create read_image_by_subagent tool that delegates to a vision sub-agent with explicit context."""

    def read_image_by_subagent(
        path: str = "", context: str = "", **kwargs: Any,
    ) -> str:
        p = path or kwargs.get("file_path", "")
        if not p:
            return "Error: path is required."
        if not context:
            return "Error: context is required."
        try:
            b64, media_type, width, height = _read_image_data(
                p, allowed_paths, base_dir,
            )
        except (ValueError, FileNotFoundError) as e:
            return f"Error: {e}"

        image_parts = [
            ContentPart(
                type="text",
                text=context,
            ),
            ContentPart(
                type="image",
                media_type=media_type,
                data=b64,
                width=width,
                height=height,
            ),
        ]
        try:
            description = vision_agent.describe(image_parts)
            return f"[Image: {p} ({width}x{height})]\n{description}"
        except Exception as e:
            logger.warning("Vision sub-agent failed for %s: %s", p, e)
            return f"[Image: {p} ({width}x{height})]\n(Vision analysis unavailable: {e})"

    return read_image_by_subagent
