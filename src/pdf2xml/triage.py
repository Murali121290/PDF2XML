"""Stage 0: what kind of PDF is this?"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
from pydantic import BaseModel

from pdf2xml.extract.types import StyleLayer
from pdf2xml.merge.text import CID_RE


class TriageReport(BaseModel):
    source: str
    sha256: str
    pages: int
    encrypted: bool
    tagged: bool
    born_digital: bool
    text_pages: int
    chars: int
    doc_type_guess: str = "generic"
    notes: list[str] = []


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def triage(pdf: Path, style: StyleLayer) -> TriageReport:
    notes: list[str] = []
    try:
        with pikepdf.open(pdf) as p:
            n_pages = len(p.pages)
            encrypted = bool(p.is_encrypted)
            root = p.Root
            mark_info = root.get("/MarkInfo")
            marked = bool(mark_info.get("/Marked", False)) if mark_info is not None else False
            tagged = "/StructTreeRoot" in root and marked
    except pikepdf.PasswordError:
        return TriageReport(source=pdf.name, sha256=sha256_of(pdf), pages=0, encrypted=True,
                            tagged=False, born_digital=False, text_pages=0, chars=0,
                            notes=["encrypted PDF: password required"])

    text_pages = sum(1 for pg in style.pages if sum(len(w.text) for w in pg.words) >= 20)
    chars = sum(len(w.text) for pg in style.pages for w in pg.words)
    checked = max(1, len(style.pages))
    born_digital = text_pages / checked >= 0.5
    if not born_digital:
        notes.append(f"only {text_pages}/{checked} pages have a text layer: likely scanned "
                     "(OCR is out of scope)")
    if tagged:
        notes.append("tagged PDF: structure tree present (tag-tree path is Phase 2)")
    cids = sum(len(CID_RE.findall(w.text)) for pg in style.pages for w in pg.words)
    if cids:
        notes.append(f"{cids} glyph(s) have no Unicode mapping and appear as (cid:N); "
                     "lone ones at the start of list items are treated as bullets")

    # Rough document-type guess from vocabulary; refined in later phases.
    sample = " ".join(w.text.lower() for pg in style.pages[:3] for w in pg.words)
    guess = "generic"
    if "abstract" in sample and ("references" in sample or "doi" in sample or "arxiv" in sample):
        guess = "paper"
    elif any(k in sample for k in ("exercise", "learning objectives", "activity", "worksheet")):
        guess = "textbook"
    return TriageReport(source=pdf.name, sha256=sha256_of(pdf), pages=n_pages,
                        encrypted=encrypted, tagged=tagged, born_digital=born_digital,
                        text_pages=text_pages, chars=chars, doc_type_guess=guess, notes=notes)
