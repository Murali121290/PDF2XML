from pdf2xml.extract.structure_heuristic import xy_cut
from pdf2xml.extract.types import PageLayer, RawBlock, StructureLayer, StyleLayer
from pdf2xml.merge.merger import merge
from pdf2xml.merge.text import runs_text
from tests.conftest import line_of, make_word


def _layers(words, blocks):  # type: ignore[no-untyped-def]
    style = StyleLayer("test", [PageLayer(1, 600, 800, words=list(words))])
    return style, StructureLayer("test", blocks)


def test_words_go_to_the_block_that_covers_them() -> None:
    w1 = line_of(["Title"], 50, 10, size=20)
    w2 = line_of(["Body", "text"], 50, 50)
    style, struct = _layers(w1 + w2, [
        RawBlock(1, (40, 5, 300, 35), "heading", 0),
        RawBlock(1, (40, 45, 300, 65), "paragraph", 1),
    ])
    res = merge(style, struct)
    assert [runs_text(b.runs) for b in res.blocks] == ["Title", "Body text"]
    assert res.orphans == 0


def test_orphans_become_new_paragraph_in_reading_order() -> None:
    top = line_of(["Top"], 50, 10)
    lost = line_of(["lost", "words"], 50, 200)
    bottom = line_of(["Bottom"], 50, 400)
    style, struct = _layers(top + lost + bottom, [
        RawBlock(1, (40, 5, 300, 25), "paragraph", 0),
        RawBlock(1, (40, 395, 300, 415), "paragraph", 1),
    ])
    res = merge(style, struct)
    assert [runs_text(b.runs) for b in res.blocks] == ["Top", "lost words", "Bottom"]
    assert res.orphans == 2
    assert res.warnings


def test_orphan_touching_a_block_joins_it() -> None:
    inside = line_of(["inside"], 50, 10)
    edge = line_of(["edge"], 50 + 40, 10)  # sticks out of the block's right edge
    style, struct = _layers(inside + edge, [RawBlock(1, (40, 5, 95, 25), "paragraph", 0)])
    res = merge(style, struct)
    assert len(res.blocks) == 1
    assert runs_text(res.blocks[0].runs) == "inside edge"


def test_words_inside_figures_are_kept_as_figure_text() -> None:
    label = line_of(["axis"], 60, 60)
    style, struct = _layers(label, [RawBlock(1, (40, 40, 300, 200), "figure", 0)])
    res = merge(style, struct)
    assert res.blocks[0].figure_text == "axis"


def test_slug_line_outside_the_trim_box_is_not_an_orphan_warning() -> None:
    body = line_of(["Body"], 50, 50)
    slug = line_of(["Job", "CH02.indd", "11"], 40, 770, size=6)    # below the trimmed page
    page = PageLayer(1, 600, 800, words=[*body, *slug], trim=(30, 30, 570, 760))
    res = merge(StyleLayer("test", [page]),
                StructureLayer("test", [RawBlock(1, (40, 45, 300, 65), "paragraph", 0)]))
    assert res.warnings == [] and res.orphans == 0
    assert [runs_text(b.runs) for b in res.blocks] == ["Body", "Job CH02.indd 11"]  # kept


def test_engine_list_marker_not_in_the_text_is_not_used() -> None:
    from pdf2xml.merge.build import _split_marker
    from pdf2xml.model import Run

    # Docling numbers list items itself; "4." is not printed before "Press; 2000."
    assert _split_marker([Run(text="Press; 2000.")], "4.") == (None, [Run(text="Press; 2000.")])
    marker, rest = _split_marker([Run(text="2. Institute of Medicine")], "3.")
    assert (marker, rest[0].text) == ("2.", "Institute of Medicine")


def _document(style, struct, artifact=None):  # type: ignore[no-untyped-def]
    from pdf2xml.merge.build import build_document
    from pdf2xml.model import Meta, PageInfo
    from pdf2xml.styles.catalogue import build_catalogue

    res = merge(style, struct)
    if artifact is not None:
        res.blocks[artifact].artifact = "header"
    meta = Meta(source="t.pdf", sha256="0" * 64, pages=1)
    return build_document(res.blocks, build_catalogue(res.blocks), meta,
                          [PageInfo(n=1, width=600, height=800)])


def test_a_table_marked_as_artifact_keeps_its_words() -> None:
    from pdf2xml.extract.types import RawCell

    words = line_of(["Running"], 50, 10) + line_of(["table"], 200, 10)
    cells = [RawCell(0, 0, bbox=(40, 5, 150, 25)), RawCell(0, 1, bbox=(190, 5, 300, 25))]
    style, struct = _layers(words + line_of(["Body"], 50, 100), [
        RawBlock(1, (40, 5, 300, 25), "table", 0, n_rows=1, n_cols=2, cells=cells),
        RawBlock(1, (40, 95, 300, 115), "paragraph", 1),
    ])
    doc = _document(style, struct, artifact=0)
    assert [a.text for a in doc.artifacts] == ["Running table"]


def test_a_footnote_between_list_items_does_not_split_the_list() -> None:
    words = (line_of(["•", "one"], 50, 50) + line_of(["1", "A", "note."], 50, 700, size=8)
             + line_of(["•", "two"], 50, 750))
    style, struct = _layers(words, [
        RawBlock(1, (40, 45, 300, 65), "list_item", 0, group="g"),
        RawBlock(1, (40, 695, 300, 712), "footnote", 1),
        RawBlock(1, (40, 745, 300, 765), "list_item", 2, group="g"),
    ])
    doc = _document(style, struct)
    assert len(doc.body) == 1 and len(doc.footnotes) == 1
    assert [runs_text(i.runs) for i in doc.body[0].items] == ["one", "two"]  # type: ignore[union-attr]


def test_a_note_number_raised_after_a_title_goes_with_its_own_line() -> None:
    from pdf2xml.merge.text import group_lines

    # "DOES TAXATION / IMPROVED GOVERNANCE?¹": the raised note touches the first line too
    first = line_of(["DOES", "TAXATION"], 77, 163, size=24)
    second = line_of(["IMPROVED", "GOVERNANCE?"], 77, 187, size=24)
    note = make_word("1", second[-1].bbox[2] + 1, 183.8, size=10.5)
    lines = group_lines([*first, *second, note])
    assert [[w.text for w in ln.words] for ln in lines] == [
        ["DOES", "TAXATION"], ["IMPROVED", "GOVERNANCE?", "1"]]


def _with_images(words, blocks, images):  # type: ignore[no-untyped-def]
    from pdf2xml.extract.types import ImageRef
    from pdf2xml.merge.build import build_document
    from pdf2xml.model import Meta, PageInfo
    from pdf2xml.styles.catalogue import build_catalogue

    page = PageLayer(1, 600, 800, words=list(words),
                     images=[ImageRef(1, b) for b in images])
    res = merge(StyleLayer("test", [page]), StructureLayer("test", blocks))
    meta = Meta(source="t.pdf", sha256="0" * 64, pages=1)
    return build_document(res.blocks, build_catalogue(res.blocks), meta,
                          [PageInfo(n=1, width=600, height=800)], [page])


def test_a_picture_no_engine_marked_becomes_a_figure() -> None:
    from pdf2xml.model import Figure

    words = line_of(["Above"], 50, 50) + line_of(["Below"], 50, 700)
    blocks = [RawBlock(1, (40, 45, 300, 65), "paragraph", 0),
              RawBlock(1, (40, 695, 300, 715), "paragraph", 1)]
    doc = _with_images(words, blocks, [(100, 200, 400, 600),     # a photo: kept
                                       (10, 300, 16, 306)])      # a bullet: not a figure
    assert [b.type for b in doc.body] == ["paragraph", "figure", "paragraph"]
    fig = doc.body[1]
    assert isinstance(fig, Figure) and fig.bbox == (100, 200, 400, 600)


def test_a_page_background_under_the_text_is_not_a_figure() -> None:
    words = [w for i in range(6) for w in line_of(["text", "on", "it"], 50, 100 + 40 * i)]
    blocks = [RawBlock(1, (40, 95 + 40 * i, 300, 115 + 40 * i), "paragraph", i)
              for i in range(6)]
    doc = _with_images(words, blocks, [(0, 0, 600, 800)])
    assert [b.type for b in doc.body] == ["paragraph"] * 6


def test_xy_cut_reads_columns_before_rows() -> None:
    title = ((50, 10, 550, 40), "title")
    left = [((50, 60 + i * 50, 290, 100 + i * 50), f"L{i}") for i in range(3)]
    right = [((310, 60 + i * 50, 550, 100 + i * 50), f"R{i}") for i in range(3)]
    order = [p for _, p in xy_cut([title, *right, *left])]
    assert order == ["title", "L0", "L1", "L2", "R0", "R1", "R2"]
