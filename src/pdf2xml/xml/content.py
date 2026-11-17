"""Flat Content XML: titles, paragraphs and structured tables, with effective styling.

This is a projection of the canonical document, not another PDF interpretation. Containers
are flattened in canonical order. Output contains no block IDs or layout metadata.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from lxml import etree

from pdf2xml.model import (
    Block,
    Box,
    Document,
    Figure,
    Formula,
    Heading,
    ListBlock,
    ListItem,
    Run,
    Table,
)
from pdf2xml.publishing.inline import segments

FILENAME = "42_content.xml"


def _text(parent: etree._Element, text: str) -> None:
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _inline(parent: etree._Element, runs: Sequence[Run], doc: Document,
            style_id: str | None) -> None:
    style = doc.styles.get(style_id or "")
    for run in runs:
        resolved = segments([run], style)
        if not resolved:
            continue
        text, fmt = resolved[0]
        el = parent
        if run.link:
            el = etree.SubElement(el, "ext-link", href=run.link)
        for prop, tag in (("bold", "bold"), ("italic", "italic"),
                          ("underline", "underline"), ("strike", "strike"),
                          ("sc", "sc"), ("mono", "monospace"),
                          ("sup", "sup"), ("sub", "sub")):
            if getattr(fmt, prop):
                el = etree.SubElement(el, tag)
        _text(el, text)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _titles(doc: Document) -> set[int]:
    """Use canonical chapter roles/bookmarks and the recorded title, never font size alone."""
    heads = [b for b in doc.body if isinstance(b, Heading)]
    titles = {id(b) for b in heads if b.role == "chapter"}
    anchors = [e for e in doc.meta.outline or [] if e.kind == "chapter"]
    for entry in anchors:
        match = next((h for h in heads if h.page == entry.page
                      and _key("".join(r.text for r in h.runs)) == _key(entry.title)), None)
        if match is not None:
            titles.add(id(match))
    # A selected range beginning inside a known chapter must not turn its first section
    # into a document title just because older canonical metadata inferred that title.
    if doc.meta.title and (not anchors or titles):
        match = next((h for h in heads if h.role not in ("section", "module")
                      and _key("".join(r.text for r in h.runs)) == _key(doc.meta.title)), None)
        # A book title can precede the chapters. A later section that happens to match
        # inferred metadata must remain a paragraph.
        before_chapters = (match is not None and titles
                           and heads.index(match) < min(i for i, h in enumerate(heads)
                                                        if id(h) in titles))
        if match is not None and (not titles or id(match) in titles or before_chapters):
            titles.add(id(match))
    return titles


def to_content_xml(doc: Document) -> etree._Element:
    root = etree.Element("content", version="1", lang=doc.meta.lang)
    body = etree.SubElement(root, "body")
    titles = _titles(doc)

    def walk(blocks: Sequence[Block]) -> None:
        for b in blocks:
            if isinstance(b, Box):
                walk(b.children)
                continue
            if isinstance(b, ListBlock):
                walk(b.items)
                continue
            tag = "table" if isinstance(b, Table) else "title" if id(b) in titles else "p"
            el = etree.SubElement(body, tag)
            if isinstance(b, Table):
                el.set("cols", str(b.n_cols))
                for row in b.rows:
                    row_el = etree.SubElement(el, "tr")
                    for cell in row:
                        cell_el = etree.SubElement(row_el, "th" if cell.header else "td")
                        if cell.rowspan != 1:
                            cell_el.set("rowspan", str(cell.rowspan))
                        if cell.colspan != 1:
                            cell_el.set("colspan", str(cell.colspan))
                        _inline(cell_el, cell.runs, doc, b.style)
            elif isinstance(b, Figure):
                if b.image:
                    etree.SubElement(el, "inline-graphic", src=b.image, alt=b.alt or "")
                elif b.alt:
                    _text(el, b.alt)
            else:
                if isinstance(b, ListItem) and b.marker:
                    _text(el, b.marker + " ")
                _inline(el, b.runs, doc, b.style)
                if isinstance(b, Formula):
                    if not b.runs and b.latex:
                        _text(el, b.latex)
                    if not b.runs and not b.latex and b.mathml:
                        # Keep the raw evidence as escaped text; do not reinterpret it.
                        _text(el, b.mathml)
                    if b.image:
                        etree.SubElement(el, "inline-graphic", src=b.image,
                                         alt="Source equation")
            if isinstance(b, ListItem):
                walk(b.children)

    for part in (doc.body, doc.footnotes, doc.references):
        walk(part)
    return root


def validate(root: etree._Element) -> list[str]:
    path = resources.files("pdf2xml") / "resources" / "schemas" / "content.rng"
    rng = etree.RelaxNG(etree.parse(str(path)))
    if rng.validate(root):
        return []
    return [f"line {e.line}: {e.message}" for e in rng.error_log]


def write_content(doc: Document, out_dir: Path) -> tuple[Path, list[str]]:
    root = to_content_xml(doc)
    errors = validate(root)
    path = out_dir / FILENAME
    # Indent only structural nodes. Generic pretty_print inserts whitespace between
    # adjacent inline elements, changing e.g. H<sub>2</sub>O or a fully bold paragraph.
    def layout(parent: etree._Element, depth: int) -> None:
        if parent.tag not in {"content", "body", "table", "tr"} or not len(parent):
            return
        parent.text = "\n" + "  " * (depth + 1)
        for i, child in enumerate(parent):
            layout(child, depth + 1)
            child.tail = "\n" + "  " * (depth if i == len(parent) - 1 else depth + 1)

    layout(root, 0)
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
    return path, errors
