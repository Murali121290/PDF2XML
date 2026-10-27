"""Completeness checks (``qa/``): each one finds what it is meant to find, and stays quiet on
a clean conversion."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from pdf2xml.extract.base import get_style_backend
from pdf2xml.extract.types import ImageRef, PageLayer, StyleLayer
from pdf2xml.model import Figure, ListBlock
from pdf2xml.pipeline import Result
from pdf2xml.publishing import Metadata, Profile, export
from pdf2xml.qa.exports import check_export
from pdf2xml.qa.images import check_images
from pdf2xml.qa.ink import check_ink
from pdf2xml.qa.textlayer import check_text_layer
from pdf2xml.qa.unicode import _LEGACY_TAMIL, _font, check_unicode
from tests.conftest import PDFS, make_word
from tests.unit.test_publishing import doc, h, item, p

SIMPLE = PDFS / "simple" / "simple.pdf"


def _xml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "out.xml"
    path.write_text(f"<book-part-wrapper><body>{body}</body></book-part-wrapper>",
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- export text

def test_export_check_tells_dropped_moved_and_missing_apart(tmp_path: Path) -> None:
    d = doc(h("3. Results", 1, "h1"), p("Kept paragraph text."), p("Chapter 2 Methods 45"),
            p("John Smith"), p("Lost words here."),
            ListBlock(page=1, bbox=(0, 0, 100, 40), ordered=True,
                      items=[item("3.", "Item one")]))
    xml = _xml(tmp_path, "<sec><title>3. Results</title><p>Kept paragraph\n text.</p>"
                         "<p><surname>Smith</surname> <given-names>John</given-names></p>"
                         "<list><list-item><p>Item one</p></list-item></list></sec>")
    res = check_export(d, xml, "bits", [("running head", 1, "Chapter 2 Methods 45"),
                                        ("list marker (given by list-type)", 1, "3.")])
    assert res.status == "warn"
    assert [m["text"] for m in res.missing] == ["Lost words here."]
    assert res.missing[0]["absent_words"] == ["Lost", "words", "here"]
    assert [m["text"] for m in res.moved] == ["John Smith"]
    # "3." is a list marker left out on purpose, although "3." is printed in the heading
    assert sorted(x["reason"] for x in res.dropped) == ["list marker (given by list-type)",
                                                       "running head"]
    assert res.found == 3


def test_export_check_reads_text_in_document_order(tmp_path: Path) -> None:
    # "research⁵ has": the citation number sits in a child element before the tail text
    d = doc(p("previous research5 has shown"))
    xml = _xml(tmp_path, '<p>previous research<xref rid="r5"><sup>5</sup></xref> has shown</p>')
    res = check_export(d, xml, "bits")
    assert (res.status, res.found) == ("ok", 1)


def test_export_ledger_records_what_the_writer_leaves_out(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Body text."), p("AQ1: Please check the date."),
            ListBlock(page=1, bbox=(0, 0, 100, 40), items=[item("•", "One"), item("•", "Two")]))
    res = export(d, tmp_path, "bits", Profile(), Metadata())
    reasons = Counter(r for r, _, _ in res.dropped)
    assert reasons["list marker (given by list-type)"] == 2
    assert reasons["author query (queries.mode: drop)"] == 1
    check = check_export(d, res.path, "bits", res.dropped)
    assert check.missing == [] and check.status in ("ok", "info")


# ---------------------------------------------------------------- unicode and fonts

def _layer(*texts: str) -> StyleLayer:
    words = [make_word(t, 10 + 60 * i, 10) for i, t in enumerate(texts)]
    return StyleLayer("test", [PageLayer(1, 600, 800, words=words)])


def test_unicode_finds_unmapped_and_scrambled_text() -> None:
    res = check_unicode(_layer("Modi(cid:31)able", "x", "ெகா", "கொ", "ாக", "ok"))
    c = res["counts"]
    assert (c["unmapped_glyphs"], c["private_use"], c["tamil_vowel_sign_order"],
            c["starts_with_mark"]) == (1, 1, 1, 2)       # "ெகா" and "ாக" start with a mark
    assert res["status"] == "warn"
    assert res["samples"]["tamil_vowel_sign_order"][0]["text"] == "ெகா"


def test_unicode_is_quiet_on_correct_tamil_and_bullets() -> None:
    bullets = doc(ListBlock(page=1, bbox=(0, 0, 100, 40), items=[item("(cid:127)", "One")]))
    res = check_unicode(_layer("கொடு", "தமிழ்", "(cid:127)"), doc=bullets)
    assert res["status"] == "info"                         # the bullet is reported, no more
    assert res["counts"] == {"unmapped_bullets": 1}


def test_font_audit_flags_fonts_whose_text_cannot_be_trusted() -> None:
    def font(**kw: object) -> dict[str, object]:
        return _font(pikepdf.Dictionary({"/" + k: v for k, v in kw.items()}))

    name = pikepdf.Name
    assert "risk" in font(Subtype=name.Type3, BaseFont=name("/ABCDEF+Glyphs"))
    assert "risk" in font(Subtype=name.Type0, BaseFont=name.Foo, Encoding=name("/Identity-H"))
    assert "risk" not in font(Subtype=name.Type1, BaseFont=name.Times,
                              Encoding=name.WinAnsiEncoding)
    odd = pikepdf.Dictionary({"/Differences": pikepdf.Array([1, name("/g31"), name("/g32")])})
    std = pikepdf.Dictionary({"/Differences": pikepdf.Array([1, name.A, name.eacute])})
    assert "risk" in font(Subtype=name.Type1, BaseFont=name.X, Encoding=odd)
    assert "risk" not in font(Subtype=name.Type1, BaseFont=name.X, Encoding=std)
    assert font(Subtype=name.Type1, BaseFont=name("/ABCDEF+Helvetica"))["name"] == "Helvetica"
    assert _LEGACY_TAMIL.search("Bamini") and not _LEGACY_TAMIL.search("Latha")


# ---------------------------------------------------------------- images

def test_images_not_in_a_figure_and_figures_not_written() -> None:
    fig = Figure(page=1, bbox=(100, 100, 300, 300), image="assets/p1_b1.png")
    d = doc(fig)
    words = [make_word("w", 50 + (i % 20) * 20, 400 + (i // 20) * 12) for i in range(60)]
    layer = PageLayer(1, 600, 800, words=words, images=[
        ImageRef(1, (110, 110, 290, 290)),            # the figure's picture
        ImageRef(1, (110, 110, 290, 290)),            # drawn twice: counted once
        ImageRef(1, (10, 10, 18, 18)),                # a bullet
        ImageRef(1, (320, 100, 520, 300)),            # a picture no engine marked
    ])
    res = check_images(StyleLayer("t", [layer]), d, {"bits": set()}, {"bits": Counter()})
    assert res["pdf_images"] == 3
    assert [m["bbox"] for m in res["missed"]] == [[320, 100, 520, 300]]
    assert res["exports"]["bits"]["figures_not_written"][0]["id"] == "b1"
    assert res["status"] == "warn"
    ok = check_images(StyleLayer("t", [PageLayer(1, 600, 800, images=layer.images[:1])]), d,
                      {"bits": {"assets/p1_b1.png"}}, {"bits": Counter()})
    assert ok["status"] == "ok"
    # a small figure the export left out on purpose is not "not written"
    small = check_images(StyleLayer("t", []), d, {"bits": set()}, {"bits": Counter({1: 1})})
    assert small["exports"]["bits"]["small_figures_left_out"] == 1
    assert small["status"] == "ok"


def test_full_page_image_under_the_text_is_a_scan_or_background() -> None:
    words = [make_word("w", 50 + (i % 20) * 20, 100 + (i // 20) * 12) for i in range(60)]
    layer = PageLayer(1, 600, 800, words=words, images=[ImageRef(1, (0, 0, 600, 800))])
    res = check_images(StyleLayer("t", [layer]), doc())
    assert res["missed"] == [] and res["background_pages"] == [1]


# ---------------------------------------------------------------- PDFium checks

def test_text_layer_and_ink_find_text_the_style_layer_lost() -> None:
    style = get_style_backend("pdfplumber").extract(SIMPLE, [1])
    pdf = pdfium.PdfDocument(str(SIMPLE))
    try:
        assert check_text_layer(style, pdf)["status"] == "ok"
        assert check_ink(style, doc(), pdf)["status"] != "warn"
        # as if pdfplumber had not read the first lines (text set as outlines, say)
        page = style.pages[0]
        top = sorted({w.bbox[1] for w in page.words})[:3]
        page.words = [w for w in page.words if w.bbox[1] not in top]
        lost = check_text_layer(style, pdf)
        assert lost["status"] == "warn" and lost["pages"][0]["not_in_style_layer"] > 10
        ink = check_ink(style, doc(), pdf)
        assert ink["status"] == "warn"
        assert any(r["kind"] == "content" for r in ink["pages"][0]["regions"])
    finally:
        pdf.close()


def test_text_read_in_the_other_case_is_not_a_loss() -> None:
    import dataclasses

    # running heads printed in capitals that a tagged PDF's replacement text spells in lower
    # case: the two readers disagree on case only
    style = get_style_backend("pdfplumber").extract(SIMPLE, [1])
    page = style.pages[0]
    page.words = [dataclasses.replace(w, text=w.text.upper()) for w in page.words]
    pdf = pdfium.PdfDocument(str(SIMPLE))
    try:
        res = check_text_layer(style, pdf)
    finally:
        pdf.close()
    assert res["status"] == "info" and res["case_differences"] > 100


# ---------------------------------------------------------------- in the pipeline

def test_every_conversion_gets_a_completeness_report(
        heuristic_results: dict[str, Result]) -> None:
    for name, res in heuristic_results.items():
        data = json.loads((res.out_dir / "qa" / "completeness.json").read_text("utf-8"))
        report = json.loads((res.out_dir / "report.json").read_text("utf-8"))
        assert report["completeness"]["status"] == data["status"], name
        assert {"text_layer", "export_bits", "export_jats", "images", "unicode",
                "ink"} <= set(data["checks"]), name
        for check, c in data["checks"].items():
            assert c["status"] != "error", (name, check, c["summary"])
        for target in ("bits", "jats"):
            assert data["checks"][f"export_{target}"]["missing"] == 0, (name, target)
        assert data["checks"]["text_layer"]["status"] == "ok", name
