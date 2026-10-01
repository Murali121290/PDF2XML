"""Boxes: text set inside a shaded or ruled frame (sidebars, tips, case studies, key points).

Structure engines report the blocks inside a box as ordinary headings and paragraphs, so a box
title reads as a section heading and the box text runs into the main text. The page drawings
show where the boxes are. Every filled or stroked rectangle that encloses at least two blocks,
some of them running text, becomes a canonical ``Box`` holding those blocks. A box that runs off
the foot of a page and carries on at the head of the next one becomes one box.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pdf2xml.extract.types import BBox, Drawing, PageLayer
from pdf2xml.model import Block, Box, Caption, Figure, Heading, ListBlock, Paragraph, Table
from pdf2xml.model.document import BoxRole

_MIN_W, _MIN_H = 100.0, 30.0
_MAX_PAGE_SHARE = 0.6          # larger frames are page backgrounds or the trim frame
_TOL = 3.0

_ROLES: tuple[tuple[BoxRole, re.Pattern[str]], ...] = (
    ("warning", re.compile(r"\b(warning|caution|danger|alert|red flag|achtung|vorsicht)", re.I)),
    # word endings too: German compounds such as "Praxistipp"
    ("tip", re.compile(r"(tips?|tipps?|hints?|pearls?)\b", re.I)),
    ("example", re.compile(r"\b(example|case study|case|scenario|vignette|fallbeispiel|"
                           r"beispiel|in action)\b", re.I)),
    ("exercise", re.compile(r"\b(exercise|activity|questions?|quiz|try it|practice|fragen|"
                            r"übung)\b", re.I)),
    ("key_point", re.compile(r"\b(key (points?|terms|concepts|messages?)|summary|objectives|"
                             r"learning outcomes|takeaways?|lernziele|merke)\b", re.I)),
    ("definition", re.compile(r"\b(definition|glossary)\b", re.I)),
    ("note", re.compile(r"\b(note|notes|hinweis|remember|for more information|further reading|"
                        r"für weitere informationen)\b", re.I)),
)


@dataclass
class _Frame:
    page: int
    bbox: BBox
    fill: str | None


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _union(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _inside(inner: BBox, outer: BBox, tol: float = _TOL) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _frames(layer: PageLayer) -> list[_Frame]:
    """Box outlines on one page: large, visible rectangles, overlapping ones merged, and only
    the outermost of nested ones (a coloured title band inside a box is part of the box)."""
    frame = layer.frame
    page_area = _area(frame)
    cands: list[_Frame] = []
    for d in layer.drawings:
        if d.kind == "line" or not _visible(d):
            continue
        b = d.bbox
        if (b[2] - b[0] < _MIN_W or b[3] - b[1] < _MIN_H or _area(b) > _MAX_PAGE_SHARE * page_area
                or _inter(b, frame) < 0.9 * _area(b)):
            continue
        cands.append(_Frame(layer.number, b, d.fill))
    # One box is often several drawings: a fill plus an outline, a coloured title band inside
    # it, or stacked strips of one colour. Merge nested rectangles, and overlapping ones of one
    # colour; the box keeps the colour of its largest part.
    merged = True
    while merged:
        merged = False
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                fa, fb = cands[i], cands[j]
                small = min(_area(fa.bbox), _area(fb.bbox))
                nested = _inside(fa.bbox, fb.bbox) or _inside(fb.bbox, fa.bbox)
                same = fa.fill == fb.fill or fa.fill is None or fb.fill is None
                if nested or (same and small and _inter(fa.bbox, fb.bbox) >= 0.3 * small):
                    big, other = (fa, fb) if _area(fa.bbox) >= _area(fb.bbox) else (fb, fa)
                    cands[i] = _Frame(fa.page, _union(fa.bbox, fb.bbox), big.fill or other.fill)
                    del cands[j]
                    merged = True
                    break
            if merged:
                break
    return [c for c in cands if not _is_grid(c.bbox, layer.drawings)]


def _visible(d: Drawing) -> bool:
    if d.fill is not None and d.fill != "#ffffff":
        return True
    return d.stroke is not None and d.stroke != "#ffffff" and d.width > 0


def _is_grid(b: BBox, drawings: Sequence[Drawing]) -> bool:
    """Two or more column rules inside the frame: a ruled table, not a box."""
    h = b[3] - b[1]
    rules = [d for d in drawings if d.kind == "line" and d.bbox[2] - d.bbox[0] < 1.5
             and d.bbox[3] - d.bbox[1] > 0.6 * h
             and b[0] + _TOL < d.bbox[0] < b[2] - _TOL and _inside(d.bbox, b)]
    return len({round(d.bbox[0]) for d in rules}) >= 2


def _role(children: Sequence[Block]) -> BoxRole:
    title = next((b for b in children if isinstance(b, Heading)), None)
    if title is not None:
        text = "".join(r.text for r in title.runs)
        for role, rx in _ROLES:
            if rx.search(text):
                return role
    return "sidebar"


def _is_box_content(blocks: Sequence[Block]) -> bool:
    """At least two blocks, some of them running text (not a figure with its caption, not a
    table in its border, not a chapter-opener banner of headings)."""
    if len(blocks) < 2:
        return False
    if not any(isinstance(b, Paragraph | ListBlock) for b in blocks):
        return False
    return not all(isinstance(b, Figure | Caption | Table) for b in blocks)


def group_boxes(body: list[Block], layers: Sequence[PageLayer]) -> list[Block]:
    """Wrap the blocks inside each box frame into a ``Box``; returns the new body list."""
    by_page = {p.number: p for p in layers}
    frames: dict[int, list[_Frame]] = {n: _frames(p) for n, p in by_page.items()}
    boxes: list[tuple[Box, _Frame]] = []
    taken: dict[int, Box] = {}             # id(block) → the box it moves into
    first: dict[int, Box] = {}             # id(first block of a box) → box
    for fr in (f for n in sorted(frames) for f in frames[n]):
        inside: list[Block] = [b for b in body if b.page == fr.page and id(b) not in taken
                               and not isinstance(b, Box) and _inside(b.bbox, fr.bbox)]
        # the rest of a box from the page before may be a single paragraph
        cont = (inside and isinstance(inside[0], Paragraph | ListBlock)
                and any(_continues_onto(f, fr, by_page) for _, f in boxes))
        if not (_is_box_content(inside) or cont):
            continue
        box = Box(page=fr.page, bbox=fr.bbox, role=_role(inside), children=inside)
        boxes.append((box, fr))
        for child in inside:
            taken[id(child)] = box
        first[id(inside[0])] = box
    if not boxes:
        return body

    out: list[Block] = []
    for blk in body:
        if id(blk) in first:
            out.append(first[id(blk)])
        elif id(blk) not in taken:
            out.append(blk)
    return _join_continued(out, boxes, by_page)


def _join_continued(body: list[Block], boxes: list[tuple[Box, _Frame]],
                    pages: dict[int, PageLayer]) -> list[Block]:
    """A box that reaches the foot of its page and a box of the same colour at the head of the
    next page, starting without a title, are one box split by the page break."""
    frame_of = {id(b): f for b, f in boxes}
    drop: set[int] = set()
    order = [b for b in body if isinstance(b, Box) and id(b) in frame_of]
    if not order:
        return body
    head = order[0]                          # the box that collects continued parts
    last = frame_of[id(head)]                # the frame of its latest part
    for b in order[1:]:
        fb = frame_of[id(b)]
        if _continues_onto(last, fb, pages) and not isinstance(b.children[0], Heading):
            head.children.extend(b.children)
            drop.add(id(b))
        else:
            head = b
        last = fb
    return [b for b in body if id(b) not in drop]


def _continues_onto(a: _Frame, b: _Frame, pages: dict[int, PageLayer]) -> bool:
    """Frame ``b`` at the head of the page after ``a``, which reaches the foot of its page, in
    the same colour: one box broken by the page turn."""
    if b.page != a.page + 1 or a.fill != b.fill:
        return False
    pa, pb = pages[a.page].frame, pages[b.page].frame
    return (a.bbox[3] >= pa[3] - 0.2 * (pa[3] - pa[1])
            and b.bbox[1] <= pb[1] + 0.2 * (pb[3] - pb[1]))
