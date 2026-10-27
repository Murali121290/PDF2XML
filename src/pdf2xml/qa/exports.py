"""Export text check: is every canonical text block in the BITS / JATS file?

The canonical text is already checked against the PDF (``qa/coverage.py``); this checks the
next step. Each canonical block (paragraph, heading, list item, table cell, footnote,
reference …) is looked up in the export's text, whitespace ignored. A block that is not there
verbatim is one of:

- **dropped**: left out on purpose, as recorded by the export (running heads, author queries,
  list markers given by ``list-type``, the contents page in JATS …);
- **moved**: every word is in the export, in another order or split up (a byline turned into
  ``surname``/``given-names``, a contents line without its dot leaders);
- **missing**: words of it are nowhere in the export. These are the real losses.

Artifacts (running heads and feet, page numbers, slug lines) are not exported by design; the
artifact list in the canonical JSON keeps them.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from pdf2xml.merge.text import SOFT_HYPHEN, fix_ligatures
from pdf2xml.model import Document, Formula, ListItem, Table, iter_blocks

_WS = re.compile(r"\s+")
_WORD = re.compile(r"\w+")
_MAX_LISTED = 50


def norm(text: str) -> str:
    """The text as compared: ligatures expanded, NFC, soft hyphens and whitespace removed."""
    t = unicodedata.normalize("NFC", fix_ligatures(text)).replace(SOFT_HYPHEN, "")
    return _WS.sub("", t)


@dataclass
class TextBlock:
    page: int
    kind: str
    text: str


def canonical_blocks(doc: Document) -> Iterator[TextBlock]:
    """The canonical document's text, block by block (artifacts excluded)."""
    for part in (doc.body, doc.footnotes, doc.references):
        for b in iter_blocks(part):
            if isinstance(b, ListItem) and b.marker:
                yield TextBlock(b.page, "list marker", b.marker)
            if isinstance(b, Table):
                for row in b.rows:
                    for c in row:
                        t = "".join(r.text for r in c.runs)
                        if t.strip():
                            yield TextBlock(b.page, "table cell", t)
                continue
            if isinstance(b, Formula) and b.latex:
                continue                     # written as TeX, not as the printed characters
            t = "".join(r.text for r in getattr(b, "runs", []) or [])
            if t.strip():
                yield TextBlock(b.page, b.type, t)


def export_text(path: Path) -> str:
    """All text of an XML file, comments and processing instructions included (author
    queries can be written as either)."""
    root = etree.parse(str(path), etree.XMLParser(load_dtd=False, no_network=True,
                                                  resolve_entities=False, huge_tree=True))
    parts: list[str] = []

    def walk(node: etree._Element) -> None:
        # document order: the node's text, its children, then its tail
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(root.getroot())
    return " ".join(parts)


@dataclass
class ExportCheck:
    target: str
    blocks: int = 0
    found: int = 0
    dropped: list[dict[str, Any]] = field(default_factory=list)
    moved: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    missing_chars: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.missing:
            return "warn"
        return "info" if self.moved or self.missing_chars else "ok"

    def to_json(self) -> dict[str, Any]:
        reasons = Counter(d["reason"] for d in self.dropped)
        return {
            "status": self.status,
            "summary": self.summary(),
            "blocks": self.blocks, "found": self.found,
            "dropped": len(self.dropped), "dropped_by_reason": dict(sorted(reasons.items())),
            "moved": len(self.moved), "missing": len(self.missing),
            "missing_chars": self.missing_chars,
            "missing_blocks": self.missing[:_MAX_LISTED],
            "moved_blocks": self.moved[:_MAX_LISTED],
            "dropped_blocks": self.dropped[:_MAX_LISTED],
        }

    def summary(self) -> str:
        s = (f"{self.found}/{self.blocks} text blocks found verbatim; {len(self.dropped)} left "
             f"out on purpose; {len(self.moved)} rearranged")
        if self.missing:
            s += f"; {len(self.missing)} MISSING"
        return s


def check_export(doc: Document, path: Path, target: str,
                 dropped: Sequence[tuple[str, int, str | None]] = ()) -> ExportCheck:
    out = norm(export_text(path))
    res = ExportCheck(target)
    whole_pages = {(pg, why) for why, pg, t in dropped if t is None}
    by_page: dict[int, list[tuple[str, str]]] = {}
    for why, pg, t in dropped:
        if t:
            by_page.setdefault(pg, []).append((why, norm(t)))
    cursor = 0
    src: Counter[str] = Counter()
    explained: Counter[str] = Counter()
    for b in canonical_blocks(doc):
        res.blocks += 1
        key = norm(b.text)
        src.update(key)
        if not key:
            res.found += 1
            continue
        entry = {"page": b.page, "kind": b.kind, "text": _excerpt(b.text)}
        if len(key) <= 4:
            # a marker or a page number is found anywhere: ask the ledger first ("3." would
            # match the heading "3. Results")
            reason = _reason(key, b.page, by_page, whole_pages, exact=True)
            if reason is not None:
                res.dropped.append({**entry, "reason": reason})
                explained.update(key)
                continue
        pos = _find(out, key, cursor)
        if pos < 0 and key.endswith("-"):            # hyphen removed where paragraphs joined
            pos = _find(out, key.rstrip("-"), cursor)
        if pos < 0:
            # a query or other dropped text inside the block (taken out of a paragraph)
            for _, t in by_page.get(b.page, []):
                if t and t in key and key != t:
                    rest = key.replace(t, "")
                    if rest and (pos := _find(out, rest, cursor)) >= 0:
                        explained.update(t)
                        key = rest
                        break
        if pos >= 0:
            res.found += 1
            cursor = pos + len(key)
            continue
        reason = _reason(key, b.page, by_page, whole_pages)
        if reason is not None:
            res.dropped.append({**entry, "reason": reason})
            explained.update(key)
            continue
        absent = [w for w in dict.fromkeys(_WORD.findall(b.text)) if norm(w) not in out]
        if not absent:
            res.moved.append(entry)
            continue
        # a query the layout interleaved word by word with the paragraph beside it
        reason = next((r for r in dict.fromkeys(r for r, _ in by_page.get(b.page, []))
                       if all(norm(w) in "".join(t for rr, t in by_page[b.page] if rr == r)
                              for w in absent)), None)
        if reason is not None:
            res.dropped.append({**entry, "reason": reason, "partly": True})
        else:
            res.missing.append({**entry, "absent_words": absent[:12]})
    # characters: what the export lacks once the blocks left out on purpose are set aside
    lacking = src - Counter(out) - explained
    res.missing_chars = dict(sorted(lacking.items(), key=lambda kv: (-kv[1], kv[0]))[:40])
    return res


def _find(out: str, key: str, cursor: int) -> int:
    """Blocks mostly come in order: look after the previous one first."""
    pos = out.find(key, cursor)
    return pos if pos >= 0 else out.find(key)


def _reason(key: str, page: int, by_page: dict[int, list[tuple[str, str]]],
            whole_pages: set[tuple[int, str]], exact: bool = False) -> str | None:
    for pg, reason in sorted(whole_pages):
        if pg == page:
            return reason
    entries = by_page.get(page, [])
    for reason, t in entries:
        if key == t or (not exact and key in t):
            return reason
    if not exact:
        # a block cut across several dropped pieces, e.g. a table cell holding the end of one
        # author query and the start of the next
        for reason in dict.fromkeys(r for r, _ in entries):
            if key in "".join(t for r, t in entries if r == reason):
                return reason
    return None


def _excerpt(text: str, n: int = 160) -> str:
    t = _WS.sub(" ", text).strip()
    return t if len(t) <= n else t[:n - 1] + "…"
