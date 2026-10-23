"""End-to-end checks on every fixture PDF (heuristic engine: fast, no ML)."""

from __future__ import annotations

import json

import docx
import pytest
from epubcheck import EpubCheck

from pdf2xml.config import Config
from pdf2xml.model import Heading, ListBlock, Paragraph, iter_blocks
from pdf2xml.pipeline import Result, convert
from tests.conftest import ALL_PDFS, EXPECTED, PDFS

NAMES = [p.stem for p in ALL_PDFS]


@pytest.mark.parametrize("name", NAMES)
def test_invariants(heuristic_results: dict[str, Result], name: str) -> None:
    res = heuristic_results[name]
    assert res.coverage.ok, (res.coverage.missing, res.coverage.extra)
    assert res.xml_errors == []
    ids = [b.id for part in (res.document.body, res.document.footnotes)
           for b in iter_blocks(part)]
    assert len(ids) == len(set(ids))
    sizes = {p.n: (p.width, p.height) for p in res.document.pages}
    for b in iter_blocks(res.document.body):
        w, h = sizes[b.page]
        assert -1 <= b.bbox[0] <= b.bbox[2] <= w + 1 and -1 <= b.bbox[1] <= b.bbox[3] <= h + 1


@pytest.mark.parametrize("name", NAMES)
def test_outputs_are_valid(heuristic_results: dict[str, Result], name: str) -> None:
    res = heuristic_results[name]
    check = EpubCheck(str(res.files["epub"]))
    assert check.valid, [m.message for m in check.messages]
    d = docx.Document(str(res.files["docx"]))
    assert d.paragraphs


@pytest.mark.parametrize("name", NAMES)
def test_bits_and_jats_are_valid(heuristic_results: dict[str, Result], name: str) -> None:
    res = heuristic_results[name]
    assert set(res.exports) == {"bits", "jats"}
    for exp in res.exports.values():
        assert exp.valid, (exp.schema, exp.errors)
    report = json.loads((res.out_dir / "report.json").read_text("utf-8"))
    assert report["exports"]["bits"]["valid"] and report["exports"]["jats"]["valid"]


def test_simple_structure_and_styles(heuristic_results: dict[str, Result]) -> None:
    doc = heuristic_results["simple"].document
    headings = [(b.level, "".join(r.text for r in b.runs)) for b in doc.body
                if isinstance(b, Heading)]
    assert headings[:3] == [(1, "Converting PDF to XML"), (2, "1. Introduction"), (3, "1.1 Goals")]
    assert doc.styles["h1"].color == "#1a3c6e" and doc.styles["h1"].bold
    assert doc.styles["body"].font == "Times New Roman" and doc.styles["body"].size == 10.5

    runs = [r for b in doc.body if isinstance(b, Paragraph) for r in b.runs]
    assert any(r.bold and r.text.strip() == "bold words" for r in runs)
    assert any(r.italic and r.text.strip() == "italic words" for r in runs)
    assert any(r.color == "#c00000" for r in runs)
    assert any(r.sub and r.text == "2" for r in runs)
    assert any(r.sup and r.text == "2" for r in runs)

    lists = [b for b in doc.body if isinstance(b, ListBlock)]
    assert [(lb.ordered, len(lb.items)) for lb in lists] == [(False, 3), (True, 3)]
    assert [a.kind for a in doc.artifacts if a.page == 1] == ["header", "page_number"]


def test_two_column_reading_order(heuristic_results: dict[str, Result]) -> None:
    doc = heuristic_results["two_column"].document
    order = ["".join(r.text for r in b.runs)[:5] for b in doc.body if isinstance(b, Paragraph)]
    tagged = [t for t in order if t.startswith("[")]
    assert tagged == sorted(tagged), tagged


def test_output_is_deterministic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pdf = PDFS / "simple" / "simple.pdf"
    cfg = Config(engine="heuristic", overlays=False, targets=["json", "xml", "bits", "jats"])
    a = convert(pdf, tmp_path / "a", cfg)
    b = convert(pdf, tmp_path / "b", cfg)
    for name in ("20_canonical.json", "30_canonical.xml", "40_bits.xml", "41_jats.xml"):
        assert (a.out_dir / name).read_bytes() == (b.out_dir / name).read_bytes()


@pytest.mark.parametrize("name", NAMES)
def test_golden(heuristic_results: dict[str, Result], name: str, update_goldens: bool) -> None:
    got = json.loads((heuristic_results[name].out_dir / "20_canonical.json").read_text("utf-8"))
    golden = EXPECTED / f"{name}.heuristic.json"
    if update_goldens or not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(got, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        pytest.skip(f"golden written: {golden.name} (review the diff before committing)")
    assert got == json.loads(golden.read_text("utf-8"))
