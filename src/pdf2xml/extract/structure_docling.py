"""Structure layer via Docling (MIT).

``from_docling_document`` is a pure mapping (DoclingDocument → StructureLayer) so it can be tested
against saved Docling JSON without running the models.
"""

from __future__ import annotations

import json
import logging
import os
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


# The models this pipeline runs (OCR, formula and picture models stay off), as
# ``docling-tools models download layout tableformer`` lays them out in a folder.
REQUIRED_MODELS = {
    "layout": "docling-project--docling-layout-heron",
    "tableformer": "docling-project--docling-models",
}
REQUIRED_FILES = {
    "layout": ("config.json", "preprocessor_config.json", "model.safetensors"),
    "tableformer": tuple(
        f"model_artifacts/tableformer/{mode}/{name}"
        for mode in ("fast", "accurate")
        for name in ("tm_config.json", f"tableformer_{mode}.safetensors")
    ),
}
DEFAULT_MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
CACHED_MODELS_DIR = Path.home() / ".cache" / "docling" / "models"
MODELS_ENV = "PDF2XML_MODELS_DIR"


def configure_offline() -> None:
    """Conversion only reads local models; model downloads belong to the preparation step."""
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                 "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK"):
        os.environ[name] = "1"


def missing_models(models_dir: Path) -> list[str]:
    """What a models folder lacks for this pipeline (empty when it is usable)."""
    if not models_dir.is_dir():
        return [f"{models_dir} is not a folder"]
    missing = []
    for kind, folder in REQUIRED_MODELS.items():
        if not (models_dir / folder).is_dir():
            missing.append(f"{kind} model ({folder})")
            continue
        for filename in REQUIRED_FILES[kind]:
            path = models_dir / folder / filename
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(f"{kind} model file ({folder}/{filename})")
    return missing


def resolve_models_dir(explicit: Path | None = None) -> Path:
    """Find complete local models without falling back to a network download."""
    asked = explicit or (Path(os.environ[MODELS_ENV]) if os.environ.get(MODELS_ENV) else None)
    if asked is not None:
        missing = missing_models(asked)
        if missing:
            raise ValueError(f"models folder {asked}: missing {', '.join(missing)}; run "
                             "`python tools/prepare_models.py` before starting the service. "
                             "Runtime model downloads are disabled.")
        return asked
    for candidate in (DEFAULT_MODELS_DIR, CACHED_MODELS_DIR):
        if not missing_models(candidate):
            return candidate
    raise ValueError(
        "pre-downloaded Docling models not found; run `python tools/prepare_models.py` "
        f"before starting the service, or set {MODELS_ENV} to a complete local model folder. "
        "Runtime model downloads are disabled."
    )


class DoclingEngine:
    name = "docling"

    def __init__(self, models_dir: Path | None = None) -> None:
        configure_offline()
        self.last_raw: dict[str, Any] | None = None
        self.models_dir = resolve_models_dir(models_dir)

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

    def _convert(self, pdf: Path, pages: Sequence[int] | None) -> Any:
        kwargs: dict[str, Any] = {}
        if pages:
            kwargs["page_range"] = (min(pages), max(pages))
        models = str(self.models_dir) if self.models_dir is not None else None
        result = _converter(models).convert(str(pdf), **kwargs)
        return result.document


def pipeline_options(models_dir: str | None) -> Any:
    """Docling's PDF options: no OCR (born-digital only, CLAUDE.md §1), tables on, formula
    and code enrichment off (Phase 3); models from ``models_dir`` when given."""
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True
    opts.do_formula_enrichment = False
    opts.do_code_enrichment = False
    opts.enable_remote_services = False
    opts.allow_external_plugins = False
    if models_dir is not None:
        opts.artifacts_path = models_dir
    return opts


@lru_cache(maxsize=2)
def _converter(models_dir: str | None = None) -> Any:
    """One converter per process (and models folder): loading the layout and table models
    takes ~40 s on CPU."""
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if models_dir is not None:
        log.info("Docling models from %s", models_dir)
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options(models_dir))})


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
