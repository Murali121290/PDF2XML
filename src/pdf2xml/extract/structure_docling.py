"""Structure layer via Docling (MIT).

``from_docling_document`` is a pure mapping (DoclingDocument → StructureLayer) so it can be tested
against saved Docling JSON without running the models.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pdf2xml.extract.types import (
    RawBlock,
    RawCell,
    RawKind,
    StructureLayer,
    StyleLayer,
    rbox,
)

log = logging.getLogger(__name__)

_LABELS: dict[str, RawKind] = {
    "title": "title",
    "section_header": "heading",
    "text": "paragraph",
    "paragraph": "paragraph",
    "handwritten_text": "paragraph",
    "list_item": "list_item",
    "caption": "caption",
    "footnote": "footnote",
    "table": "table",
    "document_index": "table",
    "picture": "figure",
    "chart": "figure",
    "formula": "formula",
    "code": "code",
    "page_header": "page_header",
    "page_footer": "page_footer",
    "reference": "reference",
}


class DoclingEngine:
    name = "docling"

    def __init__(self) -> None:
        self.last_raw: dict[str, Any] | None = None

    def analyze(
        self,
        pdf: Path,
        style: StyleLayer,
        pages: Sequence[int] | None = None,
        cache: Path | None = None,
    ) -> StructureLayer:
        from docling_core.types.doc import DoclingDocument

        if cache is not None and cache.exists():
            log.info("reusing cached Docling output %s", cache)
            raw = json.loads(cache.read_text(encoding="utf-8"))
            doc = DoclingDocument.model_validate(raw)
        else:
            doc = self._convert(pdf, pages)
            raw = doc.export_to_dict()
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.last_raw = raw
        layer = from_docling_document(doc, _crop_origins(pdf, doc))
        if pages:
            keep = set(pages)
            layer.blocks = [b for b in layer.blocks if b.page in keep]
        return layer

    @staticmethod
    def _convert(pdf: Path, pages: Sequence[int] | None) -> Any:
        kwargs: dict[str, Any] = {}
        if pages:
            kwargs["page_range"] = (min(pages), max(pages))
        result = _converter().convert(str(pdf), **kwargs)
        return result.document


@lru_cache(maxsize=1)
def _converter() -> Any:
    """One converter per process: loading the layout and table models takes ~40 s on CPU."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False                   # born-digital only (CLAUDE.md §1)
    opts.do_table_structure = True
    opts.do_formula_enrichment = False    # Phase 3
    opts.do_code_enrichment = False
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def _crop_origins(pdf: Path, doc: Any) -> dict[int, tuple[float, float]]:
    """Docling measures a page from its CropBox, the style layer from its MediaBox. In a
    printer's PDF (crop marks and slug lines around a smaller CropBox) they differ by the
    CropBox's top-left corner: that shift, per page, when Docling's page is the CropBox."""
    import pypdfium2 as pdfium

    out: dict[int, tuple[float, float]] = {}
    pdoc = pdfium.PdfDocument(str(pdf))
    try:
        for no, p in doc.pages.items():
            n = int(no)
            if not 1 <= n <= len(pdoc):
                continue
            page = pdoc[n - 1]
            ml, _, _, mt = page.get_mediabox()
            cl, cb, cr, ct = page.get_cropbox()
            w, h = float(p.size.width), float(p.size.height)
            is_crop = abs(w - (cr - cl)) < 1 and abs(h - (ct - cb)) < 1
            if is_crop and (abs(cl - ml) > 0.5 or abs(mt - ct) > 0.5):
                out[n] = (float(cl - ml), float(mt - ct))
    finally:
        pdoc.close()
    return out


def _to_top_left(bbox: Any, page_h: float, off: tuple[float, float] = (0.0, 0.0)
                 ) -> tuple[float, float, float, float]:
    from docling_core.types.doc import CoordOrigin

    if bbox.coord_origin == CoordOrigin.BOTTOMLEFT:
        bbox = bbox.to_top_left_origin(page_height=page_h)
    x0, x1 = sorted((bbox.l, bbox.r))
    y0, y1 = sorted((bbox.t, bbox.b))
    return rbox(x0 + off[0], y0 + off[1], x1 + off[0], y1 + off[1])


def from_docling_document(doc: Any, origins: dict[int, tuple[float, float]] | None = None
                          ) -> StructureLayer:
    """``origins``: per page, where Docling's page (the CropBox) sits on the style layer's page
    (the MediaBox); every box is moved by it."""
    from docling_core.types.doc import (
        ContentLayer,
        GroupItem,
        GroupLabel,
        ListItem,
        SectionHeaderItem,
        TableItem,
    )

    sizes = {int(no): (float(p.size.width), float(p.size.height)) for no, p in doc.pages.items()}
    layer = StructureLayer(engine="docling", blocks=[], page_sizes=sizes)
    order = 0
    list_groups = (GroupLabel.LIST, GroupLabel.ORDERED_LIST)

    for item, _level in doc.iterate_items(included_content_layers=set(ContentLayer)):
        if isinstance(item, GroupItem):
            continue
        label = getattr(item.label, "value", str(item.label))
        kind = _LABELS.get(label, "other")
        text = getattr(item, "text", "") or ""

        level = int(item.level) if isinstance(item, SectionHeaderItem) else None
        group = ordered = marker = None
        depth = 0
        if isinstance(item, ListItem):
            marker = item.marker or None
            ordered = bool(item.enumerated)
            parent = item.parent.resolve(doc) if item.parent else None
            if isinstance(parent, GroupItem) and parent.label in list_groups:
                group = parent.self_ref
                ordered = ordered or parent.label == GroupLabel.ORDERED_LIST
                # nesting depth = number of list groups above this one
                anc = parent.parent.resolve(doc) if parent.parent else None
                while anc is not None:
                    if isinstance(anc, GroupItem) and anc.label in list_groups:
                        depth += 1
                    anc = anc.parent.resolve(doc) if getattr(anc, "parent", None) else None

        for prov in item.prov:
            page_no = int(prov.page_no)
            page_h = sizes.get(page_no, (0.0, 0.0))[1]
            off = (origins or {}).get(page_no, (0.0, 0.0))
            block = RawBlock(
                page=page_no,
                bbox=_to_top_left(prov.bbox, page_h, off),
                kind=kind,
                order=order,
                text=text,
                level=level,
                group=group,
                ordered=ordered,
                marker=marker,
                depth=depth,
            )
            order += 1
            if isinstance(item, TableItem):
                block.n_rows = int(item.data.num_rows)
                block.n_cols = int(item.data.num_cols)
                for c in item.data.table_cells:
                    block.cells.append(
                        RawCell(
                            row=int(c.start_row_offset_idx),
                            col=int(c.start_col_offset_idx),
                            rowspan=max(1, int(c.row_span)),
                            colspan=max(1, int(c.col_span)),
                            header=bool(c.column_header or c.row_header),
                            bbox=(_to_top_left(c.bbox, page_h, off) if c.bbox is not None
                                  else None),
                            text=c.text or "",
                        )
                    )
            if kind == "formula" and text and "\\" in text:
                block.latex = text
            layer.blocks.append(block)
    return layer
