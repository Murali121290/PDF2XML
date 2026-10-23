from pdf2xml.extract.structure_heuristic import xy_cut
from pdf2xml.extract.types import PageLayer, RawBlock, StructureLayer, StyleLayer
from pdf2xml.merge.merger import merge
from pdf2xml.merge.text import runs_text
from tests.conftest import line_of


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


def test_xy_cut_reads_columns_before_rows() -> None:
    title = ((50, 10, 550, 40), "title")
    left = [((50, 60 + i * 50, 290, 100 + i * 50), f"L{i}") for i in range(3)]
    right = [((310, 60 + i * 50, 550, 100 + i * 50), f"R{i}") for i in range(3)]
    order = [p for _, p in xy_cut([title, *right, *left])]
    assert order == ["title", "L0", "L1", "L2", "R0", "R1", "R2"]
