"""Assemble the canonical Document from merged blocks + style catalogue."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, cast

from pdf2xml.extract.types import PageLayer, RawRegion
from pdf2xml.merge.boxes import group_boxes
from pdf2xml.merge.lists import nest_list
from pdf2xml.merge.merger import MBlock
from pdf2xml.merge.text import MARKER_RE, is_ordered_marker, runs_text
from pdf2xml.model import (
    Artifact,
    Block,
    Box,
    Caption,
    Code,
    Document,
    Figure,
    Formula,
    Heading,
    ListBlock,
    ListItem,
    Meta,
    PageInfo,
    Paragraph,
    Run,
    Table,
    TableCell,
    iter_blocks,
)
from pdf2xml.model.document import BoxRole
from pdf2xml.styles.catalogue import Catalogue, to_runs
from pdf2xml.styles.geometry import para_props

_LEADING_MARKER = re.compile(r"^\s*(\S+)(?:\s+|$)")

ArtifactKind = Literal["header", "footer", "page_number", "figure_text", "other"]
_ARTIFACT_KINDS: dict[str, ArtifactKind] = {
    "header": "header", "footer": "footer", "page_number": "page_number",
}


def build_document(
    blocks: Sequence[MBlock],
    cat: Catalogue,
    meta: Meta,
    pages: list[PageInfo],
    layers: Sequence[PageLayer] = (),
    regions: Sequence[RawRegion] = (),
) -> Document:
    """``layers`` (the style layer's pages) supply the drawings that frame boxes."""
    doc = Document(meta=meta, styles=cat.styles, pages=pages)
    columns = _columns(blocks)

    prev_on_page: dict[int, MBlock] = {}
    pending_list: ListBlock | None = None
    pending_group: str | None = None
    list_stack: list[tuple[int, ListBlock]] = []

    for mb in blocks:
        if mb.artifact is not None:
            text = _artifact_text(mb)
            if text:
                kind = _ARTIFACT_KINDS.get(mb.artifact, "other")
                doc.artifacts.append(Artifact(kind=kind, page=mb.page, bbox=mb.bbox, text=text))
            continue
        if mb.kind == "figure" and mb.figure_text:
            doc.artifacts.append(Artifact(kind="figure_text", page=mb.page, bbox=mb.bbox,
                                          text=mb.figure_text))

        block = _convert(mb, cat, columns)
        if block is None:
            continue
        block.source_ref = mb.raw.source_ref
        block.region_id = mb.raw.region_id
        _space_before(block, mb, prev_on_page.get(mb.page))
        prev_on_page[mb.page] = mb

        if isinstance(block, ListItem):
            group = mb.raw.group or "__adjacent__"
            if pending_list is None or group != pending_group or (
                    pending_list.region_id != block.region_id):
                pending_list = ListBlock(page=mb.page, bbox=mb.bbox,
                                         ordered=bool(mb.raw.ordered), items=[],
                                         region_id=block.region_id)
                pending_group = group
                list_stack = [(0, pending_list)]
                doc.body.append(pending_list)
            depth = mb.raw.depth
            while len(list_stack) > 1 and list_stack[-1][0] > depth:
                list_stack.pop()
            parent_depth, parent = list_stack[-1]
            if depth > parent_depth and parent.items:
                sub = ListBlock(page=mb.page, bbox=mb.bbox, ordered=bool(mb.raw.ordered),
                                items=[])
                parent.items[-1].children.append(sub)
                list_stack.append((depth, sub))
                parent = sub
            parent.items.append(block)
            parent.bbox = _union(parent.bbox, block.bbox)
            continue

        # footnotes and references leave the body, so a list continues past them (a list
        # running over a page break has the page's footnotes between its items)
        if mb.kind == "footnote":
            doc.footnotes.append(block)
            continue
        if mb.kind == "reference":
            doc.references.append(block)
            continue
        pending_list, pending_group, list_stack = None, None, []
        doc.body.append(block)

    doc.body = [nest_list(b) if isinstance(b, ListBlock) else b for b in doc.body]
    for region in regions:
        children = [b for b in doc.body if b.region_id == region.id and b.page == region.page]
        if not children:
            continue
        first = min(doc.body.index(b) for b in children)
        doc.body = [b for b in doc.body if b not in children]
        box = Box(page=region.page, bbox=region.bbox, role=cast(BoxRole, region.role),
                  children=children,
                  source_ref=region.id, region_id=region.id)
        doc.body.insert(first, box)
    if layers:
        doc.body = _missed_pictures(doc.body, doc.artifacts, layers)
        doc.body = group_boxes(doc.body, layers)
    _assign_ids(doc)
    _link_captions(doc.body)
    _classify_decorations(doc)
    doc.meta.title = doc.meta.title or _title(doc.body)
    return doc


_PIC_MIN_SIDE = 12.0      # pt: thinner is a rule or a bullet
_PIC_MIN_AREA = 300.0     # pt²


def _area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _missed_pictures(body: list[Block], artifacts: Sequence[Artifact],
                     layers: Sequence[PageLayer]) -> list[Block]:
    """No picture is ever dropped: an image the style layer saw that no figure or table
    holds (the layout engine did not mark it: a cover, a plate, a photo) becomes a figure,
    placed in reading order by its top. Not: tiny images (bullets, rules), images in a
    running head or foot, and page backgrounds or scans with the page's text set over them
    (the text is the content)."""
    by_page: dict[int, list[Block]] = {}
    for b in body:
        by_page.setdefault(b.page, []).append(b)
    added: list[Figure] = []
    for layer in layers:
        frame = layer.frame
        blocks = by_page.get(layer.number, [])
        holders = [b.bbox for b in iter_blocks(blocks) if isinstance(b, Figure | Table | Box)]
        holders += [f.bbox for f in added if f.page == layer.number]
        holders += [a.bbox for a in artifacts if a.page == layer.number]
        texts = [b for b in blocks if not isinstance(b, Figure | Table)]
        for img in layer.images:
            box = (max(img.bbox[0], frame[0]), max(img.bbox[1], frame[1]),
                   min(img.bbox[2], frame[2]), min(img.bbox[3], frame[3]))
            area = _area(box)
            if (min(box[2] - box[0], box[3] - box[1]) < _PIC_MIN_SIDE or area < _PIC_MIN_AREA
                    or any(_overlap(box, h) >= 0.5 * area for h in holders)):
                continue
            over = [t for t in texts if box[0] <= (t.bbox[0] + t.bbox[2]) / 2 <= box[2]
                    and box[1] <= (t.bbox[1] + t.bbox[3]) / 2 <= box[3]]
            if over and len(over) >= 0.5 * len(texts) and area >= 0.5 * _area(frame):
                continue                        # the page's ground: the text is set on it
            fig = Figure(page=layer.number, bbox=box)
            added.append(fig)
            holders.append(box)
    if not added:
        return body
    out = list(body)
    for fig in sorted(added, key=lambda f: (f.page, f.bbox[1], f.bbox[0])):
        # before the first block of its page that starts below it; else after its page's last
        # block; else after the last block of an earlier page
        at = next((i for i, b in enumerate(out) if b.page == fig.page
                   and b.bbox[1] >= fig.bbox[1] - 1), None)
        if at is None:
            at = max((i + 1 for i, b in enumerate(out) if b.page <= fig.page), default=0)
        out.insert(at, fig)
    return out


def _artifact_text(mb: MBlock) -> str:
    """All the words of a block marked as an artifact. A table's words are in its cells and
    a figure's in ``figure_text``, not in ``runs``: reading only the runs dropped them."""
    if mb.kind == "table":
        return " ".join(t for _, aruns in mb.cells if (t := runs_text(aruns).strip()))
    if mb.kind == "figure":
        return mb.figure_text.strip()
    return runs_text(mb.runs).strip()


def _convert(mb: MBlock, cat: Catalogue, columns: list[tuple[float, float]]) -> Block | None:
    key = cat.key_of(mb)
    base = cat.styles.get(key) if key else None
    runs = to_runs(mb.runs, base) if mb.runs else []
    pg, bb = mb.page, mb.bbox
    kind = mb.kind

    if kind == "figure":
        return Figure(page=pg, bbox=bb, style=key, alt=mb.figure_text or None,
                      decorative=True if mb.raw.role == "chapter-art" else None)
    if kind == "table":
        return _table(mb, cat, key)
    if kind == "formula":
        return Formula(page=pg, bbox=bb, style=key, latex=mb.raw.latex, runs=runs)
    if not runs or not "".join(r.text for r in runs).strip():
        return None
    para = para_props(mb.lines, _column_of(mb, columns))
    if kind in ("title", "heading"):
        level = mb.raw.level if mb.raw.role else cat.heading_level.get(id(mb), 1)
        role = cast(Literal["chapter", "module", "section"] | None,
                    mb.raw.role if mb.raw.role in ("chapter", "module", "section") else None)
        return Heading(page=pg, bbox=bb, style=key, level=level or 1,
                       runs=runs, para=para, role=role)
    if kind == "caption":
        return Caption(page=pg, bbox=bb, style=key, runs=runs, para=para)
    if kind == "code":
        return Code(page=pg, bbox=bb, style=key, runs=runs)
    if kind == "list_item":
        marker, runs = _split_marker(runs, mb.raw.marker)
        if mb.raw.ordered is None and marker:
            mb.raw.ordered = is_ordered_marker(marker)
        return ListItem(page=pg, bbox=bb, style=key, marker=marker, runs=runs, para=para)
    return Paragraph(page=pg, bbox=bb, style=key, runs=runs, para=para)


def _split_marker(runs: list[Run], engine_marker: str | None) -> tuple[str | None, list[Run]]:
    """Move a leading bullet/number from the text into ``marker``. Only a marker printed in the
    text counts: the engine's own marker (Docling numbers items itself) is not on the page, and
    the item's ``ordered`` flag already says whether the list is numbered."""
    if not runs:
        return None, runs
    m = _LEADING_MARKER.match(runs[0].text)
    if m and (MARKER_RE.match(m.group(1)) or re.fullmatch(r"\d+[–-]\d+[.)]?", m.group(1))):
        # the marker may be the whole first run (e.g. a bullet in a symbol font)
        first = runs[0].model_copy(update={"text": runs[0].text[m.end():]})
        rest = [first, *runs[1:]] if first.text else runs[1:]
        if rest:
            rest[0] = rest[0].model_copy(update={"text": rest[0].text.lstrip()})
        return m.group(1), rest
    return None, runs


def _table(mb: MBlock, cat: Catalogue, key: str | None) -> Table:
    n_rows = max([mb.raw.n_rows] + [c.row + c.rowspan for c, _ in mb.cells])
    n_cols = max([mb.raw.n_cols] + [c.col + c.colspan for c, _ in mb.cells])
    grid: list[list[tuple[int, TableCell]]] = [[] for _ in range(n_rows)]
    body = cat.styles.get("body")
    for c, aruns in mb.cells:
        cell = TableCell(runs=to_runs(aruns, body) if aruns else [], rowspan=c.rowspan,
                         colspan=c.colspan, header=c.header, bbox=c.bbox)
        grid[c.row].append((c.col, cell))
    rows = [[cell for _, cell in sorted(r, key=lambda t: t[0])] for r in grid]
    return Table(page=mb.page, bbox=mb.bbox, style=key, rows=rows, n_cols=n_cols)


def _columns(blocks: Sequence[MBlock]) -> list[tuple[float, float]]:
    """Text column extents, measured from multi-line paragraphs across the whole document
    (they run from margin to margin, unlike headings or short lines)."""
    spans: list[tuple[float, float]] = []
    for mb in blocks:
        if mb.artifact is None and mb.kind == "paragraph" and len(mb.lines) >= 3:
            spans.append((mb.bbox[0], mb.bbox[2]))
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        if merged and a < merged[-1][1] - 2:          # overlapping → same column
            merged[-1][0] = min(merged[-1][0], a)
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _column_of(mb: MBlock, columns: list[tuple[float, float]]) -> tuple[float, float]:
    cx = (mb.bbox[0] + mb.bbox[2]) / 2
    for a, b in columns:
        if a - 1 <= mb.bbox[0] and mb.bbox[2] <= b + 1:
            return (a, b)
    for a, b in columns:
        if a <= cx <= b:
            return (min(a, mb.bbox[0]), max(b, mb.bbox[2]))
    return (mb.bbox[0], mb.bbox[2])


def _space_before(block: Block, mb: MBlock, prev: MBlock | None) -> None:
    para = getattr(block, "para", None)
    if para is None or prev is None:
        return
    gap = mb.bbox[1] - prev.bbox[3]
    overlaps = prev.bbox[0] < mb.bbox[2] and prev.bbox[2] > mb.bbox[0]
    if gap >= 0 and overlaps:
        para.space_before = round(gap, 1)


def _union(a: tuple[float, float, float, float],
           b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _assign_ids(doc: Document) -> None:
    n = 0
    for part in (doc.body, doc.footnotes, doc.references):
        for b in iter_blocks(part):
            n += 1
            b.id = f"b{n}"


def _link_captions(body: list[Block]) -> None:
    """Link each caption to the nearest figure/table (next first, then previous) on its page."""
    flat = [x for b in body for x in (b.children if isinstance(b, Box) else [b])
            if not isinstance(x, ListBlock)]
    for i, b in enumerate(flat):
        if not isinstance(b, Caption):
            continue
        label = re.match(r"^\s*[\u25b2\u25b3\u25b6\u25ba]?\s*(figure|fig\.?|table)\s+\d",
                         "".join(r.text for r in b.runs), re.I)
        if label:
            kind = Table if label[1].lower() == "table" else Figure
            candidates = [t for t in flat if isinstance(t, kind) and t.page == b.page
                          and t.caption_ref is None
                          and (kind is Table or _area(t.bbox) >= 1300)]
            def distance(t: Figure | Table, caption: Caption = b) -> float:
                dx = max(0.0, caption.bbox[0] - t.bbox[2], t.bbox[0] - caption.bbox[2])
                dy = max(0.0, caption.bbox[1] - t.bbox[3], t.bbox[1] - caption.bbox[3])
                return dx + dy
            target = min(candidates, key=distance, default=None)
            if target is not None and distance(target) < 100:
                target.caption_ref, b.target = b.id, target.id
                continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(flat):
                t = flat[j]
                if isinstance(t, Figure | Table) and t.page == b.page and t.caption_ref is None:
                    t.caption_ref = b.id
                    b.target = t.id
                    break


def _title(body: list[Block]) -> str | None:
    # A multi-chapter extract has no evidence for a whole-book title. Metadata
    # supplied by the caller takes precedence over this conservative fallback.
    chapters = [b for b in body if isinstance(b, Heading) and b.role == "chapter"]
    if len(chapters) > 1:
        return None
    if chapters:
        return "".join(r.text for r in chapters[0].runs).strip() or None
    for b in body:
        if isinstance(b, Heading) and not re.fullmatch(
                r"\s*(?:CHAPTER\s*)?\d+\s*", "".join(r.text for r in b.runs), re.I):
            return "".join(r.text for r in b.runs).strip() or None
    return None


def _classify_decorations(doc: Document) -> None:
    """Retain opener art, module badges and margin backgrounds as graphics, not figures.

    No pixels/assets are deleted. Labelled illustrations and diagrams keep their normal
    figure semantics; uncertain unlabelled images remain figures.
    """
    for page in doc.pages:
        blocks = [b for b in iter_blocks(doc.body) if b.page == page.n]
        text = [b for b in blocks if isinstance(b, Paragraph | Heading | ListItem)]
        left = min((b.bbox[0] for b in text), default=0)
        right = max((b.bbox[2] for b in text), default=page.width)
        labels = [b for b in text if isinstance(b, Heading) and
                  re.fullmatch(r"\s*MODULE\s*\d+\s*", "".join(r.text for r in b.runs), re.I)]
        opener = any(isinstance(b, Heading) and b.role == "chapter" for b in text)
        for b in blocks:
            if not isinstance(b, Figure) or b.caption_ref:
                continue
            badge = any(_overlap(b.bbox, h.bbox) >= 0.5 * _area(h.bbox) for h in labels)
            edge = (not b.source_ref and b.bbox[2] - b.bbox[0] < 0.18 * page.width
                    and (b.bbox[2] <= left + 15 or b.bbox[0] >= right - 15))
            width, height = b.bbox[2] - b.bbox[0], b.bbox[3] - b.bbox[1]
            strip = not b.source_ref and min(width, height) < 40 and max(width, height) > 4 * min(
                width, height)
            # Native bitmap fragments on an opener and small unlabelled icons remain
            # visible graphics. They are never silently deleted from the delivery.
            fragment = opener and not b.source_ref
            icon = width < 60 and height < 60
            if badge or edge or strip or fragment or icon:
                b.decorative = True
