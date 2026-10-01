"""Style layer via pdfplumber (MIT): words with exact font, size, colour and position."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pdfplumber

from pdf2xml.extract.types import Drawing, ImageRef, PageLayer, StyleLayer, Word, r2, rbox
from pdf2xml.styles.fonts import parse_font, strip_subset, to_hex

# Low tolerance: we would rather split too much (the merger re-joins words by measured gaps)
# than glue two words together when a PDF has no explicit space characters.
_X_TOL = 1.5
_Y_TOL = 2.0


class PdfplumberBackend:
    name = "pdfplumber"

    def extract(self, pdf: Path, pages: Sequence[int] | None = None) -> StyleLayer:
        out: list[PageLayer] = []
        with pdfplumber.open(pdf) as doc:
            numbers = pages or range(1, len(doc.pages) + 1)
            for n in numbers:
                if not 1 <= n <= len(doc.pages):
                    continue
                out.append(self._page(doc.pages[n - 1], n))
        return StyleLayer(backend=self.name, pages=out)

    def _page(self, page: Any, n: int) -> PageLayer:
        # pdfplumber reports coordinates relative to the page's top-left already.
        x_off, y_off = float(page.bbox[0]), float(page.bbox[1])
        layer = PageLayer(number=n, width=r2(page.width), height=r2(page.height))
        trim = getattr(page, "trimbox", None)
        if trim is not None:
            t = rbox(trim[0] - x_off, trim[1] - y_off, trim[2] - x_off, trim[3] - y_off)
            # only a real trim area: inside the page and noticeably smaller than it
            if (t[0] >= -1 and t[1] >= -1 and t[2] <= layer.width + 1 and t[3] <= layer.height + 1
                    and t[2] - t[0] > 72 and t[3] - t[1] > 72
                    and (t[0] > 2 or t[1] > 2 or t[2] < layer.width - 2
                         or t[3] < layer.height - 2)):
                layer.trim = t

        words = page.extract_words(
            x_tolerance=_X_TOL,
            y_tolerance=_Y_TOL,
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["fontname", "size", "non_stroking_color"],
            expand_ligatures=True,
        )
        for i, w in enumerate(words):
            text = w["text"]
            if not text.strip():
                continue
            info = parse_font(w.get("fontname", ""))
            layer.words.append(
                Word(
                    page=n,
                    bbox=rbox(w["x0"] - x_off, w["top"] - y_off, w["x1"] - x_off,
                              w["bottom"] - y_off),
                    text=text,
                    font=strip_subset(w.get("fontname", "")),
                    family=info.family,
                    size=r2(w.get("size", 0.0)),
                    color=to_hex(w.get("non_stroking_color")),
                    bold=info.bold,
                    italic=info.italic,
                    mono=info.mono,
                    upright=bool(w.get("upright", True)),
                    seq=i,
                )
            )

        for rect in page.rects:
            layer.drawings.append(
                Drawing(
                    page=n,
                    bbox=rbox(rect["x0"] - x_off, rect["top"] - y_off, rect["x1"] - x_off,
                              rect["bottom"] - y_off),
                    kind="rect",
                    fill=to_hex(rect.get("non_stroking_color")) if rect.get("fill") else None,
                    stroke=to_hex(rect.get("stroking_color")) if rect.get("stroke") else None,
                    width=r2(rect.get("linewidth") or 0.0),
                )
            )
        # large filled or closed curves are rounded-corner boxes; the rest is rules or artwork
        for curve in page.curves:
            if curve["x1"] - curve["x0"] < 40 or curve["bottom"] - curve["top"] < 20:
                continue
            pts = curve.get("pts") or []
            closed = len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1 and abs(
                pts[0][1] - pts[-1][1]) < 1
            if not (curve.get("fill") or closed):
                continue
            layer.drawings.append(
                Drawing(
                    page=n,
                    bbox=rbox(curve["x0"] - x_off, curve["top"] - y_off, curve["x1"] - x_off,
                              curve["bottom"] - y_off),
                    kind="curve",
                    fill=to_hex(curve.get("non_stroking_color")) if curve.get("fill") else None,
                    stroke=to_hex(curve.get("stroking_color")) if curve.get("stroke") else None,
                    width=r2(curve.get("linewidth") or 0.0),
                )
            )
        for line in page.lines:
            layer.drawings.append(
                Drawing(
                    page=n,
                    bbox=rbox(line["x0"] - x_off, line["top"] - y_off, line["x1"] - x_off,
                              line["bottom"] - y_off),
                    kind="line",
                    stroke=to_hex(line.get("stroking_color")),
                    width=r2(line.get("linewidth") or 0.0),
                )
            )
        for img in page.images:
            layer.images.append(
                ImageRef(
                    page=n,
                    bbox=rbox(img["x0"] - x_off, img["top"] - y_off, img["x1"] - x_off,
                              img["bottom"] - y_off),
                    name=str(img.get("name", "")),
                )
            )
        return layer
