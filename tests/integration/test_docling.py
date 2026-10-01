"""Docling engine end-to-end (slow: loads ML models). Run with ``pytest -m slow``."""

from __future__ import annotations

import pytest

from pdf2xml.config import Config
from pdf2xml.extract.base import docling_available
from pdf2xml.model import Heading, ListBlock, Table
from pdf2xml.pipeline import Result, convert
from tests.conftest import PDFS

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not docling_available(), reason="docling not installed"),
]


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Result]:
    out = tmp_path_factory.mktemp("docling")
    cfg = Config(engine="docling", overlays=False)
    return {p.stem: convert(p, out, cfg) for p in sorted(PDFS.rglob("*.pdf"))}


def test_invariants(results: dict[str, Result]) -> None:
    for name, res in results.items():
        assert res.coverage.ok, (name, res.coverage.missing, res.coverage.extra)
        assert res.xml_errors == [], name


def test_table_grid_and_caption(results: dict[str, Result]) -> None:
    doc = results["table"].document
    tables = [b for b in doc.body if isinstance(b, Table)]
    assert len(tables) == 1
    t = tables[0]
    cells = [["".join(r.text for r in c.runs) for c in row] for row in t.rows]
    assert cells[0] == ["Tool", "Layer", "Licence"]
    assert cells[-1] == ["GROBID", "Papers", "Apache-2.0"]
    assert t.caption_ref is not None


def test_simple_headings_and_lists(results: dict[str, Result]) -> None:
    doc = results["simple"].document
    assert [b.level for b in doc.body if isinstance(b, Heading)][:3] == [1, 2, 3]
    lists = [b for b in doc.body if isinstance(b, ListBlock)]
    assert [(lb.ordered, len(lb.items)) for lb in lists] == [(False, 3), (True, 3)]
