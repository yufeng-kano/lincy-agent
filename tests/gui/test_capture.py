"""Tests for gui/capture.py: point scaling and JPEG encoding (no real capture)."""

import base64
import io

import pytest
from PIL import Image

from lincy.gui import capture
from lincy.llm.schema import ContentPart


@pytest.fixture()
def grabbed(monkeypatch):
    calls = []

    def fake_grab(region):
        calls.append(region)
        width, height = region[2:] if region else (1440, 900)
        # Retina: backing pixels are twice the points.
        return Image.new("RGBA", (width * 2, height * 2), (10, 20, 30, 255)), width, height

    monkeypatch.setattr(capture, "_grab", fake_grab)
    return calls


def test_scales_backing_pixels_to_points(grabbed):
    part = capture.take_screenshot()
    assert isinstance(part, ContentPart)
    assert part.type == "image"
    assert part.media_type == "image/jpeg"
    assert (part.width, part.height) == (1440, 900)
    decoded = Image.open(io.BytesIO(base64.b64decode(part.data)))
    assert decoded.size == (1440, 900)
    assert decoded.mode == "RGB"


def test_max_width_applies_after_point_scaling(grabbed):
    part = capture.take_screenshot(max_width=720)
    assert (part.width, part.height) == (720, 450)


def test_max_width_larger_than_screen_is_noop(grabbed):
    part = capture.take_screenshot(max_width=4000)
    assert (part.width, part.height) == (1440, 900)


def test_region_is_in_points(grabbed):
    part = capture.take_screenshot(region=(100, 200, 300, 150))
    assert grabbed == [(100, 200, 300, 150)]
    assert (part.width, part.height) == (300, 150)


def test_quality_changes_size(grabbed, monkeypatch):
    noisy = Image.effect_noise((200, 100), 64).convert("RGBA")
    monkeypatch.setattr(capture, "_grab", lambda region: (noisy, 200, 100))
    low = capture.take_screenshot(quality=10)
    high = capture.take_screenshot(quality=95)
    assert len(low.data) < len(high.data)


def test_save_screenshot(grabbed, tmp_path):
    part = capture.take_screenshot(max_width=100)
    path = tmp_path / "shot.jpg"
    capture.save_screenshot(part, str(path))
    assert Image.open(path).size == (100, 62)
