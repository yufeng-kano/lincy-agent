"""Screen capture in screen points (1 pixel = 1 point)."""

import base64
import io
from pathlib import Path

from ..llm.schema import ContentPart


def _grab(region: tuple[int, int, int, int] | None):
    """Capture region (points) or the main display.

    Returns (image at backing resolution, width in points, height in points).
    """
    import Quartz
    from PIL import Image

    # The main display's bounds start at (0, 0), which keeps image pixels
    # aligned with the AX/CGEvent global point space.
    rect = (
        Quartz.CGRectMake(*region) if region is not None
        else Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    )
    cg_image = Quartz.CGWindowListCreateImage(
        rect, Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault,
    )
    if cg_image is None:
        raise RuntimeError("screen capture failed; check the Screen Recording permission")
    info = Quartz.CGImageGetBitmapInfo(cg_image)
    byte_order = info & Quartz.kCGBitmapByteOrderMask
    alpha = info & Quartz.kCGBitmapAlphaInfoMask
    if (
        Quartz.CGImageGetBitsPerPixel(cg_image) != 32
        or byte_order != Quartz.kCGBitmapByteOrder32Little
        or alpha not in (Quartz.kCGImageAlphaPremultipliedFirst, Quartz.kCGImageAlphaNoneSkipFirst)
    ):
        raise RuntimeError(f"unsupported screenshot pixel format (bitmap info {info})")
    width = Quartz.CGImageGetWidth(cg_image)
    height = Quartz.CGImageGetHeight(cg_image)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cg_image)))
    img = Image.frombuffer(
        "RGBA", (width, height), data, "raw", "BGRA",
        Quartz.CGImageGetBytesPerRow(cg_image), 1,
    )
    return img, round(rect.size.width), round(rect.size.height)


def take_screenshot(
    *,
    max_width: int | None = None,
    quality: int = 80,
    region: tuple[int, int, int, int] | None = None,
) -> ContentPart:
    """Capture the screen as a base64 JPEG ContentPart.

    Args:
        max_width: Resize proportionally if wider (after the point scaling).
        quality: JPEG quality (1-100).
        region: Optional crop (x, y, width, height) in screen points.
    """
    from PIL import Image

    img, width_pt, height_pt = _grab(region)
    img = img.convert("RGB")
    # Retina captures come at backing scale; scale down so one pixel is one
    # point and bboxes from the AX tree can be drawn or read directly.
    if img.size != (width_pt, height_pt):
        img = img.resize((width_pt, height_pt), Image.LANCZOS, reducing_gap=2.0)
    if max_width is not None and img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return ContentPart(
        type="image",
        media_type="image/jpeg",
        data=base64.b64encode(buf.getvalue()).decode("ascii"),
        width=img.width,
        height=img.height,
    )


def save_screenshot(part: ContentPart, path: str) -> None:
    Path(path).write_bytes(base64.b64decode(part.data or ""))


def coarse_fingerprint(part: ContentPart) -> bytes:
    """A tiny grayscale thumbnail of a screenshot, for change detection.

    At 32x18 with 16 gray levels a blinking caret or the menu-bar clock
    disappears, while scrolled content or a new dialog still changes it.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(part.data or "")))
    return img.convert("L").resize((32, 18), Image.BILINEAR).point(lambda v: v >> 4).tobytes()
