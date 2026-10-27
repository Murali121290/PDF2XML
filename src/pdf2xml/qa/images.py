"""Image check: is every picture in the PDF a figure in the output?

The structure engine decides what is a figure; an image it did not mark is silently left out.
Each image the style layer saw is matched to a canonical figure by overlap. Unmatched images
are classified: tiny ones (bullets, rules, icons) and ones in the header/footer area are
reported for information; the rest are missing. The figure count is then followed into the
asset crops and the ``<graphic>`` elements of each export.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from pdf2xml.extract.types import BBox, StyleLayer
from pdf2xml.model import Document, Figure, Table, iter_blocks

_TINY_PT = 12.0          # an image thinner than this is a rule, a bullet or an icon
_TINY_AREA = 300.0       # pt²
_MATCH = 0.5             # share of the image's area inside a figure


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _inside(b: BBox, box: BBox) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _clip(b: BBox, frame: BBox) -> BBox:
    return (max(b[0], frame[0]), max(b[1], frame[1]), min(b[2], frame[2]), min(b[3], frame[3]))


def check_images(style: StyleLayer, doc: Document,
                 graphics: Mapping[str, set[str]] | None = None,
                 marks_dropped: Mapping[str, Counter[int]] | None = None) -> dict[str, Any]:
    """``graphics``: the image files each export links (``<graphic xlink:href>``);
    ``marks_dropped``: per export, the pages of small figures it left out on purpose."""
    figures = [b for b in iter_blocks(doc.body) if isinstance(b, Figure)]
    tables = [b for b in iter_blocks(doc.body) if isinstance(b, Table)]
    margins = [a for a in doc.artifacts if a.kind in ("header", "footer", "page_number")]
    matched: set[str] = set()
    missed: list[dict[str, Any]] = []
    minor: list[dict[str, Any]] = []
    backgrounds: set[int] = set()
    n_images = 0
    for page in style.pages:
        frame = page.frame
        frame_area = _area(frame) or 1.0
        seen: set[tuple[int, ...]] = set()
        for img in page.images:
            box = _clip(img.bbox, frame)
            area = _area(box)
            if area <= 0:
                continue                         # outside the trimmed page (slug, marks)
            key = tuple(round(v) for v in box)
            if key in seen:
                continue                         # the same picture drawn twice in one place
            seen.add(key)
            n_images += 1
            entry: dict[str, Any] = {"page": page.number, "bbox": list(box),
                                     "size_pt": [round(box[2] - box[0], 1),
                                                 round(box[3] - box[1], 1)]}
            fig = max((f for f in figures if f.page == page.number),
                      key=lambda f: _overlap(box, f.bbox), default=None)
            if fig is not None and _overlap(box, fig.bbox) >= _MATCH * area:
                matched.add(fig.id)
                continue
            w, h = box[2] - box[0], box[3] - box[1]
            under = sum(1 for wd in page.words if _inside(wd.bbox, box))
            if min(w, h) < _TINY_PT or area < _TINY_AREA:
                minor.append({**entry, "why": "tiny (bullet, rule or icon)"})
            elif area >= 0.5 * frame_area and under and under >= 0.5 * len(page.words) and (
                    under >= 20 or area >= 0.8 * frame_area):
                # a page scan with a text layer, or a cover or background with the text set on
                # it: the text is captured, the picture is the page's ground
                backgrounds.add(page.number)
                minor.append({**entry, "why": "under the page's text (a scanned page with a "
                                              "text layer, a cover or a background)"})
            elif any(a.page == page.number and _overlap(box, a.bbox) >= _MATCH * area
                     for a in margins):
                minor.append({**entry, "why": "in a running head or foot"})
            else:
                if any(t.page == page.number and _overlap(box, t.bbox) >= _MATCH * area
                       for t in tables):
                    entry["note"] = "inside a table: tables are written as text only"
                if area >= 0.8 * frame_area:
                    entry["note"] = "full page (a cover, plate or background)"
                if under:
                    entry["words_over_it"] = under
                missed.append(entry)

    no_crop = [{"page": f.page, "id": f.id} for f in figures if not f.image]
    exports: dict[str, Any] = {}
    problems: list[str] = []
    cropped = len(figures) - len(no_crop)
    for target, hrefs in sorted((graphics or {}).items()):
        marks = Counter((marks_dropped or {}).get(target, Counter()))
        small = 0
        lost: list[dict[str, Any]] = []
        # the smallest figures of a page are the marks the export left out
        for f in sorted((f for f in figures if f.image and f.image not in hrefs),
                        key=lambda f: (f.page, _area(f.bbox))):
            if marks[f.page] > 0:
                marks[f.page] -= 1
                small += 1
                continue
            lost.append({"page": f.page, "id": f.id, "image": f.image,
                         "size_pt": [round(f.bbox[2] - f.bbox[0], 1),
                                     round(f.bbox[3] - f.bbox[1], 1)]})
        exports[target] = {"graphics": len(hrefs), "small_figures_left_out": small,
                           "figures_not_written": lost}
        if lost:
            problems.append(f"{target}: {len(lost)} figure(s) not written "
                            f"(pages {', '.join(str(x['page']) for x in lost[:8])}"
                            f"{' …' if len(lost) > 8 else ''})")
    status = "warn" if missed or no_crop or problems else ("info" if minor else "ok")
    summary = (f"{n_images} image(s) in the PDF: {n_images - len(missed) - len(minor)} in "
               f"figures, {len(minor)} tiny or decorative, {len(missed)} not in any figure; "
               f"{len(figures)} figure(s), {len(figures) - len(matched)} drawn as vectors")
    if backgrounds:
        summary += (f"; {len(backgrounds)} page(s) have a full-page image under the text: "
                    "a scan with a text layer (the text comes from that layer, which may be "
                    "OCR) or a background")
    if no_crop:
        summary += f"; {len(no_crop)} figure(s) without an image file"
    if problems:
        summary += "; " + "; ".join(problems)
    return {
        "status": status, "summary": summary,
        "pdf_images": n_images, "figures": len(figures), "figures_with_image_object":
            len(matched), "figures_cropped": cropped,
        "missed": missed, "minor": minor[:50], "background_pages": sorted(backgrounds),
        "figures_without_file": no_crop,
        "exports": exports,
    }
