"""Debug overlays: page images with coloured block boxes, ids and reading order."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
from PIL import ImageDraw

from pdf2xml.export.assets import page_origin
from pdf2xml.model import Document, ListBlock, iter_blocks

_COLORS = {
    "heading": (220, 40, 40), "paragraph": (40, 110, 220), "caption": (150, 60, 200),
    "list": (0, 150, 120), "list_item": (0, 180, 80), "table": (230, 140, 0),
    "figure": (240, 200, 0), "formula": (200, 0, 150), "code": (90, 90, 90),
    "box": (0, 170, 200), "artifact": (170, 170, 170),
}


def _box(draw: ImageDraw.ImageDraw, scale: float, b: tuple[float, float, float, float],
         color: tuple[int, int, int], label: str, width: int = 2,
         origin: tuple[float, float] = (0.0, 0.0)) -> None:
    ox, oy = origin
    x0, y0, x1, y1 = ((b[0] - ox) * scale, (b[1] - oy) * scale, (b[2] - ox) * scale,
                      (b[3] - oy) * scale)
    draw.rectangle([x0, y0, x1, y1], outline=(*color, 255), fill=(*color, 28), width=width)
    if label:
        draw.text((x0 + 2, max(0, y0 - 11)), label, fill=(*color, 255))


def render_overlays(pdf: Path, doc: Document, out_dir: Path, scale: float = 1.5) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    pages = {p.n for p in doc.pages}
    pdf_doc = pdfium.PdfDocument(str(pdf))
    try:
        for n in sorted(pages):
            page = pdf_doc[n - 1]
            origin = page_origin(page)
            img = page.render(scale=scale).to_pil().convert("RGB")
            draw = ImageDraw.Draw(img, "RGBA")
            for a in doc.artifacts:
                if a.page == n:
                    _box(draw, scale, a.bbox, _COLORS["artifact"], a.kind, 1, origin)
            for part in (doc.body, doc.footnotes, doc.references):
                for b in iter_blocks(part):
                    if b.page != n:
                        continue
                    color = _COLORS.get(b.type, (0, 0, 0))
                    is_list = isinstance(b, ListBlock)
                    label = "" if is_list else f"{b.id} {b.type} {b.style or ''}"
                    _box(draw, scale, b.bbox, color, label, 1 if is_list else 2, origin)
            path = out_dir / f"page-{n:03d}.png"
            img.save(path)
            written.append(path)
    finally:
        pdf_doc.close()
    return written
