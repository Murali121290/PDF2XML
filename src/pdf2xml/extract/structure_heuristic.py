"""Rule-based structure engine: no ML, fast. For simple documents, tests, and machines without
Docling. It has no table detection: table cells come out as separate paragraphs.

Lines → blocks by vertical proximity and style, reading order by recursive XY-cut, block kinds by
font size / weight / markers. Good for single- and simple multi-column born-digital PDFs only.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pdf2xml.extract.types import BBox, PageLayer, RawBlock, RawKind, StructureLayer, StyleLayer
from pdf2xml.merge.text import Line, group_lines, is_marker, is_ordered_marker

_CAPTION_RE = re.compile(r"^(fig(ure)?|table|chart|plate|exhibit)\.?\s*[\dIVX]", re.I)

T = TypeVar("T")


@dataclass
class _Group:
    lines: list[Line] = field(default_factory=list)

    @property
    def bbox(self) -> BBox:
        return (min(ln.x0 for ln in self.lines), min(ln.top for ln in self.lines),
                max(ln.x1 for ln in self.lines), max(ln.bottom for ln in self.lines))


def body_size(style: StyleLayer) -> float:
    counts: Counter[float] = Counter()
    for p in style.pages:
        for w in p.words:
            counts[round(w.size * 2) / 2] += len(w.text)
    return counts.most_common(1)[0][0] if counts else 10.0


class HeuristicEngine:
    name = "heuristic"

    def analyze(
        self,
        pdf: Path,
        style: StyleLayer,
        pages: Sequence[int] | None = None,
        cache: Path | None = None,
    ) -> StructureLayer:
        base = body_size(style)
        layer = StructureLayer(engine=self.name, blocks=[],
                               page_sizes={p.number: (p.width, p.height) for p in style.pages})
        order = 0
        list_no = 0
        for page in style.pages:
            items: list[tuple[BBox, RawBlock]] = []
            for img in page.images:
                items.append((img.bbox, RawBlock(page.number, img.bbox, "figure", 0)))
            for g in self._groups(page, base):
                items.append((g.bbox, self._classify(g, page.number, base)))
            prev_list: RawBlock | None = None
            for _bbox, blk in xy_cut(items):
                blk.order = order
                order += 1
                if blk.kind == "list_item":
                    same = (prev_list is not None and abs(prev_list.bbox[0] - blk.bbox[0]) < 30
                            and prev_list.ordered == blk.ordered)
                    if not same:
                        list_no += 1
                    blk.group = f"h-list-{list_no}"
                    prev_list = blk
                else:
                    prev_list = None
                layer.blocks.append(blk)
        return layer

    # -------------------------------------------------------------- grouping

    def _groups(self, page: PageLayer, base: float) -> list[_Group]:
        def in_image(ln: Line) -> bool:
            cx, cy = (ln.x0 + ln.x1) / 2, (ln.top + ln.bottom) / 2
            return any(i.bbox[0] <= cx <= i.bbox[2] and i.bbox[1] <= cy <= i.bbox[3]
                       for i in page.images)

        lines = [ln for ln in group_lines(page.words, split_gap=1.0) if not in_image(ln)]
        lines = _attach_markers(lines)
        groups: list[_Group] = []
        for ln in lines:
            best: _Group | None = None
            best_gap = 1e9
            for g in groups[-6:]:
                last = g.lines[-1]
                gap = ln.top - last.bottom
                if not -1.0 <= gap <= 0.5 * max(ln.size, last.size):
                    continue
                gx0, _, gx1, _ = g.bbox
                if ln.x1 < gx0 or ln.x0 > gx1:
                    continue
                if abs(ln.size - last.size) > 0.6 or _all_bold(ln) != _all_bold(last):
                    continue
                if is_marker(ln.words[0].text):
                    continue
                indented = ln.x0 - gx0 > 0.8 * ln.size
                last_short = last.x1 < gx1 - 2 * ln.size
                if indented and last_short and len(g.lines) > 1:
                    continue
                if gap < best_gap:
                    best, best_gap = g, gap
            if best is None:
                groups.append(_Group([ln]))
            else:
                best.lines.append(ln)
        return groups

    def _classify(self, g: _Group, page: int, base: float) -> RawBlock:
        words = [w for ln in g.lines for w in ln.words]
        text = " ".join(w.text for w in words)
        chars: Counter[float] = Counter()
        for w in words:
            chars[w.size] += len(w.text)
        size = chars.most_common(1)[0][0]
        bold = all(w.bold for w in words)
        first = g.lines[0].words[0].text

        kind: RawKind = "paragraph"
        ordered = None
        marker = None
        large = size >= base * 1.15 and len(text) < 200
        if _CAPTION_RE.match(text) and size <= base + 0.5:
            kind = "caption"
        elif large:                      # before lists: "1. Introduction" is a heading
            kind = "heading"
        elif is_marker(first) and len(words) > 1:
            kind = "list_item"
            marker = first
            ordered = is_ordered_marker(first)
        elif all(w.mono for w in words):
            kind = "code"
        elif bold and len(g.lines) <= 2 and len(text) < 120 and not text.rstrip().endswith("."):
            kind = "heading"
        return RawBlock(page=page, bbox=g.bbox, kind=kind, order=0, text=text,
                        ordered=ordered, marker=marker)


def _all_bold(ln: Line) -> bool:
    return all(w.bold for w in ln.words)


def _attach_markers(lines: list[Line]) -> list[Line]:
    """A lone list marker split off by the column-gap rule is re-joined to the text on its right
    (bullets usually sit a wide indent away from the item text)."""
    def lone_marker(ln: Line) -> bool:
        return len(ln.words) == 1 and is_marker(ln.words[0].text)

    pairs: dict[int, int] = {}      # marker line → text line
    taken: set[int] = set()
    for i, ln in enumerate(lines):
        if not lone_marker(ln):
            continue
        m = ln.words[0]
        cy = (m.top + m.bottom) / 2
        best = None
        for j, other in enumerate(lines):
            if j == i or j in taken or lone_marker(other):
                continue
            gap = other.x0 - m.x1
            near = 0 <= gap <= 4 * max(m.size, other.size)
            if near and other.top - 2 <= cy <= other.bottom + 2 and (
                best is None or other.x0 < lines[best].x0
            ):
                best = j
        if best is not None:
            pairs[i] = best
            taken.add(best)

    out: list[Line] = []
    for i, ln in enumerate(lines):
        if i in taken:
            continue
        out.append(Line([ln.words[0], *lines[pairs[i]].words]) if i in pairs else ln)
    out.sort(key=lambda ln: (round(ln.top, 1), ln.x0))
    return out


# ---------------------------------------------------------------- reading order

def _intervals(spans: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + min_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


_COL_GAP = 4.0


def _x_cuttable(items: list[tuple[BBox, T]]) -> bool:
    return len(_intervals([(b[0], b[2]) for b, _ in items], min_gap=_COL_GAP)) > 1


def xy_cut(items: list[tuple[BBox, T]], _depth: int = 0) -> list[tuple[BBox, T]]:
    """Recursive XY-cut: split at vertical gutters first (columns), then horizontal gaps.

    When a horizontal cut is needed (e.g. a full-width title above two columns), consecutive
    row bands that share a column gutter are kept together, so columns are still read one
    after the other instead of row by row across the gutter.
    """
    if len(items) <= 1 or _depth > 40:
        return items
    cols = _intervals([(b[0], b[2]) for b, _ in items], min_gap=_COL_GAP)
    if len(cols) > 1:
        parts = [[it for it in items if a - 0.01 <= it[0][0] and it[0][2] <= b + 0.01]
                 for a, b in cols]
        return [x for p in parts for x in xy_cut(p, _depth + 1)]
    rows = _intervals([(b[1], b[3]) for b, _ in items], min_gap=0.5)
    if len(rows) > 1:
        bands = [[it for it in items if a - 0.01 <= it[0][1] and it[0][3] <= b + 0.01]
                 for a, b in rows]
        groups: list[list[tuple[BBox, T]]] = [bands[0]]
        for band in bands[1:]:
            cur = groups[-1]
            if _x_cuttable(cur) and _x_cuttable(cur + band):
                cur.extend(band)
            else:
                groups.append(list(band))
        if len(groups) == 1 and not _x_cuttable(groups[0]):
            groups = bands
        return [x for g in groups for x in xy_cut(g, _depth + 1)]
    return sorted(items, key=lambda it: (it[0][1], it[0][0]))
