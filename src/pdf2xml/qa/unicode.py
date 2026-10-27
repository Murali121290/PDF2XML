"""Unicode check: is the extracted text the text that is printed?

A PDF draws glyphs; the characters come from the font's mapping to Unicode (``/ToUnicode``).
When that mapping is missing or wrong, the page looks right but the text is not, and no
text comparison can notice: both sides read the same wrong characters. This check looks for
the signs of it:

- **characters**: unmapped glyphs ``(cid:N)``, private-use code points, U+FFFD, control
  characters; text that is not in NFC (informational);
- **Indic order**: Tamil prefix vowel signs (ெ ே ை) stored in visual order, before their
  consonant, and words that start with a combining mark. Both mean the text is scrambled;
- **fonts** (pikepdf): each font's type, embedding and ``/ToUnicode``; fonts whose text cannot
  be trusted (Type 3, CID fonts or custom encodings without ``/ToUnicode``), and legacy Tamil
  font encodings (Bamini, TSCII …) whose "text" is Latin letters.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pikepdf

from pdf2xml.extract.types import StyleLayer
from pdf2xml.merge.text import CID_RE
from pdf2xml.model import Document, ListItem, iter_blocks
from pdf2xml.styles.fonts import strip_subset

# a Tamil prefix vowel sign must follow its consonant in Unicode (logical) order
_TAMIL_MISORDER = re.compile(r"(?<![க-ஹ])[ெ-ை]")
_LEGACY_TAMIL = re.compile(r"bamini|tscii|vanavil|elango|kalaham|shree-?tam|^ta[bmu][-_]",
                           re.I)
_ODD_GLYPH = re.compile(r"^(?:g|G|c|C|cid|glyph|index|char)\d+$|^#?\d+$")
_SAMPLES = 8


def _pua(c: str) -> bool:
    o = ord(c)
    return 0xE000 <= o <= 0xF8FF or o >= 0xF0000


def check_unicode(style: StyleLayer, pdf: Path | None = None,
                  doc: Document | None = None) -> dict[str, Any]:
    """``doc``: unmapped glyphs it uses as list markers are bullets (informational)."""
    bullets = {b.marker for b in iter_blocks(doc.body) if isinstance(b, ListItem)
               and b.marker and CID_RE.fullmatch(b.marker)} if doc is not None else set()
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    words_by_font: Counter[str] = Counter()
    pages_by_font: dict[str, set[int]] = defaultdict(set)

    def note(kind: str, page: int, text: str, n: int = 1) -> None:
        counts[kind] += n
        if len(samples[kind]) < _SAMPLES:
            samples[kind].append({"page": page, "text": text})

    for page in style.pages:
        for w in page.words:
            words_by_font[w.font] += 1
            pages_by_font[w.font].add(page.number)
            t = w.text
            if t.strip() in bullets:
                note("unmapped_bullets", page.number, t)
                continue
            if n := len(CID_RE.findall(t)):
                note("unmapped_glyphs", page.number, t, n)
                t = CID_RE.sub("", t)
            if n := sum(1 for c in t if _pua(c)):
                note("private_use", page.number, t, n)
            if n := t.count("�"):
                note("replacement_char", page.number, t, n)
            if n := sum(1 for c in t if unicodedata.category(c) == "Cc"):
                note("control_chars", page.number, t, n)
            if t and unicodedata.category(t[0]).startswith("M"):
                note("starts_with_mark", page.number, t)
            if n := len(_TAMIL_MISORDER.findall(t)):
                note("tamil_vowel_sign_order", page.number, t, n)
            if t != unicodedata.normalize("NFC", t):
                note("not_nfc", page.number, t)

    fonts = audit_fonts(pdf, [p.number for p in style.pages]) if pdf else []
    for f in fonts:
        f["words"] = words_by_font.get(f["name"], 0)
        f["pages"] = sorted(pages_by_font.get(f["name"], set()))[:20]
        if _LEGACY_TAMIL.search(f["name"]):
            f["risk"] = ("legacy Tamil font encoding: the text is stored as Latin letters and "
                         "needs converting to Unicode")
    risky = [f for f in fonts if f.get("risk") and f["words"]]

    serious = ("unmapped_glyphs", "private_use", "replacement_char", "control_chars",
               "starts_with_mark", "tamil_vowel_sign_order")
    bad = {k: counts[k] for k in serious if counts[k]}
    status = "warn" if bad or risky else (
        "info" if counts["not_nfc"] or counts["unmapped_bullets"] else "ok")
    parts = [f"{k.replace('_', ' ')}: {n}" for k, n in bad.items()]
    if risky:
        parts.append(f"{len(risky)} font(s) whose text may be wrong")
    if counts["unmapped_bullets"]:
        parts.append(f"{counts['unmapped_bullets']} list bullet(s) without a Unicode mapping "
                     "(informational)")
    if counts["not_nfc"]:
        parts.append(f"{counts['not_nfc']} word(s) not in NFC (informational)")
    summary = "; ".join(parts) or f"no problems in {sum(words_by_font.values())} words"
    return {
        "status": status, "summary": summary,
        "counts": dict(sorted(counts.items())),
        "samples": {k: v for k, v in sorted(samples.items())},
        "fonts": sorted(fonts, key=lambda f: (not f.get("risk"), -f["words"], f["name"])),
    }


def audit_fonts(pdf: Path, pages: list[int]) -> list[dict[str, Any]]:
    """Every font used on ``pages`` (page resources and the form XObjects they draw)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with pikepdf.open(pdf) as p:
            for n in pages:
                if 1 <= n <= len(p.pages):
                    _collect(p.pages[n - 1].obj.get("/Resources"), out, set())
    except (pikepdf.PdfError, OSError, ValueError):
        return []
    return list(out.values())


def _collect(res: Any, out: dict[str, dict[str, Any]], seen: set[tuple[int, int]],
             depth: int = 0) -> None:
    if not isinstance(res, pikepdf.Dictionary) or depth > 8:
        return
    fonts = res.get("/Font")
    if isinstance(fonts, pikepdf.Dictionary):
        for _, f in fonts.items():
            if isinstance(f, pikepdf.Dictionary):
                info = _font(f)
                prev = out.get(info["name"])
                # one name can stand for several font objects (subsets): keep the worst
                if prev is None or (info.get("risk") and not prev.get("risk")):
                    out[info["name"]] = info
    xobjects = res.get("/XObject")
    if isinstance(xobjects, pikepdf.Dictionary):
        for _, x in xobjects.items():
            if not isinstance(x, pikepdf.Stream) or x.get("/Subtype") != "/Form":
                continue
            key = x.objgen
            if key != (0, 0):
                if key in seen:
                    continue
                seen.add(key)
            _collect(x.get("/Resources"), out, seen, depth + 1)


def _font(f: pikepdf.Dictionary) -> dict[str, Any]:
    subtype = str(f.get("/Subtype", "")).lstrip("/")
    name = strip_subset(str(f.get("/BaseFont", "")).lstrip("/")) or f"(unnamed {subtype})"
    desc = f.get("/FontDescriptor")
    if subtype == "Type0":
        kids = f.get("/DescendantFonts")
        if isinstance(kids, pikepdf.Array) and len(kids):
            desc = kids[0].get("/FontDescriptor")
    embedded = isinstance(desc, pikepdf.Dictionary) and any(
        k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
    enc = f.get("/Encoding")
    odd: list[str] = []
    if isinstance(enc, pikepdf.Name):
        encoding = str(enc).lstrip("/")
    elif isinstance(enc, pikepdf.Dictionary):
        encoding = "custom (Differences)" if "/Differences" in enc else "custom"
        diffs = enc.get("/Differences")
        if isinstance(diffs, pikepdf.Array):
            # standard glyph names (A, eacute, uni0B95 …) are readable without /ToUnicode;
            # made-up ones (g31, cid12, glyph7) are not
            odd = [str(d).lstrip("/") for d in diffs if isinstance(d, pikepdf.Name)
                   and _ODD_GLYPH.match(str(d).lstrip("/"))]
    else:
        encoding = "built-in"
    to_unicode = "/ToUnicode" in f
    symbolic = isinstance(desc, pikepdf.Dictionary) and bool(int(desc.get("/Flags", 0)) & 4)
    info: dict[str, Any] = {"name": name, "type": subtype, "embedded": embedded,
                            "to_unicode": to_unicode, "encoding": encoding}
    if subtype == "Type3":
        info["risk"] = "Type 3 font (glyphs drawn as graphics): text may be missing or wrong"
    elif not to_unicode and subtype == "Type0":
        info["risk"] = "CID font without /ToUnicode: characters are guessed from glyph ids"
    elif not to_unicode and odd:
        info["risk"] = (f"custom encoding with non-standard glyph names ({', '.join(odd[:4])} "
                        "…) and no /ToUnicode: characters may be wrong")
    elif not to_unicode and symbolic and embedded and encoding == "built-in":
        info["risk"] = "symbolic font with its own encoding and no /ToUnicode: characters " \
                       "may be wrong"
    return info
