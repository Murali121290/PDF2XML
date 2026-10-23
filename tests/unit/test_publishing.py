"""BITS/JATS export: enrichment rules and schema validity on small synthetic documents."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from pdf2xml.model import (
    Artifact,
    Box,
    Caption,
    Code,
    Document,
    Figure,
    Heading,
    ListBlock,
    ListItem,
    Meta,
    PageInfo,
    Paragraph,
    Run,
    Style,
    Table,
    TableCell,
)
from pdf2xml.publishing import Metadata, Profile, export, load_metadata, load_profile
from pdf2xml.publishing.inline import LinkContext, find_links
from pdf2xml.publishing.matter import parse_imprint, parse_toc, walk_toc
from pdf2xml.publishing.schemas import validate_file
from pdf2xml.publishing.semantic import build

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
ROOT = Path(__file__).resolve().parents[2]

STYLES = {
    "body": Style(font="Times", size=10.0, role="body"),
    "h1": Style(font="Arial", size=18.0, bold=True, role="heading", level=1),
    "h2": Style(font="Arial", size=12.0, bold=True, role="heading", level=2),
    "h3": Style(font="Arial", size=11.0, bold=True, italic=True, role="heading", level=3),
    "cap": Style(font="Arial", size=9.0, role="caption"),
    "epi": Style(font="Times", size=10.0, italic=True),
    "aq": Style(font="Arial", size=6.0),
    # one catalogue level, two sizes: "REFERENCES" set small, the next section title larger
    "h2small": Style(font="Arial", size=9.0, bold=True, role="heading", level=2),
    "h2large": Style(font="Arial", size=13.0, role="heading", level=2),
}
BOX = (0.0, 0.0, 100.0, 20.0)


def h(text: str, level: int, style: str) -> Heading:
    return Heading(page=1, bbox=BOX, level=level, style=style, runs=[Run(text=text)])


def p(*runs: str | Run, style: str = "body", page: int = 1) -> Paragraph:
    rs = [r if isinstance(r, Run) else Run(text=r) for r in runs]
    return Paragraph(page=page, bbox=BOX, style=style, runs=rs)


def doc(*body: object, artifacts: list[Artifact] | None = None) -> Document:
    d = Document(meta=Meta(source="t.pdf", sha256="0" * 64, pages=2), styles=STYLES,
                 pages=[PageInfo(n=1, width=600, height=800), PageInfo(n=2, width=600, height=800)],
                 body=list(body), artifacts=artifacts or [])  # type: ignore[arg-type]
    for i, b in enumerate(d.body, 1):
        b.id = f"b{i}"
    return d


def item(marker: str, text: str, page: int = 1, y: float = 0.0) -> ListItem:
    return ListItem(page=page, bbox=(0, y, 100, y + 20), marker=marker, runs=[Run(text=text)])


def run(d: Document, tmp_path: Path, target: str = "bits", profile: Profile | None = None,
        meta: Metadata | None = None) -> etree._Element:
    res = export(d, tmp_path, target, profile or Profile(), meta or Metadata())  # type: ignore[arg-type]
    assert res.valid, res.errors
    return etree.parse(str(res.path)).getroot()


def text(el: etree._Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


# ---------------------------------------------------------------- structure

def test_title_and_nested_sections(tmp_path: Path) -> None:
    d = doc(h("A Chapter Title", 1, "h1"), p("Intro text."),
            h("INTRODUCTION", 2, "h2"), p("First."),
            h("Background", 2, "h2"), p("Second."),
            h("Detail", 3, "h3"), p("Third."),
            h("METHODS", 2, "h2"), p("Fourth."))
    root = run(d, tmp_path)
    assert text(root.find(".//book-part-meta/title-group/title")) == "A Chapter Title"
    top = root.findall("./book-part/body/sec")
    assert [text(s.find("title")) for s in top] == ["INTRODUCTION", "METHODS"]
    assert top[0].get("sec-type") == "intro"
    background = top[0].find("sec")
    assert text(background.find("title")) == "Background"
    assert text(background.find("sec/title")) == "Detail"
    assert root.find("./book-part/body/p") is not None        # text before the first section


def test_style_map_overrides_levels(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("ONE", 2, "h2"), p("a."), h("Two", 2, "h2"), p("b."))
    prof = Profile.model_validate({"sections": {"mode": "style-map", "map": {"h2": 1},
                                                "disp_level": "level{n}"}})
    secs = run(d, tmp_path, profile=prof).findall("./book-part/body/sec")
    assert [text(s.find("title")) for s in secs] == ["ONE", "Two"]
    assert {s.get("disp-level") for s in secs} == {"level1"}


def test_heading_labels_and_box(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("1.1 Goals", 2, "h2"), p("Text."),
            h("Box 2.1 Checklist", 3, "h3"), h("Step one", 3, "h3"), p("Do it."),
            h("NEXT", 2, "h2"), p("See Box 2.1 above."))
    root = run(d, tmp_path)
    sec = root.find(".//sec")
    assert text(sec.find("label")) == "1.1" and text(sec.find("title")) == "Goals"
    box = root.find(".//boxed-text")
    assert text(box.find("label")) == "Box 2.1" and text(box.find("caption/title")) == "Checklist"
    assert text(box.find("sec/title")) == "Step one"
    xref = root.find(".//xref[@ref-type='boxed-text']")
    assert xref is not None and xref.get("rid") == box.get("id")


def test_running_head_dropped_but_title_kept(tmp_path: Path) -> None:
    arts = [Artifact(kind="header", page=2, bbox=BOX, text="12 A Chapter Title")]
    d = doc(h("A Chapter Title", 1, "h1"), p("Body."),
            h("13 A CHAPTER TITLE", 2, "h2"), p("More body."), artifacts=arts)
    root = run(d, tmp_path)
    assert text(root.find(".//book-part-meta/title-group/title")) == "A Chapter Title"
    assert root.find(".//sec") is None


def test_byline_becomes_contributors(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Ann B. Smith, PhD, and Carl Doe"), p("Body."))
    root = run(d, tmp_path)
    names = [(text(c.find("name/given-names")), text(c.find("name/surname")))
             for c in root.findall(".//book-part-meta/contrib-group/contrib")]
    assert names == [("Ann B.", "Smith"), ("Carl", "Doe")]
    assert "Carl Doe" not in text(root.find(".//book-part/body"))


def test_front_matter_moves_to_meta(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"), p("Body."),
            h("ABSTRACT", 2, "h2"), p("We did things."),
            h("KEYWORDS", 2, "h2"), p("alpha, beta; gamma"))
    root = run(d, tmp_path)
    meta = root.find(".//book-part-meta")
    assert text(meta.find("abstract/p")) == "We did things."
    assert text(meta.find("abstract/title")) == "ABSTRACT"
    assert [text(k) for k in meta.findall("kwd-group/kwd")] == ["alpha", "beta", "gamma"]
    assert [text(s.find("title")) for s in root.findall(".//body/sec")] == ["INTRO"]


def test_epigraph(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Measure what matters most. —Jane Roe (2019)", style="epi"))
    dq = run(d, tmp_path).find(".//disp-quote")
    assert dq.get("content-type") == "epigraph"
    assert text(dq.find("p")) == "Measure what matters most."
    assert text(dq.find("attrib")) == "Jane Roe (2019)"


def test_paragraph_split_by_float_is_joined(tmp_path: Path) -> None:
    fig = Figure(page=1, bbox=(0, 0, 200, 200), image=None)
    d = doc(h("Title", 1, "h1"), p("This sentence runs"), fig, p("across the figure.", page=2))
    paras = run(d, tmp_path).findall(".//body/p")
    assert [text(x) for x in paras] == ["This sentence runs across the figure."]


def test_stat_tiles_are_not_joined(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("$270bn total health expenditure"), p("$89bn primary care"))
    paras = run(d, tmp_path).findall(".//body/p")
    assert [text(x) for x in paras] == ["$270bn total health expenditure", "$89bn primary care"]


def test_empty_heading_keeps_its_text(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("SECTION 1", 2, "h2"), h("SECTION 2", 2, "h2"), p("Body."))
    titles = [text(s.find("title")) for s in run(d, tmp_path).findall(".//body/sec")]
    assert titles == ["SECTION 1", "SECTION 2"]


def test_lists_types(tmp_path: Path) -> None:
    def lst(*markers: str) -> ListBlock:
        return ListBlock(page=1, bbox=BOX, items=[
            ListItem(page=1, bbox=BOX, marker=m, runs=[Run(text=f"item {m}")]) for m in markers])
    d = doc(h("Title", 1, "h1"), lst("•", "•"), lst("1.", "2."), lst("a)", "b)"), lst("i.", "ii."))
    types = [x.get("list-type") for x in run(d, tmp_path).findall(".//list")]
    assert types == ["bullet", "order", "alpha-lower", "roman-lower"]


# ---------------------------------------------------------------- floats and links

def test_table_label_foot_and_callout(tmp_path: Path) -> None:
    def cell(t: str, header: bool = False) -> TableCell:
        return TableCell(runs=[Run(text=t)], header=header)

    table = Table(page=1, bbox=BOX, n_cols=2, rows=[
        [cell("A", True), cell("B", True)], [cell("1"), cell("2")]])
    d = doc(h("Title", 1, "h1"), p("See Table 2.1 for data."), table,
            Caption(page=1, bbox=BOX, style="cap", runs=[Run(text="Table 2.1 Results")]),
            p("Source: Our survey."))
    d.body[2].caption_ref = d.body[3].id      # type: ignore[union-attr]
    d.body[3].target = d.body[2].id           # type: ignore[union-attr]
    root = run(d, tmp_path, target="jats")
    tw = root.find(".//table-wrap")
    assert text(tw.find("label")) == "Table 2.1" and text(tw.find("caption/title")) == "Results"
    assert [text(c) for c in tw.findall(".//thead/tr/th")] == ["A", "B"]
    assert text(tw.find("table-wrap-foot/p")) == "Source: Our survey."
    assert root.find(".//p/xref[@ref-type='table']").get("rid") == tw.get("id")


def test_references_author_year_and_types(tmp_path: Path) -> None:
    refs = ListBlock(page=2, bbox=BOX, items=[
        ListItem(page=2, bbox=BOX, marker="•", runs=[Run(
            text="Smith, J. (2020). A study. Journal of Tests, 5(2), 10–20.")]),
        ListItem(page=2, bbox=BOX, marker="•", runs=[Run(
            text="World Health Organization. (2021). Report title. Geneva Press.")]),
    ])
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"),
            p("As shown (Smith, 2020; WHO, 2021) and by Smith (2020)."),
            h("REFERENCES", 2, "h2"), refs)
    root = run(d, tmp_path)
    ids = [r.get("id") for r in root.findall(".//ref-list/ref")]
    assert len(ids) == 2
    assert [m.get("publication-type") for m in root.findall(".//mixed-citation")] == [
        "journal", "book"]
    assert [x.get("rid") for x in root.findall(".//sec//xref[@ref-type='bibr']")] == [
        ids[0], ids[1], ids[0]]
    assert text(root.find(".//ref-list/title")) == "REFERENCES"


def test_numeric_citations() -> None:
    from pdf2xml.publishing.semantic import Reference
    refs = [Reference([Run(text=f"Ref {i}")], id=f"r{i}", number=i) for i in (1, 2, 3)]
    spans = find_links("as shown [1, 2–3].", LinkContext(references=refs))
    assert [(s[2], s[3]["rid"]) for s in spans] == [("xref", "r1 r2 r3")]


def test_url_split_by_line_break_is_rejoined() -> None:
    t = "See https://example.org/data/ reports/file.html and doi:10.1234/ab .c5 today."
    spans = find_links(t, LinkContext())
    assert [s[3][XLINK_HREF] for s in spans] == [
        "https://example.org/data/reports/file.html", "https://doi.org/10.1234/ab.c5"]


def test_references_are_tagged_granularly(tmp_path: Path) -> None:
    printed = [
        [Run(text="Smith, J. A., & Lee, K. (2020). A study of things. "),
         Run(text="Journal of Tests, 5", italic=True),
         Run(text="(2), 10–20. https://doi.org/10.1000/xyz")],
        [Run(text="Rothman, K. J. (2019). "), Run(text="Modern epidemiology", italic=True),
         Run(text=" (4th ed.). Lippincott.")],
        [Run(text="Lee, C. (2018). Chapter name. In A. Smith & B. Jones (Eds.), "),
         Run(text="The big book", italic=True), Run(text=" (pp. 45–67). Springer.")],
    ]
    refs = ListBlock(page=2, bbox=BOX, items=[
        ListItem(page=2, bbox=BOX, marker="•", runs=r) for r in printed])
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"), p("Text (Smith & Lee, 2020)."),
            h("REFERENCES", 2, "h2"), refs)
    root = run(d, tmp_path)
    j, b, c = root.findall(".//mixed-citation")
    assert [text(m) for m in (j, b, c)] == ["".join(r.text for r in rs) for rs in printed]
    assert [text(n) for n in j.findall("person-group/string-name/surname")] == ["Smith", "Lee"]
    assert [text(n) for n in j.findall("person-group/string-name/given-names")] == [
        "J. A.", "K."]
    assert {t: text(j.find(t)) for t in ("year", "article-title", "source", "volume", "issue",
                                          "fpage", "lpage", "pub-id")} == {
        "year": "2020", "article-title": "A study of things", "source": "Journal of Tests",
        "volume": "5", "issue": "2", "fpage": "10", "lpage": "20", "pub-id": "10.1000/xyz"}
    assert j.find("source/italic") is None and j.find("pub-id").get("pub-id-type") == "doi"
    assert (text(b.find("source")), text(b.find("edition")), text(b.find("publisher-name"))) == (
        "Modern epidemiology", "4th ed.", "Lippincott")
    assert c.get("publication-type") == "book"
    assert text(c.find("chapter-title")) == "Chapter name"
    eds = c.find("person-group[@person-group-type='editor']")
    assert [text(n) for n in eds.findall("string-name/surname")] == ["Smith", "Jones"]
    assert (text(c.find("source")), text(c.find("fpage")), text(c.find("lpage"))) == (
        "The big book", "45", "67")


def test_vancouver_reference_is_tagged() -> None:
    from pdf2xml.publishing.citations import parse_reference
    t = "Smith J, Doe AB. Title of the article. N Engl J Med. 2010;362(5):123-30."
    parsed = parse_reference(t)
    assert parsed is not None
    tags = {sp.tag: t[sp.start:sp.end] for sp in parsed.spans}
    assert tags == {"person-group": "Smith J, Doe AB", "article-title": "Title of the article",
                    "source": "N Engl J Med", "year": "2010", "volume": "362", "issue": "5",
                    "fpage": "123", "lpage": "30"}
    assert parse_reference("Personal communication with the author.") is None


def test_each_author_year_pair_is_its_own_xref(tmp_path: Path) -> None:
    refs = ListBlock(page=2, bbox=BOX, items=[
        item("•", "Smith, J. (2020a). One. Press.", 2),
        item("•", "Smith, J. (2020b). Two. Press.", 2),
        item("•", "Smith, J. (2021). Three. Press.", 2),
        item("•", "World Health Organization. (2019). Report. WHO Press.", 2)])
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"),
            p("As shown (Smith, 2020a, b, 2021; Glanz, 2025) and by the World Health "
              "Organization (2019)."), h("REFERENCES", 2, "h2"), refs)
    res = export(d, tmp_path, "bits", Profile(), Metadata())
    assert res.valid, res.errors
    root = etree.parse(str(res.path)).getroot()
    xrefs = root.findall(".//sec//xref[@ref-type='bibr']")
    assert [(text(x), x.get("rid")) for x in xrefs] == [
        ("Smith, 2020a", "ref1"), ("b", "ref2"), ("2021", "ref3"),
        ("World Health Organization (2019)", "ref4")]
    assert any("Glanz 2025" in w for w in res.warnings)


# ---------------------------------------------------------------- pages, boxes, lists

def test_page_break_inside_a_joined_paragraph_and_folios(tmp_path: Path) -> None:
    arts = [Artifact(kind="header", page=1, bbox=BOX, text="12 ■ BOOK TITLE"),
            Artifact(kind="header", page=2, bbox=BOX, text="CHAPTER TITLE ■ 13")]
    d = doc(h("Title", 1, "h1"), p("This sentence runs to the foot of the page and"),
            p("goes on at the head of the next one.", page=2), artifacts=arts)
    root = run(d, tmp_path)
    para = root.find(".//body/p")
    assert text(para) == ("This sentence runs to the foot of the page and goes on at the head "
                          "of the next one.")
    targets = para.findall("target")
    assert [(t.get("id"), t.get("target-type")) for t in targets] == [
        ("page_12", "page"), ("page_13", "page")]
    assert targets[1].tail.startswith("goes on")
    meta = root.find(".//book-part-meta")
    assert (text(meta.find("fpage")), text(meta.find("lpage"))) == ("12", "13")


def test_sentence_continues_with_a_capital_after_a_page_break(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"),
            p("Policies of this kind were first recommended in 2019 by the World Health"),
            p("Organization in its annual report.", page=2))
    paras = run(d, tmp_path, profile=Profile(page_markers="none")).findall(".//body/p")
    assert [text(x) for x in paras] == [
        "Policies of this kind were first recommended in 2019 by the World Health "
        "Organization in its annual report."]


def test_page_marker_modes(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("One."), p("Two.", page=2))
    pi = run(d, tmp_path, profile=Profile(page_markers="pi"))
    assert [(x.target, x.text) for x in pi.iter(etree.ProcessingInstruction)] == [
        ("page-break", "1"), ("page-break", "2")]
    none = run(d, tmp_path, profile=Profile(page_markers="none"))
    assert none.find(".//target") is None


def test_list_item_and_list_continued_on_next_page(tmp_path: Path) -> None:
    first = ListBlock(page=1, bbox=BOX, items=[item("•", "First item."),
                                               item("•", "Second item runs on", y=700)])
    rest = ListBlock(page=2, bbox=BOX, items=[item("•", "Third item.", page=2, y=100)])
    d = doc(h("Title", 1, "h1"), first, p("to the next page.", page=2), rest)
    root = run(d, tmp_path)
    lists = root.findall(".//body/list")
    assert len(lists) == 1
    items = lists[0].findall("list-item/p")
    assert [text(x) for x in items] == ["First item.", "Second item runs on to the next page.",
                                        "Third item."]
    assert items[1].find("target").get("id") == "page_2"


def test_nested_list_is_written_nested(tmp_path: Path) -> None:
    sub = ListBlock(page=1, bbox=BOX, items=[item("–", "sub a"), item("–", "sub b")])
    top = item("1.", "Parent")
    top.children = [sub]
    d = doc(h("Title", 1, "h1"), ListBlock(page=1, bbox=BOX, ordered=True,
                                           items=[top, item("2.", "Next")]))
    lst = run(d, tmp_path).find(".//body/list")
    assert lst.get("list-type") == "order"
    assert [text(x) for x in lst.findall("list-item/list/list-item/p")] == ["sub a", "sub b"]


def test_drawn_box_becomes_boxed_text(tmp_path: Path) -> None:
    box = Box(page=1, bbox=BOX, role="tip", children=[
        h("Box 3.1 Clinical Tip", 3, "h3"), p("Do this."), h("Questions", 3, "h3"),
        p("Why?")])
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"), p("See Box 3.1."), box, p("After."))
    root = run(d, tmp_path)
    bx = root.find(".//boxed-text")
    assert bx.get("content-type") == "tip"
    assert (text(bx.find("label")), text(bx.find("caption/title"))) == ("Box 3.1", "Clinical Tip")
    assert text(bx.find("p")) == "Do this."
    assert (text(bx.find("sec/title")), text(bx.find("sec/p"))) == ("Questions", "Why?")
    assert [text(s.find("title")) for s in root.findall(".//body/sec")] == ["INTRO"]
    assert root.find(".//xref[@ref-type='boxed-text']").get("rid") == bx.get("id")


# ---------------------------------------------------------------- queries

def test_queries_modes(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Body text."), p("AQ: Please check this.", style="aq"))
    dropped = run(d, tmp_path)
    assert "AQ" not in etree.tostring(dropped, encoding="unicode")
    kept = run(d, tmp_path, profile=Profile.model_validate({"queries": {"mode": "comment"}}))
    comments = [c.text for c in kept.iter(etree.Comment)]
    assert comments == [" AQ: Please check this. "]


def test_query_leaked_into_sentence(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"),
            p("The process is not ", Run(text="AQ: Kindly review and ", size=6.0),
              "unlike the other."))
    assert text(run(d, tmp_path).find(".//body/p")) == "The process is not unlike the other."


# ---------------------------------------------------------------- tag sets, metadata, profiles

@pytest.mark.parametrize("tag_set", ["archiving", "publishing", "authoring"])
def test_jats_tag_sets_valid_with_metadata(tmp_path: Path, tag_set: str) -> None:
    d = doc(h("Title", 1, "h1"), h("ABSTRACT", 2, "h2"), p("Summary."),
            h("INTRO", 2, "h2"), p("Body."))
    meta = load_metadata(ROOT / "examples" / "metadata" / "article.yaml")
    prof = Profile.model_validate({"jats": {"tag_set": tag_set}})
    root = run(d, tmp_path, target="jats", profile=prof, meta=meta)
    assert root.get("dtd-version") == "1.4"
    assert text(root.find(".//contrib/name/surname")) == "Doe"


def test_publishing_without_journal_metadata_explains_why(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Body."))
    res = export(d, tmp_path, "jats", Profile.model_validate({"jats": {"tag_set": "publishing"}}),
                 Metadata())
    assert not res.valid
    assert res.errors[0].startswith("JATS Publishing requires a journal id")


def test_bits_book_root_and_custom_doctype(tmp_path: Path) -> None:
    d = doc(h("Chapter One", 1, "h1"), p("A."), h("Chapter Two", 1, "h1"), p("B."))
    prof = Profile.model_validate({"bits": {
        "root": "book", "doctype": '<!DOCTYPE book PUBLIC "-//X//DTD House//EN" "house.dtd">'}})
    res = export(d, tmp_path, "bits", prof, Metadata())
    assert res.valid, res.errors
    raw = res.path.read_text(encoding="utf-8")
    assert '<!DOCTYPE book PUBLIC "-//X//DTD House//EN" "house.dtd">' in raw
    root = etree.fromstring(raw.encode("utf-8"))
    assert [text(t) for t in root.findall("./book-body/book-part/book-part-meta/title-group/title")
            ] == ["Chapter One", "Chapter Two"]


def test_semantic_build_does_not_modify_the_document() -> None:
    lst = ListBlock(page=1, bbox=BOX, items=[item("•", "An item that runs on")])
    box = Box(page=1, bbox=BOX, role="tip", children=[h("TIP", 3, "h3"), p("Tip text.")])
    d = doc(h("Title", 1, "h1"), p("Joined"), p("text."), p("AQ: q", style="aq"), lst,
            p("to the next page.", page=2), box)
    before = d.model_dump_json()
    build(d, Profile(), Metadata())
    assert d.model_dump_json() == before
    b = sample_book()
    before = b.model_dump_json()
    build(b, Profile(), Metadata(), book=None)
    assert b.model_dump_json() == before


@pytest.mark.parametrize("path", sorted((ROOT / "examples").rglob("*.yaml")),
                         ids=lambda p: p.name)
def test_example_files_load(path: Path) -> None:
    if path.parent.name == "profiles":
        load_profile(path)
    else:
        load_metadata(path)


def test_validate_file_detects_schema(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), p("Body."))
    res = export(d, tmp_path, "jats", Profile(), Metadata())
    assert validate_file(res.path) == ("jats-archiving", [])


# ---------------------------------------------------------------- books: contents, front and
# back matter

BOOK_STYLES = {**STYLES, "title": Style(font="Arial", size=28.0, bold=True, role="heading",
                                        level=1)}
FOOT = (290.0, 770.0, 310.0, 780.0)


def bh(text: str, level: int, style: str, page: int, y: float = 100.0) -> Heading:
    return Heading(page=page, bbox=(72, y, 400, y + 20), level=level, style=style,
                   runs=[Run(text=text)])


def bp(*runs: str | Run, page: int, y: float = 320.0, x: float = 72.0,
       style: str = "body") -> Paragraph:
    rs = [r if isinstance(r, Run) else Run(text=r) for r in runs]
    return Paragraph(page=page, bbox=(x, y, x + 300, y + 14), style=style, runs=rs)


def sample_book() -> Document:
    """p1 cover, p2 title page, p3 copyright page, p4 dedication, p5 contents, p6 preface (vi),
    p7–p12 chapters and index, numbered 1–6."""
    folios = [(pg, t) for pg, t in zip(range(2, 7), ("ii", "iii", "iv", "v", "vi"), strict=True)]
    folios += [(pg, str(pg - 6)) for pg in range(7, 13)]
    arts = [Artifact(kind="page_number", page=pg, bbox=FOOT, text=t) for pg, t in folios]
    body: list[object] = [
        Figure(page=1, bbox=(0, 0, 600, 800), image="assets/cover.png"),
        bh("Design Futures:", 1, "title", 2, y=200), bh("A Reader", 1, "title", 2, y=240),
        bp("Edited by Jane Q. Roe", page=2, y=320),
        bp("First published 2019 by Example Press", page=3, y=300),
        bp("© Jane Q. Roe, 2019", page=3, y=320), bp("All rights reserved.", page=3, y=340),
        bp("ISBN: HB: 978-1-4725-2539-0", page=3, y=360),
        bp("For my parents", page=4, y=300),
        bh("Contents", 2, "h2", 5), bp("Preface ........ vi", page=5, y=150),
        bp("1 Introduction ........ 1", page=5, y=170),
        bp("Ann Smith", page=5, y=185, style="epi"),
        bp("2 Methods ........ 5", page=5, y=200), bp("Index ........ 6", page=5, y=215),
        bh("Preface", 2, "h2", 6), bp("Why this book.", page=6),
        bh("Introduction", 2, "h2", 7), bp("It begins here.", page=7),
        bh("Background", 3, "h3", 8), bp("Some history.", page=8),
        bh("Methods", 2, "h2", 11), bp("How it was done.", page=11),
        bh("Index", 2, "h2", 12),
        bp("Aalto, Alvar, 1, 5", page=12, y=150),
        bp("design thinking, 5; see also planning", page=12, y=170),
        bp("history of, 2", page=12, y=185, x=84),
    ]
    d = Document(meta=Meta(source="b.pdf", sha256="0" * 64, pages=12), styles=BOOK_STYLES,
                 pages=[PageInfo(n=i, width=600, height=800) for i in range(1, 13)],
                 body=body, artifacts=arts)  # type: ignore[arg-type]
    for i, b in enumerate(d.body, 1):
        b.id = f"b{i}"
    return d


def test_book_front_matter_contents_and_chapters(tmp_path: Path) -> None:
    res = export(sample_book(), tmp_path, "bits", Profile(), Metadata())
    assert res.valid, res.errors
    root = etree.parse(str(res.path)).getroot()
    assert root.tag == "book"                                  # decided from the document
    bm = root.find("book-meta")
    assert (text(bm.find(".//book-title")), text(bm.find(".//subtitle"))) == (
        "Design Futures", "A Reader")
    ed = bm.find(".//contrib")
    assert (ed.get("contrib-type"), text(ed.find("name/surname"))) == ("editor", "Roe")
    isbn = bm.find("isbn")
    assert (text(isbn), isbn.get("publication-format"), isbn.get("content-type")) == (
        "978-1-4725-2539-0", "print", "hardback")
    assert text(bm.find("publisher/publisher-name")) == "Example Press"
    assert text(bm.find("pub-date/year")) == "2019"
    assert text(bm.find("permissions/copyright-statement")) == "© Jane Q. Roe, 2019"
    fm = root.find("front-matter")
    assert [(c.tag, c.get("book-part-type")) for c in fm] == [
        ("front-matter-part", "cover"), ("front-matter-part", "title-page"),
        ("front-matter-part", "copyright-page"), ("dedication", "dedication"), ("toc", None),
        ("preface", None)]
    ids = {e.get("id"): e for e in root.iter() if e.get("id")}
    entries = []
    for te in fm.find("toc").iter("toc-entry"):
        np = te.find("nav-pointer")
        entries.append((text(te.find("label")), text(te.find("title")), text(np),
                        ids[np.get("rid")].tag))
    assert entries == [("", "Preface", "vi", "preface"), ("1", "Introduction", "1", "book-part"),
                       ("2", "Methods", "5", "book-part"), ("", "Index", "6", "index")]
    assert text(fm.find(".//toc-entry/contrib-group/contrib/name/surname")) == "Smith"
    parts = root.findall("book-body/book-part")
    assert [text(bp_.find("book-part-meta/title-group/title")) for bp_ in parts] == [
        "Introduction", "Methods"]
    assert text(parts[0].find("body/sec/title")) == "Background"
    assert (text(parts[0].find("book-part-meta/fpage")),
            text(parts[0].find("book-part-meta/lpage"))) == ("1", "4")
    assert "page_vi" in ids and "page_1" in ids                # roman, then arabic


def test_back_of_book_index(tmp_path: Path) -> None:
    root = run(sample_book(), tmp_path)
    idx = root.find("book-back/index")
    top = idx.findall("index-entry")
    assert [text(e.find("term")) for e in top] == ["Aalto, Alvar", "design thinking"]
    assert [(text(n), n.get("rid")) for n in top[0].findall("nav-pointer")] == [
        ("1", "page_1"), ("5", "page_5")]
    assert text(top[1].find("see-also-entry")) == "planning"
    assert text(top[1].find("index-entry/term")) == "history of"      # indented: a sub-entry


def test_book_in_jats_drops_the_contents_with_a_warning(tmp_path: Path) -> None:
    res = export(sample_book(), tmp_path, "jats", Profile(), Metadata())
    assert res.valid, res.errors
    assert any("table of contents is not written to JATS" in w for w in res.warnings)
    assert not any(w.startswith("TOC:") for w in res.warnings)


def test_contents_entries_without_a_heading_are_reported(tmp_path: Path) -> None:
    d = sample_book()
    d.body = [b for b in d.body if not (isinstance(b, Heading) and b.page == 11)]
    d.body.insert(-4, bh("Results", 2, "h2", 10))
    res = export(d, tmp_path, "bits", Profile(), Metadata())
    assert res.valid, res.errors
    assert any(w.startswith("TOC: 1 of 4 entries point at their printed page only")
               for w in res.warnings)
    assert any("not listed in the printed contents: Results" in w for w in res.warnings)
    root = etree.parse(str(res.path)).getroot()
    np = [n for n in root.iter("nav-pointer") if text(n) == "5"][0]
    assert np.get("rid") == "page_5"                          # the printed page, at least


def test_toc_page_numbers_in_their_own_column() -> None:
    def ln(t: str, x: float, y: float, w: float, style: str = "body") -> Paragraph:
        return Paragraph(page=3, bbox=(x, y, x + w, y + 10), style=style, runs=[Run(text=t)])

    parsed = parse_toc([
        ln("Part 1 THE DESIGNER", 150, 90, 120),
        ln("1", 30, 110, 8), ln("The Man in the Middle", 55, 110, 200),
        ln("C. Wright Mills", 55, 122, 90),
        ln("2 Good Design Is Goodwill", 30, 140, 220), ln("Paul Rand", 55, 152, 60),
        ln("23", 360, 110, 12), ln("42", 360, 140, 12)])
    part = parsed.entries[0]
    assert (part.division, part.label, part.text) == (True, "Part 1", "THE DESIGNER")
    assert [(e.label, e.text, e.folio, e.contributors) for e in part.children] == [
        ("1", "The Man in the Middle", "23", ["C. Wright Mills"]),
        ("2", "Good Design Is Goodwill", "42", ["Paul Rand"])]


def test_copyright_page_is_read() -> None:
    imp = parse_imprint([
        "ISBN: PB: 978-1-3500-1015-4", "ePDF: 978-1-3500-1018-5",
        "Identifiers: LCCN 2017049946| ISBN 9781350010154 (pbk.)",
        "Description: London ; New York : Bloomsbury Academic, an imprint of Bloomsbury "
        "Publishing Plc, 2018.", "© Jane Roe 2018", "Second edition"])
    assert [(i.value, i.fmt, i.kind) for i in imp.isbns] == [
        ("978-1-3500-1015-4", "print", "paperback"), ("978-1-3500-1018-5", "electronic", "epdf")]
    assert (imp.publisher, imp.publisher_loc, imp.year) == (
        "Bloomsbury Academic", "London; New York", 2018)
    assert (imp.copyright, imp.copyright_year, imp.edition) == (
        "© Jane Roe 2018", 2018, "Second edition")


def test_back_matter_of_a_chapter(tmp_path: Path) -> None:
    notes = ListBlock(page=1, bbox=BOX, items=[item("1.", "First note."),
                                               item("2.", "Second note. 3. Third note.")])
    d = doc(h("Title", 1, "h1"), h("INTRO", 2, "h2"),
            p("Cosplay grew fast.", Run(text="1", sup=True), " It is studied.",
              Run(text="2 3", sup=True)),
            h("Appendix A Survey", 2, "h2"), p("The questions."),
            h("Glossary", 2, "h2"), p("Cosplay: dressing up as a character."),
            p("Con: a fan convention."),
            h("About the authors", 2, "h2"), p("Ann Smith teaches design."),
            h("Further reading", 2, "h2"), p("Smith, J. (2019). A book. Example Press."),
            h("Notes", 2, "h2"), notes)
    for target in ("bits", "jats"):
        root = run(d, tmp_path, target=target)
        back = root.find(".//back")
        assert [c.tag for c in back] == ["app-group", "glossary", "bio", "ref-list", "fn-group"]
        app = back.find("app-group/app")
        assert (text(app.find("label")), text(app.find("title"))) == ("Appendix A", "Survey")
        assert [(text(di.find("term")), text(di.find("def"))) for di in back.iter("def-item")] == [
            ("Cosplay", "dressing up as a character."), ("Con", "a fan convention.")]
        assert back.find("ref-list").get("content-type") == "further-reading"
        fns = {f.get("id"): (text(f.find("label")), text(f.find("p"))) for f in back.iter("fn")}
        assert sorted(fns.values()) == [("1", "First note."), ("2", "Second note."),
                                        ("3", "Third note.")]
        calls = [(text(x), fns[x.get("rid")][0]) for x in root.iter("xref")
                 if x.get("ref-type") == "fn"]
        assert calls == [("1", "1"), ("2", "2"), ("3", "3")]
        assert [text(s.find("title")) for s in root.findall(".//body/sec")] == ["INTRO"]


def test_each_chapter_keeps_its_own_reference_list(tmp_path: Path) -> None:
    d = doc(h("One", 1, "h1"), p("As Smith (2019) showed."), h("References", 2, "h2"),
            p("Smith, J. (2019). Alpha. Journal of Things, 1(2), 3–4."),
            h("Two", 1, "h1"), p("As Smith (2019) showed."), h("References", 2, "h2"),
            p("Smith, J. (2019). Beta. Journal of Things, 5(6), 7–8."))
    root = run(d, tmp_path, profile=Profile.model_validate({"bits": {"root": "book"}}))
    parts = root.findall("book-body/book-part")
    assert len(parts) == 2
    for part in parts:
        refs = part.findall("back/ref-list/ref")
        assert len(refs) == 1
        assert part.find("body//xref[@ref-type='bibr']").get("rid") == refs[0].get("id")


def test_label_printed_above_a_heading(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("S E C T I O N 1", 3, "h3"), h("Introduction", 2, "h2"),
            p("Text."), h("4", 2, "h2"), h("Thinking Strategy", 2, "h2"), p("More."))
    secs = run(d, tmp_path).findall(".//body/sec")
    assert [(text(s.find("label")), text(s.find("title"))) for s in secs] == [
        ("SECTION 1", "Introduction"), ("4", "Thinking Strategy")]    # a bare chapter number


def test_contents_lines_run_together_are_split_and_levelled() -> None:
    def ln(t: str, x: float, y: float, page: int = 13, h_: float = 10.0) -> Paragraph:
        return Paragraph(page=page, bbox=(x, y, 420, y + h_), style="body", runs=[Run(text=t)])

    block = Code(page=13, bbox=(113, 200, 420, 240), style="body", runs=[Run(
        text="3 Information Management 16\nSection 1. Information and Communication 16\n"
             "Clarifying Patient History 16\nSection 2. Co-Production 21")])
    parsed = parse_toc([
        ln("Dedication v Acknowledgments v Preface vi", 102, 60, h_=22),   # two printed lines
        ln("PART I PRINCIPLES 1", 102, 90), ln("1 Improving Diagnosis 2", 113, 102),
        ln("Section 1. A Global Imperative 2", 127, 114),
        ln("PART II EXPERTISE 15", 102, 180), block,
        # the facing page: margins 10 pt to the left, and "10" right-aligned left of "7"
        ln("PART III INSIGHT 245", 92, 60, page=14), ln("7 Two Insights 246", 103, 72, page=14),
        ln("10 Navigating Uncertainty 258", 96, 84, page=14),
        ln("Section 1. Doubt 258", 117, 96, page=14)])
    flat = [(e.level, e.label, e.text, e.folio) for e in walk_toc(parsed.entries)]
    assert flat == [
        (1, None, "Dedication", "v"), (1, None, "Acknowledgments", "v"), (1, None, "Preface", "vi"),
        (1, "PART I", "PRINCIPLES", "1"), (2, "1", "Improving Diagnosis", "2"),
        (3, "Section 1", "A Global Imperative", "2"),
        (1, "PART II", "EXPERTISE", "15"), (2, "3", "Information Management", "16"),
        (3, "Section 1", "Information and Communication", "16"),
        (4, None, "Clarifying Patient History", "16"), (3, "Section 2", "Co-Production", "21"),
        (1, "PART III", "INSIGHT", "245"), (2, "7", "Two Insights", "246"),
        (2, "10", "Navigating Uncertainty", "258"), (3, "Section 1", "Doubt", "258")]


def test_contents_heading_read_after_its_entries(tmp_path: Path) -> None:
    d = sample_book()
    k = next(i for i, b in enumerate(d.body) if isinstance(b, Heading) and b.page == 5)
    d.body.insert(k + 5, d.body.pop(k))          # the engine reads "Contents" last on its page
    root = run(d, tmp_path)
    assert [text(t) for t in root.findall("front-matter/toc/toc-entry/title")] == [
        "Preface", "Introduction", "Methods", "Index"]


def test_running_head_text_mid_page_is_kept(tmp_path: Path) -> None:
    arts = [Artifact(kind="header", page=2, bbox=BOX, text="Part I The Principles 3")]
    d = doc(h("Title", 1, "h1"), p("Body."),
            Heading(page=1, bbox=(72, 300, 400, 320), level=2, style="h2",
                    runs=[Run(text="PART I THE PRINCIPLES 1")]), p("More."), artifacts=arts)
    sec = run(d, tmp_path).find(".//body/sec")
    assert (text(sec.find("label")), text(sec.find("title"))) == ("PART I", "THE PRINCIPLES 1")


def test_small_references_heading_does_not_swallow_the_next_section(tmp_path: Path) -> None:
    d = doc(h("Title", 1, "h1"), h("Form I", 2, "h2large"), p("As Smith (2019) showed."),
            h("REFERENCES", 2, "h2small"), p("Smith, J. (2019). Alpha. Journal, 1(2), 3–4."),
            h("Form II", 2, "h2large"), p("Body of the next section."),
            h("REFERENCES", 2, "h2small"), p("Lee, K. (2020). Beta. Journal, 5(6), 7–8."),
            p("Section 5. The Art of Examination"), p("Body text the engine left untitled."))
    root = run(d, tmp_path)
    assert [text(s.find("title")) for s in root.findall(".//body/sec")] == ["Form I", "Form II"]
    assert len(root.findall(".//ref-list/ref")) == 2
    body = text(root.find(".//book-part/body"))
    assert "Body of the next section." in body and "Body text the engine left untitled." in body


def test_running_head_paragraphs_in_the_margin_are_dropped(tmp_path: Path) -> None:
    top = (300.0, 20.0, 360.0, 30.0)
    d = doc(h("Title", 1, "h1"),
            Heading(page=1, bbox=(72, 200, 300, 220), level=2, style="h2",
                    runs=[Run(text="PREFACE")]),
            Paragraph(page=1, bbox=top, style="body", runs=[Run(text="Source: WHO")]),
            bp("Body one.", page=1),
            Paragraph(page=2, bbox=top, style="body", runs=[Run(text="PREFACE")]),
            Paragraph(page=2, bbox=(72, 20, 200, 30), style="body",
                      runs=[Run(text="Source: WHO")]),
            bp("Body two.", page=2),
            Paragraph(page=3, bbox=top, style="body", runs=[Run(text="PREFACE")]),
            bp("Body three.", page=3))
    d.pages.append(PageInfo(n=3, width=600, height=800))
    body = run(d, tmp_path).find(".//book-part/body")
    paras = [text(x) for x in body.iter("p")]
    assert "PREFACE" not in paras                   # the repeat of a heading, in the margin
    assert paras.count("Source: WHO") == 2          # repeated on two pages only: kept
    assert text(body.find("sec/title")) == "PREFACE"
