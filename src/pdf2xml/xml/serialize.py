"""Canonical JSON (Document) → canonical XML, validated against ``resources/schemas/canonical.rng``.

Element map (see the RelaxNG schema for the full contract):
  heading → <h level>, paragraph → <p>, caption → <caption>, code → <code>, formula → <formula>,
  list → <list>/<item>, table → <table>/<row>/<cell>, figure → <figure>, box → <box>.
Inline runs are text, or <r> with only the attributes that differ from the block style.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from lxml import etree

from pdf2xml.model import (
    Block,
    Box,
    Caption,
    Code,
    Document,
    Figure,
    Formula,
    Heading,
    ListBlock,
    ListItem,
    Paragraph,
    ParaProps,
    Run,
    Table,
)

XML_VERSION = "1"


def num(v: float) -> str:
    return (f"{v:.2f}").rstrip("0").rstrip(".") or "0"


def _bool(v: bool) -> str:
    return "true" if v else "false"


def _bbox(b: Sequence[float]) -> str:
    return " ".join(num(v) for v in b)


def _common(el: etree._Element, b: Block) -> None:
    el.set("id", b.id)
    el.set("page", str(b.page))
    el.set("bbox", _bbox(b.bbox))
    if b.style:
        el.set("style", b.style)


def _para(el: etree._Element, p: ParaProps | None) -> None:
    if p is None:
        return
    for attr, val in (
        ("align", p.align), ("indent-first", p.indent_first), ("indent-left", p.indent_left),
        ("space-before", p.space_before), ("space-after", p.space_after),
        ("line-height", p.line_height),
    ):
        if val is not None:
            el.set(attr, val if isinstance(val, str) else num(val))


_RUN_ATTRS = (
    ("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "s"), ("sup", "sup"),
    ("sub", "sub"), ("small_caps", "sc"),
)


def _inline(el: etree._Element, runs: Sequence[Run]) -> None:
    last: etree._Element | None = None
    for r in runs:
        attrs: dict[str, str] = {}
        for field, name in _RUN_ATTRS:
            v = getattr(r, field)
            if v is not None:
                attrs[name] = _bool(v)
        if r.color:
            attrs["color"] = r.color
        if r.font:
            attrs["font"] = r.font
        if r.size is not None:
            attrs["size"] = num(r.size)
        if r.link:
            attrs["href"] = r.link
        if attrs:
            last = etree.SubElement(el, "r", attrs)
            last.text = r.text
        elif last is None:
            el.text = (el.text or "") + r.text
        else:
            last.tail = (last.tail or "") + r.text


def _block(parent: etree._Element, b: Block) -> None:
    if isinstance(b, Heading):
        el = etree.SubElement(parent, "h")
        _common(el, b)
        el.set("level", str(b.level))
        _para(el, b.para)
        _inline(el, b.runs)
    elif isinstance(b, Paragraph):
        el = etree.SubElement(parent, "p")
        _common(el, b)
        _para(el, b.para)
        _inline(el, b.runs)
    elif isinstance(b, Caption):
        el = etree.SubElement(parent, "caption")
        _common(el, b)
        if b.target:
            el.set("target", b.target)
        _para(el, b.para)
        _inline(el, b.runs)
    elif isinstance(b, Code):
        el = etree.SubElement(parent, "code")
        _common(el, b)
        _inline(el, b.runs)
    elif isinstance(b, Formula):
        el = etree.SubElement(parent, "formula")
        _common(el, b)
        el.set("display", _bool(b.display))
        if b.latex:
            etree.SubElement(el, "tex").text = b.latex
        if b.mathml:
            etree.SubElement(el, "mathml").text = b.mathml
        if b.runs:
            _inline(etree.SubElement(el, "text"), b.runs)
    elif isinstance(b, ListBlock):
        el = etree.SubElement(parent, "list")
        _common(el, b)
        el.set("ordered", _bool(b.ordered))
        for item in b.items:
            _block(el, item)
    elif isinstance(b, ListItem):
        el = etree.SubElement(parent, "item")
        _common(el, b)
        if b.marker:
            el.set("marker", b.marker)
        _para(el, b.para)
        _inline(el, b.runs)
        for child in b.children:
            _block(el, child)
    elif isinstance(b, Table):
        el = etree.SubElement(parent, "table")
        _common(el, b)
        el.set("cols", str(b.n_cols))
        if b.caption_ref:
            el.set("caption-ref", b.caption_ref)
        for row in b.rows:
            r_el = etree.SubElement(el, "row")
            for c in row:
                c_el = etree.SubElement(r_el, "cell")
                if c.rowspan != 1:
                    c_el.set("rowspan", str(c.rowspan))
                if c.colspan != 1:
                    c_el.set("colspan", str(c.colspan))
                if c.header:
                    c_el.set("header", "true")
                if c.bbox is not None:
                    c_el.set("bbox", _bbox(c.bbox))
                _inline(c_el, c.runs)
    elif isinstance(b, Figure):
        el = etree.SubElement(parent, "figure")
        _common(el, b)
        if b.image:
            el.set("src", b.image)
        if b.caption_ref:
            el.set("caption-ref", b.caption_ref)
        if b.alt:
            el.set("alt", b.alt)
    elif isinstance(b, Box):
        el = etree.SubElement(parent, "box")
        _common(el, b)
        el.set("role", b.role)
        for child in b.children:
            _block(el, child)
    else:  # pragma: no cover - exhaustive
        raise TypeError(f"unsupported block {type(b).__name__}")


def to_xml(doc: Document) -> etree._Element:
    root = etree.Element("document", version=XML_VERSION)

    m = doc.meta
    meta = etree.SubElement(root, "meta")
    for tag, val in (
        ("source", m.source), ("sha256", m.sha256), ("pages", str(m.pages)),
        ("doc-type", m.doc_type), ("title", m.title), ("lang", m.lang),
        ("tagged", _bool(m.tagged)), ("engine", m.engine), ("style-backend", m.style_backend),
    ):
        if val is not None:
            etree.SubElement(meta, tag).text = val
    if m.warnings:
        ws = etree.SubElement(meta, "warnings")
        for w in m.warnings:
            etree.SubElement(ws, "warning").text = w
    if m.stats:
        st = etree.SubElement(meta, "stats")
        for k in sorted(m.stats):
            etree.SubElement(st, "stat", name=k, value=str(m.stats[k]))

    styles = etree.SubElement(root, "styles")
    for key in sorted(doc.styles):
        s = doc.styles[key]
        el = etree.SubElement(styles, "style", id=key, font=s.font, size=num(s.size),
                              bold=_bool(s.bold), italic=_bool(s.italic), color=s.color,
                              generic=s.generic)
        if s.small_caps:
            el.set("small-caps", "true")
        if s.role:
            el.set("role", s.role)
        if s.level is not None:
            el.set("level", str(s.level))

    pages = etree.SubElement(root, "pages")
    for p in doc.pages:
        etree.SubElement(pages, "page", n=str(p.n), width=num(p.width), height=num(p.height))

    for tag, blocks in (("body", doc.body), ("footnotes", doc.footnotes),
                        ("references", doc.references)):
        el = etree.SubElement(root, tag)
        for b in blocks:
            _block(el, b)

    arts = etree.SubElement(root, "artifacts")
    for a in doc.artifacts:
        el = etree.SubElement(arts, "artifact", kind=a.kind, page=str(a.page), bbox=_bbox(a.bbox))
        el.text = a.text
    return root


def schema_path() -> Path:
    return Path(str(resources.files("pdf2xml") / "resources" / "schemas" / "canonical.rng"))


def validate(root: etree._Element) -> list[str]:
    """Validate against the RelaxNG schema; returns error messages (empty when valid)."""
    rng = etree.RelaxNG(etree.parse(str(schema_path())))
    if rng.validate(root):
        return []
    return [f"line {e.line}: {e.message}" for e in rng.error_log]


def write_xml(root: etree._Element, path: Path) -> None:
    # lxml only indents elements without text, so mixed content is left untouched.
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                    pretty_print=True))
