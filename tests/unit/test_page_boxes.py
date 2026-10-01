"""Printer's PDFs: a CropBox inside a larger MediaBox. Docling and pdfium measure from the
CropBox, the style layer from the MediaBox; every box must end up in the style layer's frame."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pikepdf
import pypdfium2 as pdfium
import pytest

from pdf2xml.export.assets import page_origin


def _printers_pdf(path: Path) -> Path:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(564, 780))
    pdf.pages[0].obj.CropBox = pikepdf.Array([30, 30, 534, 750])
    pdf.save(path)
    return path


def test_rendered_page_starts_at_the_crop_origin(tmp_path: Path) -> None:
    doc = pdfium.PdfDocument(str(_printers_pdf(tmp_path / "p.pdf")))
    try:
        assert page_origin(doc[0]) == (30.0, 30.0)
    finally:
        doc.close()


def test_docling_boxes_are_moved_onto_the_media_box(tmp_path: Path) -> None:
    pytest.importorskip("docling_core")
    from docling_core.types.doc import BoundingBox, CoordOrigin

    from pdf2xml.extract.structure_docling import _crop_origins, _to_top_left

    # Docling reports the page as the 504 × 720 CropBox
    fake = SimpleNamespace(pages={1: SimpleNamespace(size=SimpleNamespace(width=504.0,
                                                                         height=720.0))})
    origins = _crop_origins(_printers_pdf(tmp_path / "p.pdf"), fake)
    assert origins == {1: (30.0, 30.0)}
    box = BoundingBox(l=72, t=102, r=348, b=110, coord_origin=CoordOrigin.TOPLEFT)
    assert _to_top_left(box, 720.0, origins[1]) == (102.0, 132.0, 378.0, 140.0)
    # a PDF whose page is the whole MediaBox needs no shift
    whole = SimpleNamespace(pages={1: SimpleNamespace(size=SimpleNamespace(width=564.0,
                                                                          height=780.0))})
    assert _crop_origins(tmp_path / "p.pdf", whole) == {}
