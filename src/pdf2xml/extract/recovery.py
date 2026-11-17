"""Conservative recovery of text regions and chapter evidence from native PDF geometry.

This stage never generates prose. It changes block boundaries/order, and every word still
comes from the independently extracted style layer. Bookmarks are evidence, not titles to
invent when their names are just production filenames.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pdf2xml.extract.types import (
    BBox,
    PageLayer,
    RawBlock,
    RawRegion,
    StructureLayer,
    StyleLayer,
    Word,
)
from pdf2xml.model.document import OutlineEntry


def deduplicate_overprint(style: StyleLayer) -> int:
    """Identical words drawn twice at exactly the same position are one printed word."""
    removed = 0
    for page in style.pages:
        seen = set()
        unique = []
        for word in page.words:
            key = (word.text, tuple(word.bbox), word.font, word.size, word.color, word.upright)
            if key in seen:
                removed += 1
            else:
                unique.append(word)
                seen.add(key)
        page.words = unique
    return removed


def read_outline(pdf: Path) -> list[OutlineEntry]:
    import pikepdf

    out: list[OutlineEntry] = []
    with pikepdf.open(pdf) as document:
        pages = {p.obj.objgen: i + 1 for i, p in enumerate(document.pages)}
        with document.open_outline() as outline:
            def walk(items: Sequence[Any], depth: int = 0) -> None:
                for item in items:
                    dest = item.destination
                    if dest is None and item.action is not None:
                        dest = item.action.get("/D")
                    page = None
                    if isinstance(dest, pikepdf.Array) and len(dest):
                        first = dest[0]
                        if isinstance(first, pikepdf.Dictionary):
                            page = pages.get(first.objgen)
                    title = str(item.title)
                    chapter = re.search(r"(?:\bchapter\s+|(?:^|[-_])ch(?:apter)?)(0*\d+)(?=$|[^A-Za-z0-9])",
                                        title, re.I)
                    kind: Literal["chapter", "section", "glossary", "index"] = (
                        "chapter" if chapter else "section")
                    if re.search(r"(?:^|[\W_])glossary(?=$|[\W_])|[-_]glos(?=$|[\W_])", title, re.I):
                        kind = "glossary"
                    elif re.search(r"(?:^|[\W_])index(?=$|[\W_])", title, re.I):
                        kind = "index"
                    if page is not None:
                        out.append(OutlineEntry(page=page, title=title, depth=depth,
                                                label=str(int(chapter[1])) if chapter else None,
                                                kind=kind))
                    walk(item.children, depth + 1)
            walk(outline.root)
    return out


def _inside(word: Word, box: BBox) -> bool:
    return box[0] - 1 <= (word.x0 + word.x1) / 2 <= box[2] + 1 and (
        box[1] - 1 <= (word.top + word.bottom) / 2 <= box[3] + 1)


def _bbox(words: list[Word]) -> BBox:
    return (min(w.x0 for w in words), min(w.top for w in words),
            max(w.x1 for w in words), max(w.bottom for w in words))


def _chapter_headers(
    blocks: list[RawBlock], page: PageLayer, outline: list[OutlineEntry], *,
    infer_chapters: bool = False,
) -> list[RawBlock]:
    anchors = [o for o in outline if o.page <= page.number <= o.page + 3 and o.kind == "chapter"]
    if anchors:
        anchors = [max(anchors, key=lambda e: e.page)]
        printed = [w for w in page.words if w.text.strip() == anchors[0].label
                   and w.size >= 18 and w.top < 0.35 * page.height]
        if printed:
            label = max(printed, key=lambda w: w.size)
            title = [w for w in page.words if label.bottom <= w.top <= label.bottom + 130
                     and w.size >= max(16, 0.45 * label.size) and w is not label]
            if title:
                title_box = _bbox(title)
                label_box = label.bbox
                keep = [b for b in blocks if not (b.kind in ("heading", "paragraph", "title")
                        and all(_inside(w, title_box) or _inside(w, label_box)
                                for w in page.words if _inside(w, b.bbox)))]
                for b in keep:
                    if b.kind == "figure" and all(_inside(w, b.bbox) for w in title):
                        b.role = "chapter-art"
                order = min((b.order for b in blocks), default=0)
                keep.extend([RawBlock(page.number, label_box, "heading", order, text=label.text,
                                      level=1, role="chapter-label", source_ref=f"native-chapter-label:{page.number}"),
                             RawBlock(page.number, title_box, "heading", order + 1,
                                      text=" ".join(w.text for w in title), level=1, role="chapter",
                                      source_ref=f"native-chapter-title:{page.number}")])
                authors = [w for w in page.words if title_box[3] <= w.top <= title_box[3] + 35
                           and 11 <= w.size < 16]
                if authors and not " ".join(w.text for w in authors).isupper():
                    keep.append(RawBlock(page.number, _bbox(authors), "paragraph", order + 2,
                                text=" ".join(w.text for w in authors),
                                source_ref=f"native-chapter-byline:{page.number}"))
                return keep
        if anchors[0].page != page.number:
            return blocks
    banners = [b for b in blocks if b.role == "chapter-art"]
    if infer_chapters and not anchors and banners:
        # Extracted chapter PDFs often lose their bookmarks. A printed chapter badge
        # supplies the same evidence without depending on a production filename.
        banner = min(banners, key=lambda b: b.bbox[1])
        words = [w for w in page.words if _inside(w, banner.bbox)]
        badge = next((w for w in words if w.text.strip().lower() == "chapter"), None)
        numbers = [w for w in words if re.fullmatch(r"[1-9]\d*", w.text.strip())]
        if badge is not None and numbers:
            badge_x, badge_y = badge.x0, badge.top
            number = min(numbers, key=lambda w: abs(w.x0 - badge_x) + abs(w.top - badge_y))
            entry = OutlineEntry(page=page.number, title=f"Chapter {number.text.strip()}",
                                 label=number.text.strip(), kind="chapter")
            outline.append(entry)
            anchors = [entry]
    if not anchors:
        return blocks
    if not banners:
        candidates = [b for b in blocks if b.kind == "heading" and not b.region_id
                      and b.bbox[1] < 0.3 * page.height
                      and not re.search(r"CHAPTER|LEARNING (?:OUTCOMES|OBJECTIVES)", b.text, re.I)]
        if candidates:
            def size(block: RawBlock) -> float:
                return max((w.size for w in page.words if _inside(w, block.bbox)), default=0)
            title_block = max(candidates, key=size)
            title_block.role, title_block.level = "chapter", 1
            for block in blocks:
                if re.fullmatch(r"\s*(?:CHAPTER\s*\d+|\d+\s*CHAPTER)\s*", block.text, re.I):
                    block.kind, block.role, block.level = "heading", "chapter-label", 1
        return blocks
    banner = min(banners, key=lambda b: b.bbox[1])
    words = [w for w in page.words if _inside(w, banner.bbox)]
    labels = [w for w in words if w.text.strip().lower() == "chapter"]
    numbers = [w for w in words if w.text.strip() == anchors[0].label]
    if labels and numbers:
        badge = labels[0]
        labels.append(min(numbers, key=lambda w: abs(w.x0 - badge.x0) +
                          abs(w.top - badge.top)))
    title = [w for w in words if w not in labels]
    if not title or not labels:
        return blocks
    # Recover label/title independently. Their printed positions differ between openers;
    # the source words are preserved, including formatting and the actual chapter number.
    keep = [b for b in blocks if b is banner or not (
        b.page == page.number and b.bbox[0] >= banner.bbox[0] - 2 and
        b.bbox[2] <= banner.bbox[2] + 2 and b.bbox[1] >= banner.bbox[1] - 2 and
        b.bbox[3] <= banner.bbox[3] + 2)]
    keep.extend([
        RawBlock(page.number, _bbox(labels), "heading", banner.order,
                 level=1, role="chapter-label", source_ref=banner.source_ref),
        RawBlock(page.number, _bbox(title), "heading", banner.order + 1,
                 level=1, role="chapter", source_ref=banner.source_ref),
    ])
    return keep


def _columns(blocks: list[RawBlock], page: PageLayer) -> list[tuple[float, float]]:
    width = page.frame[2] - page.frame[0]
    candidates = [b for b in blocks if b.kind in ("paragraph", "list_item")
                  and not b.region_id and len(b.text.split()) >= 3
                  and 0.18 * width <= b.bbox[2] - b.bbox[0] <= 0.6 * width]
    clusters: list[list[RawBlock]] = []
    for b in sorted(candidates, key=lambda x: (x.bbox[0] + x.bbox[2]) / 2):
        center = (b.bbox[0] + b.bbox[2]) / 2
        previous = ((clusters[-1][0].bbox[0] + clusters[-1][0].bbox[2]) / 2
                    if clusters else 0)
        if not clusters or center - previous > 0.18 * width:
            clusters.append([])
        clusters[-1].append(b)
    clusters = [c for c in clusters if len(c) >= 2]
    if len(clusters) != 2:
        return _word_gutter_columns(blocks, page)
    spans = [(statistics.median(b.bbox[0] for b in c),
              statistics.median(b.bbox[2] for b in c)) for c in clusters]
    return spans if spans[0][1] < spans[1][0] + 3 else []


def _word_gutter_columns(blocks: list[RawBlock], page: PageLayer) -> list[tuple[float, float]]:
    """Recover a merged paragraph only when repeated rows share a large empty gutter."""
    width = page.frame[2] - page.frame[0]
    for block in blocks:
        if (block.kind != "paragraph" or block.region_id or block.role
                or block.bbox[2] - block.bbox[0] < 0.6 * width):
            continue
        rows: list[list[Word]] = []
        for word in sorted((w for w in page.words if _inside(w, block.bbox)),
                           key=lambda w: (w.top, w.x0)):
            if not rows or abs(rows[-1][0].top - word.top) > 3:
                rows.append([])
            rows[-1].append(word)
        if len(rows) < 2:
            continue
        gaps = []
        for row in rows:
            ordered = sorted(row, key=lambda w: w.x0)
            pairs = [(a.x1, b.x0) for a, b in zip(ordered, ordered[1:])
                     if b.x0 - a.x1 >= 0.08 * width
                     and page.frame[0] + 0.3 * width < (a.x1 + b.x0) / 2 < page.frame[0] + 0.7 * width]
            if not pairs:
                break
            gaps.append(max(pairs, key=lambda p: p[1] - p[0]))
        if len(gaps) != len(rows):
            continue
        left, right = max(p[0] for p in gaps), min(p[1] for p in gaps)
        if right - left >= 0.08 * width:
            return [(block.bbox[0], left), (right, block.bbox[2])]
    return []


def _split_crossing(blocks: list[RawBlock], columns: list[tuple[float, float]],
                    page: PageLayer) -> list[RawBlock]:
    if not columns:
        return blocks
    gutter = (columns[0][1] + columns[1][0]) / 2
    out: list[RawBlock] = []
    for b in blocks:
        if b.kind not in ("paragraph", "list_item") or b.region_id or b.role or not (
                b.bbox[0] < gutter < b.bbox[2]):
            out.append(b)
            continue
        pieces = []
        for n, (left, right) in enumerate(columns):
            # Estimated spans are useful for deciding which column a block
            # occupies, never for cropping legitimate line-start words.
            lo = b.bbox[0] if n == 0 else max(b.bbox[0], gutter)
            hi = min(b.bbox[2], gutter) if n == 0 else b.bbox[2]
            # A sliver at the end of another column is bbox contamination, not a paragraph.
            if hi - lo < 0.65 * (right - left):
                continue
            box = (lo, b.bbox[1], hi, b.bbox[3])
            if sum(_inside(w, box) for w in page.words) >= 3:
                pieces.append(replace(b, bbox=box,
                                      source_ref=f"{b.source_ref or 'region'}:column{n + 1}"))
        # A single-column block whose bbox touches the inferred gutter is
        # already valid. Only crop contamination when its bbox extends
        # substantially into the other column.
        if len(pieces) == 1:
            piece = pieces[0]
            clipped = (piece.bbox[0] - b.bbox[0]) + (b.bbox[2] - piece.bbox[2])
            if clipped < 0.2 * (b.bbox[2] - b.bbox[0]):
                pieces = []
        out.extend(pieces or [b])
    return out


def _recover_index_lines(blocks: list[RawBlock], columns: list[tuple[float, float]],
                         page: PageLayer, outline: list[OutlineEntry]) -> list[RawBlock]:
    """An empty table prediction is not index structure: preserve native column line order."""
    anchors = [entry for entry in outline if entry.page <= page.number]
    if not anchors or max(anchors, key=lambda e: e.page).kind != "index" or len(columns) != 2:
        return blocks
    if not any(b.kind == "table" and not any(c.bbox for c in b.cells)
               and not any(c.text.strip() for c in b.cells) for b in blocks):
        return blocks
    from pdf2xml.merge.text import group_lines

    body = [b for b in blocks if b.kind in ("paragraph", "list_item", "table") and not b.region_id]
    retained = [b for b in blocks if b not in body]
    words = [w for w in page.words if any(_inside(w, b.bbox) for b in body)
             and not any(_inside(w, b.bbox) for b in retained)]
    gutter = (columns[0][1] + columns[1][0]) / 2
    for column in (0, 1):
        selected = [w for w in words if int((w.x0 + w.x1) / 2 >= gutter) == column]
        for number, line in enumerate(group_lines(selected)):
            retained.append(RawBlock(page.number, (line.x0, line.top, line.x1, line.bottom),
                "paragraph", min((b.order for b in body), default=0),
                text=" ".join(w.text for w in line.words),
                source_ref=f"index-native:{page.number}:{column}:{number}", role="index-line"))
    return retained


def _key_terms(blocks: list[RawBlock], page: PageLayer) -> list[RawBlock]:
    from pdf2xml.merge.text import group_lines

    heads = [b for b in blocks if b.kind == "heading" and
             re.fullmatch(r"\s*key\s+terms\s*", b.text, re.I)]
    if not heads:
        return blocks
    out = []
    for b in blocks:
        head = next((h for h in heads if b.bbox[1] >= h.bbox[3] - 2), None)
        if head is None or b.kind != "paragraph":
            out.append(b)
            continue
        words = [w for w in page.words if _inside(w, b.bbox)]
        lines = group_lines(words, split_gap=1.5)
        if not lines or any(len(ln.words) > 7 for ln in lines):
            out.append(b)
            continue
        for n, line in enumerate(sorted(lines, key=lambda ln: (round(ln.x0 / 20), ln.top))):
            out.append(replace(b, kind="list_item", bbox=_bbox(line.words),
                               group=f"key-terms:{page.number}", ordered=False,
                               role="key-term",
                               source_ref=f"{b.source_ref or 'key-terms'}:term{n + 1}"))
    return out


def _bullet_headings(blocks: list[RawBlock], page: PageLayer) -> None:
    """Bold bullet labels remain peers in the printed list, not body sections."""
    bullet = re.compile(r"^\s*[●•▪◦]\s+")
    heads = [b for b in blocks if b.kind == "heading" and bullet.match(b.text)]
    if not heads:
        return
    origins = {}
    for side in (0, 1):
        xs = [b.bbox[0] for b in heads if int(b.bbox[0] > page.width / 2) == side]
        if xs:
            origins[side] = min(xs)
    active = None
    for b in sorted(blocks, key=lambda b: b.order):
        if b.kind == "heading" and bullet.match(b.text):
            active = active or f"bullet-tree:{page.number}:{b.source_ref or b.order}"
            b.kind, b.ordered = "list_item", False
        elif b.kind in ("figure", "caption", "page_header", "page_footer"):
            continue
        elif b.kind != "list_item":
            active = None
        if active and b.kind == "list_item":
            side = int(b.bbox[0] > page.width / 2)
            origin = origins.get(side, b.bbox[0])
            b.group = active
            b.depth = max(0, round((b.bbox[0] - origin) / 12))


def _editorial_panels(blocks: list[RawBlock], page: PageLayer, layer: StructureLayer) -> None:
    named = re.compile(r"^(?:LEARNING (?:OUTCOMES|OBJECTIVES)|STOP AND CONSIDER\s*\d*|"
                       r"NOTABLE RESEARCH|BOX\s+\d+(?:\.\d+)*\b)", re.I)
    for head in blocks:
        if head.region_id or not named.match(head.text.strip()):
            continue
        head.kind = "heading"
        candidates = []
        for drawing in page.drawings:
            box = drawing.bbox
            if (box[2] - box[0] >= 100 and box[3] - box[1] >= 12
                    and box[0] - 12 <= head.bbox[0] <= box[2]
                    and box[2] >= head.bbox[2] - 6
                    and box[1] - 12 <= head.bbox[1] <= box[3]
                    and (box[2] - box[0]) * (box[3] - box[1]) < 0.45 * (
                        page.width * page.height)):
                candidates.append(box)
        members = []
        if candidates:
            box = min(candidates, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            # A blue title strip and a green body are separate PDF rectangles.
            # Grow only through touching, aligned drawing components, never through
            # nearby prose below the actual panel boundary.
            changed = True
            while changed:
                changed = False
                for drawing in page.drawings:
                    other = drawing.bbox
                    if (drawing.kind not in ("rect", "curve") or not (
                            drawing.fill or drawing.stroke) or other[3] - other[1] < 8):
                        continue
                    aligned = abs(other[0] - box[0]) <= 6 and abs(other[2] - box[2]) <= 6
                    touching = other[1] <= box[3] + 3 and other[3] >= box[1] - 3
                    union = (min(box[0], other[0]), min(box[1], other[1]),
                             max(box[2], other[2]), max(box[3], other[3]))
                    if aligned and touching and union != box and (
                            (union[2] - union[0]) * (union[3] - union[1])
                            < 0.45 * page.width * page.height):
                        box, changed = union, True
            # Open bordered panels may continue in the next column. Match the
            # border style and top/bottom band, not unrelated nearby prose.
            if head.text.upper().startswith("STOP AND CONSIDER"):
                border = next((d for d in page.drawings if d.bbox == min(
                    candidates, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))), None)
                if border is not None and border.stroke and not border.fill:
                    continuations = [d.bbox for d in page.drawings
                                     if d.kind == "curve" and d.stroke == border.stroke
                                     and not d.fill and d.bbox[0] >= box[2]
                                     and d.bbox[0] - box[2] < 0.12 * page.width
                                     and abs(d.bbox[1] - box[1]) < 15
                                     and abs(d.bbox[3] - box[3]) < 30
                                     and abs((d.bbox[2] - d.bbox[0]) -
                                             (box[2] - box[0])) < 15
                                     and not any(h is not head and named.match(h.text.strip())
                                                 and d.bbox[0] - 4 <= h.bbox[0] <= d.bbox[2]
                                                 and d.bbox[1] - 12 <= h.bbox[1] <= d.bbox[3]
                                                 for h in blocks)]
                    if continuations:
                        box = (box[0], min([box[1]] + [b[1] for b in continuations]),
                               max(b[2] for b in continuations),
                               max([box[3]] + [b[3] for b in continuations]))
            members = [b for b in blocks if not b.region_id and b.role != "chapter-art"
                       and b.bbox[0] >= box[0] - 4 and b.bbox[2] <= box[2] + 4
                       and b.bbox[1] >= box[1] - 12 and b.bbox[3] <= box[3] + 4]
        else:
            # Some shaded cards are raster backgrounds. Their native text remains usable.
            following = sorted([b for b in blocks if b is not head and not b.region_id
                                and b.kind in ("paragraph", "list_item")
                                and b.bbox[0] >= head.bbox[0] - 18
                                and b.bbox[0] < head.bbox[2]
                                and b.bbox[1] >= head.bbox[3] - 2],
                               key=lambda b: b.bbox[1])
            bottom = head.bbox[3]
            for b in following:
                if b.bbox[1] - bottom > 18:
                    break
                if any(h.kind == "heading" and h is not head and not h.region_id
                       and bottom < h.bbox[1] < b.bbox[1] and h.bbox[0] >= head.bbox[0] - 18
                       for h in blocks):
                    break
                members.append(b)
                bottom = max(bottom, b.bbox[3])
                if head.text.upper().startswith("STOP AND CONSIDER"):
                    break
            box = (head.bbox[0], head.bbox[1], max([head.bbox[2]] + [b.bbox[2] for b in members]),
                   max([head.bbox[3]] + [b.bbox[3] for b in members]))
        members = [head, *[b for b in members if b is not head]]
        if len(members) < 2:
            continue
        # A cut-out title intentionally crosses an open border's top edge.
        # The canonical panel envelope includes that source heading as well.
        box = (min(box[0], head.bbox[0]), min(box[1], head.bbox[1]),
               max(box[2], head.bbox[2]), max(box[3], head.bbox[3]))
        region = f"editorial:{page.number}:{head.source_ref or head.order}"
        role = "exercise" if "CONSIDER" in head.text.upper() else "sidebar"
        layer.regions.append(RawRegion(region, page.number, box, role))
        for b in members:
            b.region_id = region
        if head.text.upper().startswith("BOX ") and len(members) >= 4 and all(
                b.kind == "paragraph" and len(b.text.split()) < 15
                and not re.search(r"[.!?]$", b.text.strip()) for b in members[1:]):
            for b in members[1:]:
                b.kind, b.group, b.ordered = "list_item", region, False


def _order(blocks: list[RawBlock], columns: list[tuple[float, float]],
           page: PageLayer, layer: StructureLayer) -> list[RawBlock]:
    # Panels are atoms in reading order; their internal lines must remain together.
    regions = {r.id: r for r in layer.regions if r.page == page.number}
    atoms: dict[str, list[RawBlock]] = {}
    for i, b in enumerate(blocks):
        atoms.setdefault(b.region_id or (b.group if b.role == "key-term" else None)
                         or f"block:{i}", []).append(b)
    entries = []
    for key, bs in atoms.items():
        if bs[0].role == "key-term":
            bs.sort(key=lambda b: (round(b.bbox[0] / 20), b.bbox[1]))
        box = regions[key].bbox if key in regions else bs[0].bbox
        entries.append((box, bs))
    if not columns:
        # A failed column hypothesis is not evidence for row-major reading. Retain
        # Docling's order, which now includes separator-aware layout evidence.
        return [b for _, bs in sorted(entries, key=lambda e: min(b.order for b in e[1]))
                for b in bs]
    gutter = (columns[0][1] + columns[1][0]) / 2
    width = page.frame[2] - page.frame[0]
    for index, (box, bs) in enumerate(entries):
        if not any(b.role in ("module", "chapter") for b in bs):
            continue
        separators = [d.bbox for d in page.drawings
                      if d.bbox[0] < gutter < d.bbox[2]
                      and d.bbox[2] - d.bbox[0] > 0.75 * width
                      and d.bbox[3] - d.bbox[1] < 70
                      and abs(d.bbox[1] - box[1]) < 25]
        if separators:
            line = min(separators, key=lambda d: abs(d[1] - box[1]))
            entries[index] = ((min(box[0], line[0]), min(box[1], line[1]),
                               max(box[2], line[2]), max(box[3], line[3])), bs)
    spans = sorted([e for e in entries if e[0][0] < gutter < e[0][2]
                    and e[0][2] - e[0][0] > 0.75 * width], key=lambda e: e[0][1])
    rest = [e for e in entries if e not in spans]
    out = []
    def band(limit: float) -> None:
        selected = [e for e in rest if e[0][1] < limit]
        selected.sort(key=lambda e: (0 if (e[0][0] + e[0][2]) / 2 < gutter else 1,
                                     e[0][1], e[0][0]))
        for entry in selected:
            out.extend(entry[1])
            rest.remove(entry)
    for box, bs in spans:
        band(box[1])
        out.extend(bs)
    band(float("inf"))
    return out


_LIST_MARKER = re.compile(r"\(?((?:\d{1,3}(?:[–-]\d{1,3})?|[a-zA-Z]))[.)]$")


def _practice_lists(blocks: list[RawBlock], style: StyleLayer) -> list[RawBlock]:
    """Use source lines to recover flat question/option events across page breaks.

    Engine list parents/depth may be inconsistent across columns. Numeric questions
    and lettered options provide explicit evidence, so reconstruct their depth before
    the canonical list builder runs. Never generate question or option text.
    """
    from pdf2xml.merge.text import group_lines

    pages = {p.number: p for p in style.pages}
    group = None
    serial = 0
    out = []
    for block in blocks:
        text = block.text.strip()
        if block.kind == "heading":
            if re.fullmatch(r"PRACTICE QUESTIONS", text, re.I):
                serial += 1
                group = f"practice:{serial}"
            elif re.fullmatch(r"DISCUSSION(?: AND SHORT ANSWER)?|REFLECTION|KEY TERMS|"
                              r"BIBLIOGRAPHY|REFERENCES|SUMMARY", text, re.I) or (
                                  block.role == "chapter"):
                group = None
        if not group or block.region_id or block.kind not in ("paragraph", "list_item"):
            out.append(block)
            continue
        page = pages[block.page]
        lines = group_lines([w for w in page.words if _inside(w, block.bbox)], split_gap=1.5)
        if lines and all(re.fullmatch(r"_+", w.text) for line in lines for w in line.words):
            # A fill-in blank can be a separate engine block on the same line as
            # its numbered stem. Preserve it with that stem instead of ending a list.
            previous = out[-1] if out else None
            if (previous is not None and previous.kind == "list_item"
                    and previous.page == block.page and previous.group == group
                    and abs(previous.bbox[1] - block.bbox[1]) < 4):
                previous.bbox = (min(previous.bbox[0], block.bbox[0]), previous.bbox[1],
                                 max(previous.bbox[2], block.bbox[2]),
                                 max(previous.bbox[3], block.bbox[3]))
                continue
        chunks: list[tuple[str | None, list[Word]]] = []
        for line in lines:
            marker = line.words[0].text.strip()
            if _LIST_MARKER.fullmatch(marker):
                chunks.append((marker, list(line.words)))
            elif chunks:
                chunks[-1][1].extend(line.words)
            else:
                chunks.append((block.marker, list(line.words)))
        if not chunks:
            out.append(block)
            continue
        for number, (chunk_marker, words) in enumerate(chunks):
            if chunk_marker is None or not _LIST_MARKER.fullmatch(chunk_marker):
                out.append(replace(block, bbox=_bbox(words), marker=None, depth=0))
                continue
            depth = 0 if re.search(r"\d", chunk_marker) else 1
            out.append(replace(block, bbox=_bbox(words), kind="list_item", group=group,
                               marker=chunk_marker, ordered=True, depth=depth,
                               source_ref=f"{block.source_ref or 'practice'}:part{number + 1}"))
    return out


def recover(pdf: Path, style: StyleLayer, layer: StructureLayer) -> list[OutlineEntry]:
    """Repair source-supported regions before words are assigned to blocks."""
    outline = read_outline(pdf)
    infer_chapters = not any(entry.kind == "chapter" for entry in outline)
    by_page: dict[int, list[RawBlock]] = defaultdict(list)
    for b in layer.blocks:
        by_page[b.page].append(b)
    recovered = []
    for page in style.pages:
        blocks = _chapter_headers(by_page[page.number], page, outline,
                                  infer_chapters=infer_chapters)
        for block in blocks:
            if block.kind in ("paragraph", "list_item", "other") and re.match(
                    r"^\s*[\u25b2\u25b3\u25b6\u25ba]?\s*(?:figure|fig\.?|table)\s+\d+(?:\s*\.\s*\d+)*\s*[.:]",
                    block.text, re.I):
                block.kind = "caption"
                block.group, block.depth = None, 0
        _bullet_headings(blocks, page)
        for block in blocks:
            if (not block.role and not block.region_id and re.fullmatch(r"\s*\d+\s*", block.text)
                    and (block.bbox[0] > page.width * 0.9
                         or block.bbox[2] < page.width * 0.1)):
                block.kind, block.role = "other", "margin-label"
        _editorial_panels(blocks, page, layer)
        columns = _columns(blocks, page)
        blocks = _recover_index_lines(blocks, columns, page, outline)
        blocks = _split_crossing(blocks, columns, page)
        blocks = _key_terms(blocks, page)
        for block in blocks:
            text = block.text.strip()
            if not block.region_id and block.kind in ("paragraph", "heading"):
                if re.fullmatch(r"SUMMARY|APPLY|CASE STUDIES|PRACTICE QUESTIONS|REFLECTION|"
                                r"KEY TERMS|DISCUSSION AND SHORT ANSWER", text, re.I):
                    block.kind, block.level, block.role = "heading", 2, "section"
                elif re.fullmatch(r"CASE STUDY\s+\d+(?:\.\d+)*", text, re.I):
                    block.kind, block.level, block.role = "heading", 3, "section"
                elif re.fullmatch(r"GLOSSARY|INDEX", text, re.I):
                    block.kind, block.level, block.role = "heading", 1, "section"
        # A module banner is a structural header, not the first paragraph's content.
        modules = [b for b in blocks if re.fullmatch(r"\s*(?:MODULE\s*\d+|\d+\s*MODULE)\s*",
                                                    b.text, re.I)]
        for marker in modules:
            heads = [b for b in blocks if b.kind == "heading" and not b.region_id
                     and abs(b.bbox[1] - marker.bbox[1]) < 90 and b is not marker]
            if heads:
                head = min(heads, key=lambda b: abs(b.bbox[1] - marker.bbox[1]))
                head.role, head.level = "module", 2
                marker.kind, marker.level, marker.role = "heading", 2, "module-label"
        ordered = _order(blocks, columns, page, layer)
        # Label and title are adjacent even when the badge is to the right of the title.
        for role in ("chapter", "module"):
            for head in [b for b in ordered if b.role == role]:
                label = next((b for b in ordered if b.role == role + "-label"), None)
                if label is not None:
                    ordered.remove(label)
                    ordered.insert(ordered.index(head), label)
        recovered.extend(ordered)
    recovered = _practice_lists(recovered, style)
    for i, block in enumerate(recovered):
        block.order = i
    layer.blocks = recovered
    return outline
