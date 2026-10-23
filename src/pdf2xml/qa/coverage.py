"""Text-coverage invariant: every text-layer character appears exactly once in the output.

Compares multisets of non-whitespace characters: style-layer words vs. document (body, lists,
tables, footnotes, references, artifacts, list markers). Removed end-of-line hyphens are allowed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from pdf2xml.extract.types import StyleLayer
from pdf2xml.merge.text import SOFT_HYPHEN, fix_ligatures
from pdf2xml.model import Document, Formula, ListItem, Table, iter_blocks


@dataclass
class Coverage:
    source_chars: int
    output_chars: int
    missing: dict[str, int]
    extra: dict[str, int]

    @property
    def ratio(self) -> float:
        lost = sum(self.missing.values())
        return 1.0 if self.source_chars == 0 else 1 - lost / self.source_chars

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra


def _norm(text: str) -> Counter[str]:
    return Counter(c for c in fix_ligatures(text).replace(SOFT_HYPHEN, "") if not c.isspace())


def document_text(doc: Document) -> str:
    parts: list[str] = []
    for part in (doc.body, doc.footnotes, doc.references):
        for b in iter_blocks(part):
            if isinstance(b, ListItem) and b.marker:
                parts.append(b.marker)
            if isinstance(b, Table):
                parts.extend(r.text for row in b.rows for c in row for r in c.runs)
            elif isinstance(b, Formula):
                parts.extend(r.text for r in b.runs)
            else:
                parts.extend(r.text for r in getattr(b, "runs", []))
    parts.extend(a.text for a in doc.artifacts)
    return " ".join(parts)


def check_coverage(style: StyleLayer, doc: Document) -> Coverage:
    src: Counter[str] = Counter()
    for p in style.pages:
        for w in p.words:
            src += _norm(w.text)
    out = _norm(document_text(doc))
    allowed_hyphens = doc.meta.stats.get("dehyphenated", 0)
    if allowed_hyphens:
        out["-"] += allowed_hyphens
    missing = dict(sorted((src - out).items()))
    extra = dict(sorted((out - src).items()))
    return Coverage(sum(src.values()), sum(out.values()), missing, extra)
