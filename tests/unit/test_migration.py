"""Regression coverage for layout fixes brought over from the 0.5.3 release."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from pdf2xml import pipeline
from pdf2xml.config import Config
from pdf2xml.extract.structure_docling import (
    REQUIRED_FILES,
    REQUIRED_MODELS,
    DoclingEngine,
    resolve_models_dir,
)
from pdf2xml.extract.structure_heuristic import HeuristicEngine
from pdf2xml.extract.types import ImageRef, PageLayer
from pdf2xml.merge.build import build_document
from pdf2xml.merge.text import group_lines
from pdf2xml.model import Figure, Meta
from pdf2xml.styles.catalogue import Catalogue
from tests.conftest import line_of, make_word


def test_raised_note_joins_the_line_it_overlaps_most() -> None:
    first = line_of(["First title line"], 50, 10, size=20)
    second = line_of(["Second title line"], 50, 32, size=20)
    note = make_word("1", second[-1].x1, 27, size=12)
    lines = group_lines([*first, *second, note])
    assert [[w.text for w in line.words] for line in lines] == [
        ["First title line"], ["Second title line", "1"],
    ]


def test_picture_missed_by_layout_is_kept_as_a_figure() -> None:
    layer = PageLayer(1, 600, 800, images=[ImageRef(1, (60, 100, 300, 300))])
    doc = build_document([], Catalogue(styles={}),
                         Meta(source="cover.pdf", sha256="0" * 64, pages=1),
                         [], [layer])
    assert len(doc.body) == 1 and isinstance(doc.body[0], Figure)
    assert doc.body[0].bbox == (60, 100, 300, 300)


def test_selected_pages_and_engine_changes_invalidate_docling_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "pages.pdf"
    output = tmp_path / "out"
    page = canvas.Canvas(str(pdf))
    for number in (1, 2):
        page.drawString(72, 720, f"Page {number} contains a paragraph of ordinary readable text.")
        page.showPage()
    page.save()
    cache_present: list[bool] = []

    class RecordingDocling(DoclingEngine):
        def __init__(self) -> None:
            pass

        def analyze(self, pdf, style, pages=None, cache=None):
            cache_present.append(cache.exists())
            cache.write_text("cached layout")
            return HeuristicEngine().analyze(pdf, style, pages)

    monkeypatch.setattr(pipeline, "get_structure_engine", lambda name, models: (
        RecordingDocling() if name == "docling" else HeuristicEngine()))
    for pages in ([1], [1], [1, 2]):
        pipeline.convert(pdf, output, Config(engine="docling", targets=["json"],
                                             pages=pages, overlays=False))
    pipeline.convert(pdf, output, Config(engine="heuristic", targets=["json"], overlays=False))
    pipeline.convert(pdf, output, Config(engine="docling", targets=["json"],
                                         pages=[1, 2], overlays=False))
    assert cache_present == [False, True, False, False]


def test_offline_models_require_both_layout_and_table_models(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing layout model"):
        resolve_models_dir(tmp_path)
    (tmp_path / "docling-project--docling-layout-heron").mkdir()
    with pytest.raises(ValueError, match="tableformer model"):
        resolve_models_dir(tmp_path)
    (tmp_path / "docling-project--docling-models").mkdir()
    for kind, folder in REQUIRED_MODELS.items():
        for name in REQUIRED_FILES[kind]:
            target = tmp_path / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"test model file")
    assert resolve_models_dir(tmp_path) == tmp_path
