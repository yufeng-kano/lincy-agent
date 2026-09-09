"""GUI Worker: single-shot screenshot analysis using Flash LLM."""

import base64
import io
import logging
import os
import time
from typing import Any

from pydantic import BaseModel

from ..llm.session import llm_session
from ..llm.base import LLMClient
from ..llm.schema import ContentPart, Message
from ..llm.json_extract import extract_json_object
from .actions import take_screenshot

logger = logging.getLogger(__name__)

_OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "bbox": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
            "minItems": 4,
            "maxItems": 4,
        },
        "found": {"type": "boolean"},
        "mismatch": {"type": ["string", "null"]},
        "obstructed": {"type": ["string", "null"]},
    },
    "required": ["description", "found"],
    "additionalProperties": False,
}


_SCREEN_DESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "crop_bbox": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
            "minItems": 4,
            "maxItems": 4,
        },
        "crop_label": {"type": ["string", "null"]},
    },
    "required": ["description"],
    "additionalProperties": False,
}


class ScreenDescription(BaseModel):
    """Result from describe_screen(): text analysis + optional crop."""

    description: str
    crop_path: str | None = None
    screenshot_sec: float = 0.0
    inference_sec: float = 0.0


class WorkerObservation(BaseModel):
    """Structured result from a Worker observation."""

    description: str
    bbox: list[int] | None = None  # [ymin, xmin, ymax, xmax] or null
    found: bool = True
    mismatch: str | None = None
    obstructed: str | None = None
    screenshot_sec: float = 0.0
    inference_sec: float = 0.0


class GUIWorker:
    """Single-shot screenshot observer using a vision-capable Flash LLM.

    Each call to observe() is stateless: fresh system + user messages.
    """

    def __init__(
        self,
        client: LLMClient,
        system_prompt: str,
        parse_retries: int = 1,
        screenshot_max_width: int | None = None,
        screenshot_quality: int = 80,
        layout_prompt: str = "",
        describe_prompt: str = "",
    ):
        self.client = client
        self.system_prompt = system_prompt
        self.layout_prompt = layout_prompt
        self.describe_prompt = describe_prompt
        self.parse_retries = parse_retries
        self._screenshot_max_width = screenshot_max_width
        self._screenshot_quality = screenshot_quality

    @llm_session("gui_worker")
    def observe(self, instruction: str) -> WorkerObservation:
        """Take screenshot, send to LLM with instruction, return observation."""
        t0 = time.monotonic()
        screenshot = take_screenshot(
            max_width=self._screenshot_max_width,
            quality=self._screenshot_quality,
        )
        t1 = time.monotonic()
        user_content: list[ContentPart] = [
            screenshot,
            ContentPart(type="text", text=instruction),
        ]
        messages = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=user_content),
        ]
        raw = self.client.chat(messages, response_schema=_OBSERVATION_SCHEMA)
        t2 = time.monotonic()
        obs = self._parse(raw)
        obs.screenshot_sec = t1 - t0
        obs.inference_sec = t2 - t1
        return obs

    @llm_session("gui_worker")
    def scan_layout(self) -> str:
        """Capture screenshot and return a comprehensive GUI layout description.

        Uses a dedicated layout prompt (no structured schema) to get a
        free-text description of all visible UI regions and elements.
        """
        if not self.layout_prompt:
            raise RuntimeError("layout_prompt not configured")

        t0 = time.monotonic()
        screenshot = take_screenshot(
            max_width=self._screenshot_max_width,
            quality=self._screenshot_quality,
        )
        t1 = time.monotonic()
        user_content: list[ContentPart] = [
            screenshot,
            ContentPart(type="text", text="Describe the complete GUI layout."),
        ]
        messages = [
            Message(role="system", content=self.layout_prompt),
            Message(role="user", content=user_content),
        ]
        raw = self.client.chat(messages)
        t2 = time.monotonic()
        logger.debug(
            "scan_layout: screenshot=%.1fs inference=%.1fs",
            t1 - t0, t2 - t1,
        )
        return raw.strip() if raw else ""

    @llm_session("gui_worker")
    def describe_screen(
        self, context: str, *, save_dir: str | None = None,
    ) -> ScreenDescription:
        """Take screenshot, analyze with context, optionally crop and save.

        The vision LLM receives a full-screen screenshot plus the caller's
        *context* string.  If the LLM identifies a region of interest it
        returns a crop_bbox; the region is then cropped from the original
        screenshot and saved as a JPEG file.
        """
        if not self.describe_prompt:
            raise RuntimeError("describe_prompt not configured")

        t0 = time.monotonic()
        screenshot = take_screenshot(
            max_width=self._screenshot_max_width,
            quality=self._screenshot_quality,
        )
        t1 = time.monotonic()

        user_content: list[ContentPart] = [
            screenshot,
            ContentPart(type="text", text=context),
        ]
        messages = [
            Message(role="system", content=self.describe_prompt),
            Message(role="user", content=user_content),
        ]
        raw = self.client.chat(
            messages, response_schema=_SCREEN_DESCRIPTION_SCHEMA,
        )
        t2 = time.monotonic()

        data = extract_json_object(raw)
        if data is None:
            # Fallback: treat raw text as description
            logger.warning("describe_screen parse failed: %s", raw[:200])
            return ScreenDescription(
                description=raw.strip(),
                screenshot_sec=t1 - t0,
                inference_sec=t2 - t1,
            )

        desc = data.get("description", raw.strip())
        crop_path: str | None = None
        crop_bbox = data.get("crop_bbox")
        if crop_bbox and len(crop_bbox) == 4 and screenshot.width and screenshot.height:
            crop_path = self._crop_and_save(
                screenshot, crop_bbox,
                label=data.get("crop_label", "crop"),
                save_dir=save_dir,
            )

        return ScreenDescription(
            description=desc,
            crop_path=crop_path,
            screenshot_sec=t1 - t0,
            inference_sec=t2 - t1,
        )

    @staticmethod
    def _crop_and_save(
        screenshot: ContentPart,
        bbox: list[int],
        *,
        label: str | None = None,
        save_dir: str | None = None,
    ) -> str | None:
        """Crop a region from the screenshot and save as JPEG.

        Args:
            screenshot: The original screenshot ContentPart (base64 JPEG).
            bbox: Gemini normalized [ymin, xmin, ymax, xmax], 0-1000.
            label: Short filename label for the saved file.
            save_dir: Directory to save the crop. Defaults to tempdir.

        Returns:
            Absolute path to the saved JPEG, or None on failure.
        """
        from PIL import Image

        try:
            img_data = base64.b64decode(screenshot.data)
            img = Image.open(io.BytesIO(img_data))

            ymin, xmin, ymax, xmax = bbox
            w, h = img.size
            left = int(xmin / 1000 * w)
            top = int(ymin / 1000 * h)
            right = int(xmax / 1000 * w)
            bottom = int(ymax / 1000 * h)

            # Clamp to image bounds
            left = max(0, min(left, w))
            top = max(0, min(top, h))
            right = max(left + 1, min(right, w))
            bottom = max(top + 1, min(bottom, h))

            cropped = img.crop((left, top, right, bottom))
            if cropped.mode != "RGB":
                cropped = cropped.convert("RGB")

            safe_label = (label or "crop").replace("/", "_")[:32]
            dest_dir = save_dir or os.path.join(
                os.path.expanduser("~"), ".cache", "lincy",
            )
            os.makedirs(dest_dir, exist_ok=True)

            ts = int(time.time())
            path = os.path.join(dest_dir, f"crop_{ts}_{safe_label}.jpg")
            cropped.save(path, format="JPEG", quality=85)
            logger.info("Cropped screenshot saved: %s", path)
            return path
        except Exception:
            logger.warning("Failed to crop screenshot", exc_info=True)
            return None

    def _parse(self, raw: str) -> WorkerObservation:
        """Parse LLM response into WorkerObservation with retries."""
        for attempt in range(self.parse_retries + 1):
            data = extract_json_object(raw)
            if data is not None:
                try:
                    return WorkerObservation.model_validate(data)
                except Exception:
                    pass
            if attempt < self.parse_retries:
                logger.debug("Worker parse retry %d: %s", attempt + 1, raw[:200])

        # Fallback: treat raw text as description
        logger.warning("Worker parse failed, using fallback: %s", raw[:200])
        return WorkerObservation(description=raw.strip(), found=False)
