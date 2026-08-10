"""Tests for PDF text extraction utility."""

import pymupdf
import pytest

from lincy.tools.builtin.pdf_utils import extract_pdf_text


def _make_pdf(*page_texts: str) -> bytes:
    """Create a minimal PDF with the given page texts."""
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


class TestExtractPdfText:
    def test_single_page(self):
        pdf = _make_pdf("Hello, World!")
        result = extract_pdf_text(pdf)
        assert "## Page 1" in result
        assert "Hello, World!" in result

    def test_multi_page(self):
        pdf = _make_pdf("Page one content", "Page two content")
        result = extract_pdf_text(pdf)
        assert "## Page 1" in result
        assert "Page one content" in result
        assert "## Page 2" in result
        assert "Page two content" in result

    def test_specific_pages(self):
        pdf = _make_pdf("Alpha", "Beta", "Gamma")
        result = extract_pdf_text(pdf, pages=[0, 2])
        assert "Alpha" in result
        assert "Beta" not in result
        assert "Gamma" in result

    def test_from_file_path(self, tmp_path):
        pdf = _make_pdf("From disk")
        path = tmp_path / "test.pdf"
        path.write_bytes(pdf)
        result = extract_pdf_text(str(path))
        assert "From disk" in result

    def test_empty_pdf(self):
        doc = pymupdf.open()
        doc.new_page()  # blank page, no text
        pdf = doc.tobytes()
        doc.close()
        result = extract_pdf_text(pdf)
        assert result == "(No text content found in PDF)"

    def test_corrupt_pdf_raises(self):
        with pytest.raises(ValueError, match="Cannot open PDF"):
            extract_pdf_text(b"this is not a pdf")

    def test_from_bytes(self):
        pdf = _make_pdf("Bytes input")
        result = extract_pdf_text(pdf)
        assert "Bytes input" in result
