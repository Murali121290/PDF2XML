"""Publishing regressions for figures, chapter labels and shared bibliographies."""

from pathlib import Path

from pdf2xml.model import Figure, Heading, ListBlock, Paragraph, Run
from pdf2xml.publishing.matter import runs_text
from tests.unit.test_publishing import (
    BOX,
    XLINK_HREF,
    bh,
    bp,
    doc,
    h,
    item,
    p,
    run,
    sample_book,
    text,
)


def test_figure_after_a_reference_list_is_kept(tmp_path: Path) -> None:
    fig = Figure(page=2, bbox=(100, 100, 300, 300), image="assets/art.png")
    d = doc(h("A Chapter Title", 1, "h1"), p("Body text."), h("References", 2, "h2"),
            ListBlock(page=1, bbox=BOX, items=[item("1.", "Smith J. A study. Nature. 2020.")]),
            fig)
    root = run(d, tmp_path)
    assert [g.get(XLINK_HREF) for g in root.iter("graphic")] == ["assets/art.png"]
    assert len(root.findall(".//ref")) == 1


def test_chapter_opening_art_belongs_to_its_chapter(tmp_path: Path) -> None:
    d = sample_book()
    at = next(i for i, b in enumerate(d.body) if isinstance(b, Heading)
              and runs_text(b.runs) == "Methods")
    d.body[at:at] = [bh("References", 3, "h3", 10),
                     ListBlock(page=10, bbox=BOX, items=[item("1.", "Roe J. Design. 2018.",
                                                             page=10)]),
                     Figure(page=11, bbox=(100, 100, 300, 300), image="assets/art.png")]
    root = run(d, tmp_path)
    parts = {text(part.find("book-part-meta/title-group/title")): part
             for part in root.iter("book-part")}
    assert [g.get(XLINK_HREF) for g in parts["Methods"].iter("graphic")] == ["assets/art.png"]
    assert not list(parts["Introduction"].iter("graphic"))
    assert len(parts["Introduction"].findall(".//ref")) == 1


def test_shared_bibliography_and_endnotes_link_to_the_correct_chapter(tmp_path: Path) -> None:
    d = sample_book()
    at = next(i for i, b in enumerate(d.body) if isinstance(b, Heading)
              and runs_text(b.runs) == "Methods")
    d.body[at:at + 2] = [
        bp("Chapter 2", page=11, y=60), bh("Methods", 2, "h2", 11),
        bp(Run(text="As Roe (2018) argued."), Run(text="1", sup=True), page=11)]
    toc_at = next(i for i, b in enumerate(d.body) if isinstance(b, Paragraph)
                  and runs_text(b.runs).startswith("Index ...."))
    d.body[toc_at:toc_at] = [bp("Bibliography ........ 6", page=5, y=206),
                             bp("Notes ........ 6", page=5, y=210)]
    idx = next(i for i, b in enumerate(d.body) if isinstance(b, Heading)
               and runs_text(b.runs) == "Index")
    d.body[idx:idx] = [
        bh("Bibliography", 2, "h2", 12, y=60),
        ListBlock(page=12, bbox=BOX, items=[
            item("", "Roe, J. (2018) Design futures. London: Example Press.", page=12)]),
        bh("Notes", 2, "h2", 12, y=90), bh("Chapter 2", 3, "h3", 12, y=100),
        bp("1 A note on the methods.", page=12, y=110)]
    root = run(d, tmp_path)
    methods = next(part for part in root.iter("book-part")
                   if text(part.find("book-part-meta/title-group/title")) == "Methods")
    assert text(methods.find("book-part-meta/title-group/label")) == "Chapter 2"
    ref_ids = [ref.get("id") for ref in root.findall("book-back//ref-list/ref")]
    assert len(ref_ids) == 1 and not methods.findall(".//ref-list")
    assert [x.get("rid") for x in methods.iter("xref") if x.get("ref-type") == "bibr"] == ref_ids
    note = root.find("book-back/notes//fn")
    assert note is not None
    assert [x.get("rid") for x in methods.iter("xref") if x.get("ref-type") == "fn"] == [
        note.get("id")]
