"""Uncovered-ink check: is anything printed on the page that the pipeline did not capture?

Each page is rendered (PDFium), and everything the pipeline knows about is masked: the style
layer's words, rules, boxes and images, and the canonical figures, tables and formulas (they
are exported as crops or as structure). Ink left over is content nobody read: text converted
to outlines, a chart drawn in vectors that no engine marked as a figure, a stamp or a
signature. No OCR: this only says *where*, the QA overlay shows *what*.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pypdfium2 as pdfium
from PIL import ImageDraw

from pdf2xml.export.assets import page_origin
from pdf2xml.extract.types import BBox, StyleLayer
from pdf2xml.model import Document, Figure, Formula, Table, iter_blocks

_SCALE = 1.0          # 72 dpi is enough to see a glyph
_CELL = 3             # px: ink is grouped in cells of 3 × 3 pt
_CELL_INK = 0.06      # share of a cell's pixels that must be ink
_CONTRAST = 60        # grey levels from the page background that count as ink
_PAD = 1.5            # pt around masked boxes (anti-aliasing, italic overhang)
_MIN_REGION = 20.0    # pt²: smaller specks are ignored
_WARN_REGION = 300.0  # pt²: a region this large is something real


def check_ink(style: StyleLayer, doc: Document, pdf: pdfium.PdfDocument) -> dict[str, Any]:
    known: dict[int, list[BBox]] = {}
    for b in iter_blocks(doc.body):
        if isinstance(b, Figure | Table | Formula):
            known.setdefault(b.page, []).append(b.bbox)
    for a in doc.artifacts:
        known.setdefault(a.page, []).append(a.bbox)
    found: dict[int, list[dict[str, Any]]] = {}
    for layer in style.pages:
        boxes = [*known.get(layer.number, []), *(w.bbox for w in layer.words),
                 *(d.bbox for d in layer.drawings), *(i.bbox for i in layer.images)]
        page = pdf[layer.number - 1]
        try:
            regions = _uncovered(page, boxes, layer.frame)
        finally:
            page.close()
        if regions:
            found[layer.number] = regions
    # a page decoration (a thumb tab, an ornament) sits at the same height with the same size
    # on many pages, on the left or the right: like a running head, it is not content
    repeats: dict[tuple[int, ...], set[int]] = {}
    for n, regions in found.items():
        for r in regions:
            repeats.setdefault(_shape(r["bbox"]), set()).add(n)
    pages: list[dict[str, Any]] = []
    total = 0.0
    decorated: set[int] = set()
    for n, regions in sorted(found.items()):
        for r in regions:
            if len(repeats[_shape(r["bbox"])]) >= 3:
                r["repeats_on_pages"] = len(repeats[_shape(r["bbox"])])
                decorated.add(n)
        area = sum(r["area_pt2"] for r in regions)
        total += area
        pages.append({"page": n, "uncovered_pt2": round(area, 1),
                      "regions": sorted(regions, key=lambda r: -r["area_pt2"])[:20]})
    big = [p for p in pages if any(r["area_pt2"] >= _WARN_REGION and r["kind"] == "content"
                                   and "repeats_on_pages" not in r for r in p["regions"])]
    status = "warn" if big else ("info" if pages else "ok")
    summary = (f"{len(pages)} page(s) with ink outside every captured word, drawing, image "
               f"and figure ({round(total)} pt²)" if pages
               else "all ink on the pages is captured")
    if decorated:
        summary += f"; on {len(decorated)} page(s) a repeated page decoration"
    if big:
        summary += (f"; {len(big)} page(s) with uncaptured content, e.g. text set as outlines "
                    f"or a drawing (pages {', '.join(str(p['page']) for p in big[:10])}"
                    f"{' …' if len(big) > 10 else ''})")
    return {"status": status, "summary": summary, "pages": pages}


def _shape(b: list[float]) -> tuple[int, ...]:
    """Height on the page and size, to 6 pt; the horizontal position is left out (facing
    pages mirror it)."""
    return (round(b[1] / 6), round((b[2] - b[0]) / 6), round((b[3] - b[1]) / 6))


def _uncovered(page: pdfium.PdfPage, boxes: Iterable[BBox],
               frame: BBox) -> list[dict[str, Any]]:
    ox, oy = page_origin(page)
    img = page.render(scale=_SCALE, grayscale=True).to_pil().convert("L")
    hist = img.histogram()
    bg = max(range(256), key=lambda v: hist[v])               # the page colour
    ink = img.point([255 if abs(v - bg) > _CONTRAST else 0 for v in range(256)])
    draw = ImageDraw.Draw(ink)
    w, h = ink.size

    def px(b: BBox, pad: float = 0.0) -> tuple[float, float, float, float]:
        return ((b[0] - ox - pad) * _SCALE, (b[1] - oy - pad) * _SCALE,
                (b[2] - ox + pad) * _SCALE, (b[3] - oy + pad) * _SCALE)

    for b in boxes:
        if b[2] >= b[0] and b[3] >= b[1]:
            draw.rectangle(px(b, _PAD), fill=0)
    # only the finished page: crop marks and slug lines outside the trim box do not count
    fx0, fy0, fx1, fy1 = px(frame)
    for m in ((0, 0, w, fy0), (0, fy1, w, h), (0, 0, fx0, h), (fx1, 0, w, h)):
        if m[2] > m[0] and m[3] > m[1]:
            draw.rectangle(m, fill=0)
    cells = ink.reduce(_CELL) if _CELL > 1 else ink
    cw, ch = cells.size
    data = cells.tobytes()
    limit = 255 * _CELL_INK
    on = {(x, y) for y in range(ch) for x in range(cw) if data[y * cw + x] > limit}
    out = []
    for group in _groups(on):
        region = _region(group, ox, oy)
        if region["area_pt2"] >= _MIN_REGION:
            region["kind"] = _kind(data, cw, group)
            out.append(region)
    return out


def _kind(data: bytes, cw: int, group: list[tuple[int, int]]) -> str:
    """What the leftover ink looks like, from the region's own cells. Box borders, banners
    and rules are often drawn as paths the style layer does not keep; text set as outlines
    or a chart is detail in between: "content"."""
    xs = [x for x, _ in group]
    ys = [y for _, y in group]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    if min(bw, bh) <= 2:
        return "rule"
    ink = sum(data[y * cw + x] for x, y in group) / 255
    cells = set(group)
    # a filled rectangle (a banner behind white text) covers the sides of its box, all but
    # one where the text reaches its edge; text covers two at most (its first and last
    # letter): x-height letters leave the top row, descenders and gaps the bottom one
    sides = [sum((x, y0) in cells for x in range(x0, x1 + 1)) / bw,
             sum((x, y1) in cells for x in range(x0, x1 + 1)) / bw,
             sum((x0, y) in cells for y in range(y0, y1 + 1)) / bh,
             sum((x1, y) in cells for y in range(y0, y1 + 1)) / bh]
    if ink >= 0.85 * bw * bh or sum(s >= 0.9 for s in sides) >= 3:
        return "filled shape"
    edge = sum(1 for x, y in group if x - x0 < 2 or x1 - x < 2 or y - y0 < 2 or y1 - y < 2)
    if bw >= 6 and bh >= 6 and edge >= 0.85 * len(group):
        return "frame"
    return "content"


def _groups(on: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Connected groups of ink cells (8-neighbourhood)."""
    out: list[list[tuple[int, int]]] = []
    left = set(on)
    while left:
        start = left.pop()
        group, stack = [start], [start]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in left:
                        left.remove(n)
                        group.append(n)
                        stack.append(n)
        out.append(group)
    return out


def _region(group: list[tuple[int, int]], ox: float, oy: float) -> dict[str, Any]:
    size = _CELL / _SCALE
    xs = [x for x, _ in group]
    ys = [y for _, y in group]
    bbox = [round(min(xs) * size + ox, 1), round(min(ys) * size + oy, 1),
            round((max(xs) + 1) * size + ox, 1), round((max(ys) + 1) * size + oy, 1)]
    return {"bbox": bbox, "area_pt2": round(len(group) * size * size, 1)}
