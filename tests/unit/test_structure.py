"""Canonical structure recovered from page geometry: nested lists, boxes, printer's marks."""

from __future__ import annotations

from pdf2xml.extract.types import BBox, Drawing, PageLayer, RawBlock
from pdf2xml.merge.artifacts import detect_artifacts
from pdf2xml.merge.boxes import group_boxes
from pdf2xml.merge.lists import nest_list
from pdf2xml.merge.merger import MBlock
from pdf2xml.merge.text import ARun
from pdf2xml.model import Block, Box, Heading, ListBlock, ListItem, Paragraph, Run, Table


def item(marker: str, x0: float, y: float, text: str = "item") -> ListItem:
    return ListItem(page=1, bbox=(x0, y, x0 + 200, y + 10), marker=marker, runs=[Run(text=text)])


def shape(lb: ListBlock) -> list[object]:
    """[marker, [sub-list shape]] per item, for compact assertions."""
    out: list[object] = []
    for it in lb.items:
        subs = [shape(c) for c in it.children if isinstance(c, ListBlock)]
        out.append((it.marker, subs[0]) if subs else it.marker)
    return out


# ---------------------------------------------------------------- lists

def test_sub_items_with_other_markers_and_indent_are_nested() -> None:
    lb = ListBlock(page=1, bbox=(100, 0, 300, 100), ordered=True, items=[
        item("1.", 100, 0), item("2.", 100, 12), item("•", 118, 24), item("•", 118, 36),
        item("3.", 100, 48), item("•", 118, 60), item("4.", 100, 72)])
    nested = nest_list(lb)
    assert shape(nested) == ["1.", ("2.", ["•", "•"]), ("3.", ["•"]), "4."]
    assert nested.items[1].children[0].ordered is False           # type: ignore[union-attr]
    assert len(lb.items) == 7 and not lb.items[1].children       # the input is unchanged


def test_three_levels_and_return_to_the_top() -> None:
    lb = ListBlock(page=1, bbox=(0, 0, 1, 1), items=[
        item("■", 100, 0), item("–", 112, 12), item("◦", 124, 24), item("–", 112, 36),
        item("■", 100, 48)])
    assert shape(nest_list(lb)) == [("■", [("–", ["◦"]), "–"]), "■"]


def test_same_bullet_indented_is_a_sub_list() -> None:
    lb = ListBlock(page=1, bbox=(0, 0, 1, 1), items=[
        item("•", 100, 0), item("•", 118, 12), item("•", 118, 24), item("•", 100, 36)])
    assert shape(nest_list(lb)) == [("•", ["•", "•"]), "•"]


def test_list_across_two_columns_stays_flat() -> None:
    lb = ListBlock(page=1, bbox=(0, 0, 1, 1), items=[
        item("•", 93, 700), item("•", 93, 712), item("•", 360, 100), item("•", 360, 112)])
    assert shape(nest_list(lb)) == ["•", "•", "•", "•"]


def test_right_aligned_numbers_do_not_nest() -> None:
    lb = ListBlock(page=1, bbox=(0, 0, 1, 1), ordered=True, items=[
        item("8.", 72, 0), item("9.", 72, 12), item("10.", 66, 24), item("11.", 66, 36)])
    assert shape(nest_list(lb)) == ["8.", "9.", "10.", "11."]


# ---------------------------------------------------------------- boxes

def para(y: float, text: str = "Box text.", page: int = 1, x: float = 110) -> Paragraph:
    return Paragraph(page=page, bbox=(x, y, x + 200, y + 30), runs=[Run(text=text)])


def head(y: float, text: str, page: int = 1, x: float = 110) -> Heading:
    return Heading(page=page, bbox=(x, y, x + 200, y + 12), level=2, runs=[Run(text=text)])


def rect(bbox: BBox, fill: str | None = "#f2e8f2", stroke: str | None = None,
         page: int = 1) -> Drawing:
    return Drawing(page=page, bbox=bbox, kind="rect", fill=fill, stroke=stroke, width=0.5)


def layer(n: int, *drawings: Drawing, trim: BBox | None = None) -> PageLayer:
    return PageLayer(n, 600, 800, drawings=list(drawings), trim=trim)


def test_blocks_inside_a_shaded_frame_become_a_box() -> None:
    body: list[Block] = [para(50, "Main text."), head(210, "PRAXISTIPP"), para(230),
                         para(400, "More main text.")]
    frame = (100, 200, 330, 270)
    out = group_boxes(body, [layer(1, rect(frame), rect(frame, fill=None, stroke="#801980"),
                                   rect((100, 200, 330, 222), fill="#0050e6"))])
    assert [type(b).__name__ for b in out] == ["Paragraph", "Box", "Paragraph"]
    box = out[1]
    assert isinstance(box, Box) and box.role == "tip"
    assert [type(c).__name__ for c in box.children] == ["Heading", "Paragraph"]


def test_page_frame_table_border_and_banner_are_not_boxes() -> None:
    table = Table(page=1, bbox=(110, 310, 300, 380), rows=[], n_cols=1)
    body: list[Block] = [head(60, "CHAPTER 2"), head(80, "The Title"), para(120), table]
    drawings = [
        rect((46, 46, 556, 760), fill=None, stroke="#808080"),       # trim frame: whole page
        rect((50, 50, 540, 100), fill="#001980"),                     # opener banner: headings
        rect((105, 305, 305, 385), fill=None, stroke="#000000"),      # table border: one block
    ]
    out = group_boxes(body, [layer(1, *drawings)])
    assert not any(isinstance(b, Box) for b in out)


def test_box_continued_on_next_page_is_one_box() -> None:
    body: list[Block] = [head(600, "Primary Survey"), para(620), para(100, "rest of box",
                                                                        page=2)]
    out = group_boxes(body, [layer(1, rect((100, 590, 330, 780))),
                             layer(2, rect((100, 60, 330, 160), page=2))])
    assert len(out) == 1 and isinstance(out[0], Box)
    assert [c.page for c in out[0].children] == [1, 1, 2]


# ---------------------------------------------------------------- page furniture

def mblock(page: int, bbox: BBox, text: str, kind: str = "paragraph") -> MBlock:
    run = ARun(text=text, family="Times", size=9, bold=False, italic=False, color="#000000",
               mono=False)
    return MBlock(raw=RawBlock(page, bbox, kind, 0), runs=[run])  # type: ignore[arg-type]


def test_marks_outside_the_trim_box_and_heads_inside_it() -> None:
    trim = (45.0, 45.0, 657.0, 828.0)
    blocks = []
    for p in range(1, 5):
        blocks += [mblock(p, (55, 863, 142, 869), f"book_CH02.indd {14 + p}"),   # slug line
                   mblock(p, (344, 0, 357, 14), "", kind="figure"),              # registration
                   mblock(p, (93, 75, 477, 84), f"{14 + p} Running Head Title", kind="heading"),
                   mblock(p, (93, 300, 477, 400), "Body text " * 20)]
    blocks[1].runs = []
    detect_artifacts(blocks, {p: trim for p in range(1, 5)})
    kinds = [b.artifact for b in blocks[:4]]
    assert kinds == ["other", "other", "header", None]
