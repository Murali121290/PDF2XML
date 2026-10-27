"""Text-layer check: a second, independent reader of the PDF's text (PDFium).

The coverage invariant compares the output with the style layer (pdfplumber), so text that
pdfplumber itself never read is invisible to it. PDFium reads the same text with its own
parser; each page's characters are compared as multisets (whitespace, ligature forms and
unmapped glyphs set aside). Characters PDFium reads and pdfplumber does not are text the
pipeline never saw.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from typing import Any

import pypdfium2 as pdfium

from pdf2xml.extract.types import StyleLayer
from pdf2xml.merge.text import CID_RE, SOFT_HYPHEN

# per page: a difference smaller than this is noise (PDFium and pdfplumber differ on a few
# hyphens and generated characters)
_TOLERANCE = 3
_SHARE = 0.002
_HYPHENS = str.maketrans({"\x02": "-", "￾": "-", "‐": "-", "‑": "-"})


def _chars(text: str) -> Counter[str]:
    # PDFium marks a hyphen at the end of a line as U+0002 or U+FFFE; the two readers also
    # disagree on which hyphen character a font's hyphen glyph is
    t = text.replace(SOFT_HYPHEN, "").translate(_HYPHENS)
    t = unicodedata.normalize("NFKC", t)
    return Counter(c for c in t if not c.isspace() and unicodedata.category(c)[0] != "C")


def check_text_layer(style: StyleLayer, pdf: pdfium.PdfDocument) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    total_plumber = total_pdfium = total_unread = total_extra = total_case = 0
    for layer in style.pages:
        page = pdf[layer.number - 1]
        tp = page.get_textpage()
        try:
            theirs = _chars(tp.get_text_range())
        finally:
            tp.close()
            page.close()
        words = layer.words
        cids = sum(len(CID_RE.findall(w.text)) for w in words)
        ours = _chars(" ".join(CID_RE.sub("", w.text) for w in words))
        unread = theirs - ours            # PDFium has it, the style layer does not
        extra = ours - theirs
        # the same letter in the other case is no loss: running heads in capitals that a
        # tagged PDF's /ActualText spells in lower case, small caps …
        case = 0
        for c in sorted(unread):
            s = c.swapcase()
            if s != c and extra.get(s):
                k = min(unread[c], extra[s])
                unread[c] -= k
                extra[s] -= k
                case += k
        unread, extra = +unread, +extra
        total_case += case
        n_unread, n_extra = sum(unread.values()), sum(extra.values())
        total_plumber += sum(ours.values())
        total_pdfium += sum(theirs.values())
        limit = max(_TOLERANCE, _SHARE * sum(theirs.values()))
        if n_unread > limit + cids or n_extra > limit:
            pages.append({
                "page": layer.number, "pdfium_chars": sum(theirs.values()),
                "style_layer_chars": sum(ours.values()),
                "not_in_style_layer": n_unread, "only_in_style_layer": n_extra,
                "unmapped_glyphs": cids,
                "top_not_in_style_layer": dict(unread.most_common(15)),
                "top_only_in_style_layer": dict(extra.most_common(15)),
            })
            total_unread += n_unread
            total_extra += n_extra
    worse = [p for p in pages if p["not_in_style_layer"] > max(_TOLERANCE, p["unmapped_glyphs"])]
    status = "warn" if worse else ("info" if pages or total_case else "ok")
    summary = (f"PDFium read {total_pdfium} characters, the style layer {total_plumber}; "
               f"{len(pages)} page(s) differ")
    if total_case:
        summary += (f"; {total_case} letter(s) read in the other case (capitals vs. "
                    "replacement text), not lost")
    if worse:
        summary += f"; {total_unread} character(s) on {len(worse)} page(s) not read"
    return {"status": status, "summary": summary, "pages": pages,
            "case_differences": total_case}
