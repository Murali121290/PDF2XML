"""Figure images: crop the figure's box from a rendered page (pypdfium2)."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pypdfium2 as pdfium

from pdf2xml.model import Document, Figure, iter_blocks


def page_origin(page: pdfium.PdfPage) -> tuple[float, float]:
    """Where the rendered area (the CropBox) starts on the page our boxes are measured on (the
    MediaBox, top-left origin). Non-zero for a printer's PDF with crop marks around the page."""
    ml, _, _, mt = page.get_mediabox()
    cl, _, _, ct = page.get_cropbox()
    return float(cl - ml), float(mt - ct)


def extract_figures(pdf: Path, doc: Document, out_dir: Path, scale: float = 2.0) -> int:
    """Render each figure's bbox to ``assets/p<page>_<id>.png`` and set ``Figure.image``."""
    figures: dict[int, list[Figure]] = defaultdict(list)
    for b in iter_blocks(doc.body):
        if isinstance(b, Figure):
            figures[b.page].append(b)
    if not figures:
        return 0
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    count = 0
    pdf_doc = pdfium.PdfDocument(str(pdf))
    try:
        for page_no, figs in sorted(figures.items()):
            page = pdf_doc[page_no - 1]
            ox, oy = page_origin(page)
            img = page.render(scale=scale).to_pil()
            for f in figs:
                box = (f.bbox[0] - ox, f.bbox[1] - oy, f.bbox[2] - ox, f.bbox[3] - oy)
                x0, y0, x1, y1 = (round(v * scale) for v in box)
                if x1 - x0 < 2 or y1 - y0 < 2:
                    continue
                rel = f"assets/p{page_no}_{f.id}.png"
                img.crop((x0, y0, x1, y1)).save(out_dir / rel)
                f.image = rel
                count += 1
    finally:
        pdf_doc.close()
    return count
