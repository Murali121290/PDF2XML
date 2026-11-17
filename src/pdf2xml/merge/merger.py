"""Merge the style layer (words) into the structure layer (blocks) by bounding-box overlap.

Rule (CLAUDE.md §2.1): a word belongs to the block that covers the largest share of its area,
if that share is ≥ ``threshold``. Otherwise it is an orphan: attached to a nearby text block, or
grouped into new paragraphs inserted into the reading order. Every word ends up somewhere.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdf2xml.extract.types import BBox, RawBlock, RawCell, StructureLayer, StyleLayer, Word
from pdf2xml.merge.text import ARun, Line, build_runs, group_lines, fix_ligatures

_TEXT_KINDS = {"title", "heading", "paragraph", "list_item", "caption", "footnote", "code",
               "reference", "other", "formula"}


@dataclass
class MBlock:
    raw: RawBlock
    words: list[Word] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    runs: list[ARun] = field(default_factory=list)
    cells: list[tuple[RawCell, list[ARun]]] = field(default_factory=list)
    artifact: str | None = None      # set when the block is an artifact (header/footer/…)
    figure_text: str = ""            # words found inside a figure (labels, chart text)

    @property
    def kind(self) -> str:
        return self.raw.kind

    @property
    def page(self) -> int:
        return self.raw.page

    @property
    def bbox(self) -> BBox:
        return self.raw.bbox


@dataclass
class MergeResult:
    blocks: list[MBlock]
    warnings: list[str]
    dehyphenated: int
    orphans: int
    dehyphenated_by_page: dict[int, int] = field(default_factory=dict)


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: BBox, b: BBox) -> float:
    x = min(a[2], b[2]) - max(a[0], b[0])
    y = min(a[3], b[3]) - max(a[1], b[1])
    return x * y if x > 0 and y > 0 else 0.0


def _share(w: BBox, b: BBox) -> float:
    a = _area(w)
    if a <= 1e-6:  # degenerate word box: use its centre
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        return 1.0 if b[0] <= cx <= b[2] and b[1] <= cy <= b[3] else 0.0
    return _overlap(w, b) / a


def _distance(w: BBox, b: BBox) -> float:
    dx = max(b[0] - w[2], 0.0, w[0] - b[2])
    dy = max(b[1] - w[3], 0.0, w[1] - b[3])
    return float((dx * dx + dy * dy) ** 0.5)


def merge(
    style: StyleLayer,
    structure: StructureLayer,
    *,
    threshold: float = 0.5,
    dehyphenate: bool = True,
) -> MergeResult:
    blocks = [MBlock(raw=b) for b in sorted(structure.blocks, key=lambda b: b.order)]
    by_page: dict[int, list[MBlock]] = defaultdict(list)
    for mb in blocks:
        by_page[mb.page].append(mb)

    warnings: list[str] = []
    orphan_total = 0
    inserted: dict[int, list[MBlock]] = defaultdict(list)  # anchor index → new blocks after it

    for page in style.pages:
        cands = by_page.get(page.number, [])
        orphans: list[Word] = []
        for w in page.words:
            best, best_share = None, 0.0
            for mb in cands:
                s = _share(w.bbox, mb.bbox)
                if s > best_share or (s == best_share and s > 0 and best is not None and (
                        _area(mb.bbox) < _area(best.bbox) or (
                            _area(mb.bbox) == _area(best.bbox) and best.kind == "figure"
                            and mb.kind in _TEXT_KINDS))):
                    best, best_share = mb, s
            if best is not None and best_share >= threshold:
                best.words.append(w)
            else:
                orphans.append(w)

        # Orphans next to a text block join it; the rest become new paragraphs.
        leftover: list[Word] = []
        for w in orphans:
            near = min(
                (mb for mb in cands if mb.kind in _TEXT_KINDS),
                key=lambda mb: _distance(w.bbox, mb.bbox),
                default=None,
            )
            if near is not None and _distance(w.bbox, near.bbox) <= 0.6 * w.size:
                near.words.append(w)
            else:
                # A single glyph on a float's edge can fall outside the model
                # bbox (notably italic index suffixes and punctuation). Preserve
                # it with the source figure/table, not as invented body prose.
                image = min((mb for mb in cands if (mb.kind == "figure" or
                             mb.kind == "table" and any(c.bbox for c in mb.raw.cells))
                             and len(w.text) == 1 and mb.bbox[1] <= w.top
                             and w.bottom <= mb.bbox[3]),
                            key=lambda mb: _distance(w.bbox, mb.bbox), default=None)
                if image is not None and _distance(w.bbox, image.bbox) <= 0.6 * w.size:
                    image.words.append(w)
                    x0, y0, x1, y1 = image.bbox
                    image.raw.bbox = (min(x0, w.x0), min(y0, w.top),
                                      max(x1, w.x1), max(y1, w.bottom))
                else:
                    leftover.append(w)
        if leftover:
            new_blocks = _orphan_blocks(leftover, page.number)
            # slug lines and marks outside a printer's trim box are expected to be loose (they
            # become artifacts); only words on the finished page are worth a warning
            fx0, fy0, fx1, fy1 = page.frame
            inside = [w for w in leftover
                      if fx0 <= (w.x0 + w.x1) / 2 <= fx1 and fy0 <= (w.top + w.bottom) / 2 <= fy1]
            orphan_total += len(inside)
            if inside:
                warnings.append(
                    f"page {page.number}: {len(inside)} word(s) outside all detected blocks were "
                    f"grouped into {len(new_blocks)} new paragraph(s)"
                )
            for nb in new_blocks:
                anchor = _anchor_index(blocks, nb)
                inserted[anchor].append(nb)

    ordered: list[MBlock] = inserted.get(-1, [])
    for i, mb in enumerate(blocks):
        ordered.append(mb)
        ordered.extend(inserted.get(i, []))

    joins = 0
    by_page_joins: dict[int, int] = defaultdict(int)
    for mb in ordered:
        removed = _build(mb, dehyphenate, warnings)
        joins += removed
        by_page_joins[mb.page] += removed
    return MergeResult(ordered, warnings, joins, orphan_total, dict(by_page_joins))


def _orphan_blocks(words: Sequence[Word], page: int) -> list[MBlock]:
    """Group loose words into paragraphs: lines that are vertically close and overlap in x."""
    out: list[MBlock] = []
    for ln in group_lines(words, split_gap=1.5):
        box = (ln.x0, ln.top, ln.x1, ln.bottom)
        prev = out[-1] if out else None
        if prev is not None:
            pb = prev.raw.bbox
            if 0 <= ln.top - pb[3] <= 0.6 * ln.size and ln.x0 < pb[2] and ln.x1 > pb[0]:
                prev.raw.bbox = (min(pb[0], box[0]), pb[1], max(pb[2], box[2]), box[3])
                prev.words.extend(ln.words)
                continue
        out.append(MBlock(raw=RawBlock(page=page, bbox=box, kind="paragraph", order=-1),
                          words=list(ln.words)))
    return out


def _anchor_index(blocks: Sequence[MBlock], nb: MBlock) -> int:
    """Index of the block after which ``nb`` should be read: the last block on the same page
    that sits above it (and overlaps it horizontally), else the last block of the previous page."""
    top = nb.bbox[1]
    anchor = -1
    for i, mb in enumerate(blocks):
        if mb.page < nb.page:
            anchor = i
        elif mb.page == nb.page:
            above = mb.bbox[3] <= top + 1
            overlaps = mb.bbox[0] < nb.bbox[2] and mb.bbox[2] > nb.bbox[0]
            if above and overlaps:
                anchor = i
    return anchor


def _build(mb: MBlock, dehyphenate: bool, warnings: list[str]) -> int:
    if mb.kind == "figure":
        mb.figure_text = " ".join(w.text for ln in group_lines(mb.words) for w in ln.words)
        mb.words.sort(key=lambda w: (w.top, w.x0))
        return 0
    if mb.kind == "table":
        return _build_table(mb, dehyphenate, warnings)
    mb.lines = group_lines(mb.words)
    res = build_runs(mb.lines, dehyphenate=dehyphenate, preserve_lines=mb.kind == "code")
    mb.runs = res.runs
    return res.dehyphenated


def _build_table(mb: MBlock, dehyphenate: bool, warnings: list[str]) -> int:
    cells = mb.raw.cells
    def characters(text: str) -> Counter[str]:
        return Counter(c for c in fix_ligatures(text) if not c.isspace())

    no_boxes = not any(c.bbox is not None for c in cells)
    incomplete_transcription = (bool(mb.words) and no_boxes and
        characters(" ".join(w.text for w in mb.words)) != characters(" ".join(c.text for c in cells)))
    if not cells or incomplete_transcription:
        # No cell structure: keep the words as one paragraph-like cell so nothing is lost.
        mb.lines = group_lines(mb.words)
        res = build_runs(mb.lines, dehyphenate=dehyphenate)
        mb.cells = [(RawCell(row=0, col=0), res.runs)]
        mb.raw.n_rows, mb.raw.n_cols = 1, 1
        warnings.append(f"page {mb.page}: table without reliable cell geometry/transcription "
                        "preserved from native PDF words as a single cell; review its structure")
        return res.dehyphenated
    has_boxes = any(c.bbox is not None for c in cells)
    per_cell: dict[int, list[Word]] = defaultdict(list)
    if has_boxes:
        for w in mb.words:
            best_i, best_s = -1, 0.0
            for i, c in enumerate(cells):
                if c.bbox is None:
                    continue
                s = _share(w.bbox, c.bbox)
                if s > best_s:
                    best_i, best_s = i, s
            if best_i < 0:
                best_i = min(
                    (i for i, c in enumerate(cells) if c.bbox is not None),
                    key=lambda i: _distance(w.bbox, cells[i].bbox or w.bbox),
                )
            per_cell[best_i].append(w)
    joins = 0
    for i, c in enumerate(cells):
        if has_boxes:
            lines = group_lines(per_cell.get(i, []))
            res = build_runs(lines, dehyphenate=dehyphenate)
            joins += res.dehyphenated
            mb.cells.append((c, res.runs))
        else:
            mb.cells.append((c, [ARun(c.text, "", 0.0, False, False, "#000000", False)]))
    if not has_boxes:
        warnings.append(f"page {mb.page}: table cells have no boxes; cell text taken from the "
                        f"structure engine without styles")
    return joins
