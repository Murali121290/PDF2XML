"""BITS 2.2 / JATS 1.4 writer: canonical Document → publishing XML, validated offline.

Body markup is shared: BITS reuses the JATS body model. Only the wrapper and front matter differ:
BITS ``book-part-wrapper`` (one chapter) or ``book``; JATS ``article`` in the Archiving,
Publishing or Authoring tag set.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from lxml import etree

from pdf2xml.model import (
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
    Run,
    Style,
    Table,
)
from pdf2xml.publishing import schemas
from pdf2xml.publishing.inline import HREF, XLINK, LinkContext, emit, emit_tagged, plain_text
from pdf2xml.publishing.matter import IndexEntry, TocEntry, names, runs_text, walk_toc
from pdf2xml.publishing.profile import DateParts, Metadata, Person, Profile
from pdf2xml.publishing.semantic import (
    BoxNode,
    Epigraph,
    FloatObj,
    Footnote,
    Matter,
    Node,
    Query,
    Reference,
    Section,
    SemanticDoc,
    Unit,
    build,
)

MML = "http://www.w3.org/1998/Math/MathML"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
NSMAP = {"xlink": XLINK, "mml": MML}
_TITLE = frozenset({"bold", "italic", "sc", "color"})   # typographic in headings and captions
_FILES = {"bits": "40_bits.xml", "jats": "41_jats.xml"}

Target = Literal["bits", "jats"]


@dataclass
class ExportResult:
    target: Target
    schema: str
    path: Path
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _el(parent: etree._Element, tag: str, text: str | None = None, **attrs: str) -> etree._Element:
    e = etree.SubElement(parent, tag, {k.replace("_", "-"): v for k, v in attrs.items()})
    if text is not None:
        e.text = text
    return e


def _list_type(lb: ListBlock) -> str:
    markers = [(it.marker or "").strip("().[] ") for it in lb.items if it.marker]
    if not markers:
        return "order" if lb.ordered else "bullet"
    if all(m.isdigit() for m in markers):
        return "order"
    if all(re.fullmatch(r"[ivxlc]+", m) for m in markers) and (len(markers) > 1 or markers[0]
                                                                != "c"):
        return "roman-lower"
    if all(re.fullmatch(r"[IVXLC]+", m) for m in markers) and len(markers) > 1:
        return "roman-upper"
    if all(re.fullmatch(r"[a-z]", m) for m in markers):
        return "alpha-lower"
    if all(re.fullmatch(r"[A-Z]", m) for m in markers):
        return "alpha-upper"
    if all(not any(c.isalnum() for c in m) or m.startswith("(cid:") for m in markers):
        return "bullet"
    return "simple"


class _Writer:
    def __init__(self, doc: Document, sem: SemanticDoc, profile: Profile, meta: Metadata,
                 target: Target, schema: schemas.Schema) -> None:
        self.doc = doc
        self.sem = sem
        self.p = profile
        self.meta = meta
        self.target = target
        self.schema = schema
        self.ctx = LinkContext(targets=sem.targets, references=sem.references,
                               citations=profile.citation_links, callouts=profile.callout_links,
                               ext_links=profile.ext_links,
                               keep_visual=profile.styling == "keep-visual",
                               notes=sem.note_ids)
        self.stats = {"tables": 0, "figures": 0, "lists": 0, "formulas": 0, "page_markers": 0}
        # pages whose marker waits for the next text (the page starts with a table, a box, …)
        self.pending: list[int] = []
        self.page_by_folio: dict[str, int] = {}          # printed page number → PDF page
        for pg, f in sorted(sem.folios.items()):
            self.page_by_folio.setdefault(f.lower(), pg)

    # -------------------------------------------------------------- page markers
    def _page_el(self, page: int) -> etree._Element:
        self.stats["page_markers"] += 1
        if self.p.page_markers == "pi":
            return etree.ProcessingInstruction("page-break",
                                               self.sem.folios.get(page, str(page)))
        return etree.Element("target", {"id": self.sem.page_ids[page], "target-type": "page"})

    def _hold(self, key: object) -> None:
        """Queue the page markers of a node without text of its own (a list, float, box, …);
        they go at the start of the next text written."""
        self.pending.extend(pg for _, pg in self.sem.page_marks.get(id(key), []))

    def _take(self, key: object | None) -> list[tuple[int, etree._Element]]:
        marks = [(0, pg) for pg in self.pending]
        self.pending = []
        if key is not None:
            marks.extend(self.sem.page_marks.get(id(key), []))
        return [(off, self._page_el(pg)) for off, pg in marks]

    # -------------------------------------------------------------- inline
    def _style(self, key: str | None) -> Style | None:
        return self.sem.styles.get(key or "")

    def _inline(self, parent: etree._Element, runs: Sequence[Run], style: str | None, *,
                title: bool = False, links: bool = True, drop: frozenset[str] = frozenset(),
                key: object | None = None, flush: bool = True) -> etree._Element:
        """``key``: the semantic node whose page markers go into this text. ``flush=False``
        for elements that cannot hold a marker (``disp-formula``)."""
        marks = self._take(key) if flush else []
        emit(parent, runs, self._style(style), self.ctx, suppress=_TITLE if title else frozenset(),
             drop=drop, citations=links and not title, callouts=links and not title, links=links,
             marks=marks)
        return parent

    # -------------------------------------------------------------- body
    def _nodes(self, parent: etree._Element, nodes: Sequence[Node]) -> None:
        for n in nodes:
            self._node(parent, n)

    def _node(self, parent: etree._Element, n: Node) -> None:
        if isinstance(n, Paragraph | Caption):
            self._inline(_el(parent, "p"), n.runs, n.style, key=n)
            return
        self._hold(n)
        if isinstance(n, ListBlock):
            self._list(parent, n)
        elif isinstance(n, FloatObj):
            self._table(parent, n) if n.kind == "table" else self._fig(parent, n)
        elif isinstance(n, Code):
            _el(parent, "code", "".join(r.text for r in n.runs))
        elif isinstance(n, Formula):
            self.stats["formulas"] += 1
            f = _el(parent, "disp-formula", id=self.sem.node_ids[id(n)])
            if n.latex:
                _el(f, "tex-math", n.latex)
            else:
                self._inline(f, n.runs, n.style, links=False, flush=False)
        elif isinstance(n, Box):
            box = _el(parent, "boxed-text", content_type=n.role)
            if id(n) in self.sem.node_ids:
                box.set("id", self.sem.node_ids[id(n)])
            for ch in n.children:
                if isinstance(ch, Heading):
                    self._inline(_el(_el(box, "p"), "bold"), ch.runs, ch.style, title=True)
                elif isinstance(ch, Paragraph | Caption | ListBlock | Code | Formula | Box):
                    self._node(box, ch)
        elif isinstance(n, Epigraph):
            dq = _el(parent, "disp-quote", content_type="epigraph")
            self._inline(_el(dq, "p"), n.quote, n.style, title=True)
            if n.attrib:
                self._inline(_el(dq, "attrib"), n.attrib, n.style, title=True)
        elif isinstance(n, BoxNode):
            self.stats["boxes"] = self.stats.get("boxes", 0) + 1
            box = _el(parent, "boxed-text", id=n.id, content_type=n.kind)
            if n.label:
                _el(box, "label", n.label)
            if n.title:
                self._inline(_el(_el(box, "caption"), "title"), n.title, n.title_style, title=True,
                             links=False)
            self._nodes(box, n.nodes)
            for sub in n.children:
                self._sec(box, sub)
        elif isinstance(n, Query):
            self._query(parent, n)

    def _query(self, parent: etree._Element, q: Query) -> None:
        mode = self.p.queries.mode
        if mode == "comment":
            parent.append(etree.Comment(" " + q.text.replace("--", "- -").rstrip("-") + " "))
        elif mode == "pi":
            parent.append(etree.ProcessingInstruction("query", q.text.replace("?>", "? >")))
        elif mode == "keep":
            _el(parent, "p", q.text, content_type="author-query")

    def _list(self, parent: etree._Element, lb: ListBlock) -> None:
        self.stats["lists"] += 1
        ltype = _list_type(lb)
        el = _el(parent, "list", list_type=ltype)
        mode = self.p.list_labels
        labels = mode == "keep" or (mode == "auto" and ltype == "simple")
        for it in lb.items:
            li = _el(el, "list-item")
            if labels and it.marker:
                _el(li, "label", it.marker)
            self._inline(_el(li, "p"), it.runs, it.style, key=it)
            for ch in it.children:
                if isinstance(ch, ListBlock):
                    self._list(li, ch)
                elif isinstance(ch, ListItem | Paragraph | Caption):
                    self._inline(_el(li, "p"), ch.runs, ch.style, key=ch)

    def _caption(self, parent: etree._Element, f: FloatObj) -> None:
        if f.label:
            _el(parent, "label", f.label)
        if f.title and plain_text(f.title):
            cap = _el(parent, "caption")
            self._inline(_el(cap, "title"), f.title, f.title_style, title=True)

    def _table(self, parent: etree._Element, f: FloatObj) -> None:
        t = f.block
        assert isinstance(t, Table)
        self.stats["tables"] += 1
        tw = _el(parent, "table-wrap", id=f.id)
        self._caption(tw, f)
        table = _el(tw, "table")
        rows = [r for r in t.rows if r]
        n_head = 0
        while n_head < len(rows) and all(c.header for c in rows[n_head]):
            n_head += 1
        if n_head == len(rows):
            n_head = 0
        sections = [("thead", rows[:n_head]), ("tbody", rows[n_head:])]
        for tag, group in sections:
            if not group:
                continue
            g = _el(table, tag)
            for row in group:
                tr = _el(g, "tr")
                for c in row:
                    cell = _el(tr, "th" if c.header else "td")
                    if c.rowspan != 1:
                        cell.set("rowspan", str(c.rowspan))
                    if c.colspan != 1:
                        cell.set("colspan", str(c.colspan))
                    self._inline(cell, c.runs, "body",
                                 drop=frozenset({"bold"}) if c.header else frozenset())
        if f.notes:
            foot = _el(tw, "table-wrap-foot")
            for runs in f.notes:
                self._inline(_el(foot, "p"), runs, "body")

    def _fig(self, parent: etree._Element, f: FloatObj) -> None:
        fig_b = f.block
        assert isinstance(fig_b, Figure)
        self.stats["figures"] += 1
        fig = _el(parent, "fig", id=f.id)
        self._caption(fig, f)
        if fig_b.alt:
            _el(fig, "alt-text", fig_b.alt)
        if fig_b.image:
            ext = Path(fig_b.image).suffix.lstrip(".").lower() or "png"
            g = _el(fig, "graphic", mimetype="image", mime_subtype="jpeg" if ext == "jpg" else ext)
            g.set(HREF, fig_b.image)
        for runs in f.notes:
            self._inline(_el(fig, "attrib"), runs, "body")

    def _sec(self, parent: etree._Element, s: Section) -> None:
        el = _el(parent, "sec", id=s.id)
        if s.sec_type:
            el.set("sec-type", s.sec_type)
        if self.p.sections.disp_level:
            el.set("disp-level", self.p.sections.disp_level.format(n=s.depth))
        if s.label:
            _el(el, "label", s.label)
        self._inline(_el(el, "title"), s.title, s.heading.style, title=True, links=False,
                     key=s.heading)
        self._nodes(el, s.nodes)
        for ch in s.children:
            self._sec(el, ch)

    def _body(self, parent: etree._Element, tag: str = "body",
              preamble: Sequence[Node] | None = None,
              sections: Sequence[Section] | None = None) -> None:
        preamble = self.sem.preamble if preamble is None else preamble
        sections = self.sem.sections if sections is None else sections
        if not preamble and not sections:
            return
        body = _el(parent, tag)
        self._nodes(body, preamble)
        for s in sections:
            self._sec(body, s)

    def _back_items(self, back: etree._Element, footnotes: Sequence[Footnote],
                    references: Sequence[Reference], ref_title: Sequence[Run],
                    matters: Sequence[Matter]) -> None:
        self.pending = []           # pages that held nothing but dropped content
        if footnotes:
            self._fn_group(back, footnotes)
        if references:
            self._ref_list(back, references, ref_title)
        self._matters(back, matters, "back")

    def _fn_group(self, parent: etree._Element, fns: Sequence[Footnote],
                  title: Sequence[Run] | None = None, *, key: object | None = None,
                  gid: str | None = None, label: str | None = None) -> None:
        g = _el(parent, "fn-group")
        if gid:
            g.set("id", gid)
        if label:
            _el(g, "label", label)
        if title:
            self._inline(_el(g, "title"), title, None, title=True, links=False, key=key)
        elif key is not None:
            self._hold(key)
        for fn in fns:
            e = _el(g, "fn", id=fn.id)
            if fn.label:
                _el(e, "label", fn.label)
            self._inline(_el(e, "p"), fn.runs, "body", key=fn)

    def _ref_list(self, parent: etree._Element, refs: Sequence[Reference],
                  title: Sequence[Run], content_type: str | None = None, *,
                  key: object | None = None, rid: str | None = None) -> None:
        rl = _el(parent, "ref-list")
        if rid:
            rl.set("id", rid)
        if content_type:
            rl.set("content-type", content_type)
        if title:
            self._inline(_el(rl, "title"), title, None, title=True, links=False, key=key)
        elif key is not None:
            self._hold(key)
        for r in refs:
            ref = _el(rl, "ref", id=r.id)
            if r.label:
                _el(ref, "label", r.label)
            mc = _el(ref, "mixed-citation")
            if r.pub_type:
                mc.set("publication-type", r.pub_type)
            for _, el in self._take(r):
                mc.append(el)
            if r.parsed is not None:
                emit_tagged(mc, r.runs, self._style("body"), self.ctx, r.parsed.spans)
            else:
                emit(mc, r.runs, self._style("body"), self.ctx, citations=False,
                     callouts=False)
            for q in r.queries:
                self._query(rl, q)

    def _back(self, parent: etree._Element, tag: str = "back") -> None:
        sem = self.sem
        back = [m for m in sem.back if isinstance(m, Matter)]
        if self.target == "jats":
            back = [m for m in back if m.kind != "toc"]
        if sem.footnotes or sem.references or back:
            self._back_items(_el(parent, tag), sem.footnotes, sem.references, sem.ref_title,
                             back)

    # -------------------------------------------------------------- front and back matter
    def _mtitle(self, parent: etree._Element, m: Matter, tag: str = "title") -> None:
        """Label and title of a front/back-matter part (its page marker goes in the title)."""
        if m.label:
            _el(parent, "label", m.label)
        if m.title:
            self._inline(_el(parent, tag), m.title, m.title_style, title=True, links=False,
                         key=m)
        else:
            self._hold(m)

    def _items(self, parent: etree._Element, items: Sequence[object]) -> None:
        """Untitled front matter as printed: headings on a title page become paragraphs."""
        for x in items:
            if isinstance(x, Heading):
                self._inline(_el(parent, "p"), x.runs, x.style, key=x)
            elif not isinstance(x, Matter):
                self._node(parent, x)  # type: ignore[arg-type]

    def _content(self, parent: etree._Element, m: Matter) -> None:
        self._items(parent, m.items)
        self._nodes(parent, m.nodes)
        for s in m.sections:
            self._sec(parent, s)

    def _matters(self, parent: etree._Element, ms: Sequence[Matter], zone: str) -> None:
        """``zone``: "front" (BITS front-matter), "back" (JATS back, BITS book-part back) or
        "book-back" (BITS book-back); each allows different elements."""
        k = 0
        while k < len(ms):
            if ms[k].kind == "app" and zone == "back":
                run: list[Matter] = []
                while k < len(ms) and ms[k].kind == "app":
                    run.append(ms[k])
                    k += 1
                self._app_group(parent, run)
                continue
            self._matter(parent, ms[k], zone)
            k += 1

    def _matter(self, parent: etree._Element, m: Matter, zone: str) -> None:
        self.stats["matter_" + m.kind.replace("-", "_")] = (
            self.stats.get("matter_" + m.kind.replace("-", "_"), 0) + 1)
        bits = self.target == "bits"
        k = m.kind
        if k == "toc":
            if bits:
                self._toc(parent, m)
            else:
                self._hold(m)
                for te in walk_toc(m.toc):
                    self._hold(te)
        elif k in ("ack", "bio"):
            el = _el(parent, k, id=m.id)
            self._mtitle(el, m)
            self._content(el, m)
        elif k == "glossary":
            self._glossary(parent, m)
        elif k == "notes":
            self._notes(parent, m)
        elif k in ("refs", "further"):
            self._ref_list(parent, m.references, m.title,
                           "further-reading" if k == "further" else None, key=m, rid=m.id)
        elif k == "index" and bits and m.index and zone != "front":
            self._index(parent, m)
        elif k == "app" and zone == "book-back":
            self._book_app(parent, m)
        elif k == "app" and zone == "back":
            self._app_group(parent, [m])
        elif zone == "front":
            tag = k if k in ("dedication", "foreword", "preface") else "front-matter-part"
            self._named_part(parent, tag, m)
        elif zone == "book-back":
            if k == "dedication":
                self._named_part(parent, "dedication", m)
            else:
                self._named_part(parent, "book-part", m, body_tag="body")
        else:
            el = _el(parent, "sec", id=m.id)
            if k in ("index", "preface", "foreword", "dedication"):
                el.set("sec-type", k)
            self._mtitle(el, m)
            for d in m.index:
                for ie in d.entries:
                    self._hold_index(ie)
            self._content(el, m)

    def _named_part(self, parent: etree._Element, tag: str, m: Matter,
                    body_tag: str = "named-book-part-body") -> None:
        """preface, foreword, dedication, front-matter-part (and book-part in book-back):
        book-part-meta with the title, then the text."""
        el = _el(parent, tag, id=m.id)
        part_type = m.part_type or (m.kind if tag in ("front-matter-part", "book-part") else None)
        if part_type:
            el.set("book-part-type", part_type)
        if m.title or m.label:
            self._mtitle(_el(_el(el, "book-part-meta"), "title-group"), m)
        else:
            self._hold(m)
        body = etree.Element(body_tag)
        self._content(body, m)
        if any(isinstance(c.tag, str) for c in body):
            el.append(body)

    def _toc(self, parent: etree._Element, m: Matter) -> None:
        el = _el(parent, "toc", id=m.id)
        self._mtitle(_el(el, "toc-title-group"), m)
        for e in m.toc:
            self._toc_entry(el, e)
        self.stats["toc_entries"] = len(walk_toc(m.toc))

    def _toc_entry(self, parent: etree._Element, e: TocEntry) -> None:
        """label, title, the authors of the chapter, and a nav-pointer holding the printed page
        number and pointing at the chapter (or at the page)."""
        el = _el(parent, "toc-entry")
        if e.label:
            _el(el, "label", e.label)
        if e.title:
            self._inline(_el(el, "title"), e.title, e.style, title=True, links=False, key=e)
        else:
            self._hold(e)
        if e.contributors:
            cg = _el(el, "contrib-group")
            for c in e.contributors:
                people = names(c)
                for given, surname in people:
                    n = _el(_el(cg, "contrib", contrib_type="author"), "name")
                    _el(n, "surname", surname)
                    if given:
                        _el(n, "given-names", given)
                if not people:
                    _el(_el(cg, "contrib"), "string-name", c)
        if e.folio or e.target:
            np = _el(el, "nav-pointer", e.folio)
            if e.target:
                np.set("rid", e.target)
        for child in e.children:
            self._toc_entry(el, child)

    def _glossary(self, parent: etree._Element, m: Matter) -> None:
        el = _el(parent, "glossary", id=m.id)
        self._mtitle(el, m)
        self._items(el, m.items)
        self._nodes(el, m.nodes)
        if m.terms:
            dl = _el(el, "def-list")
            for g in m.terms:
                di = _el(dl, "def-item")
                self._inline(_el(di, "term"), g.term, "body", key=g, drop=frozenset({"bold"}))
                d = _el(di, "def")
                for runs in g.defs or [[]]:
                    self._inline(_el(d, "p"), runs, "body")
        for s in m.sections:
            self._glossary_sec(el, s)

    def _glossary_sec(self, parent: etree._Element, s: Section) -> None:
        """A sub-heading inside a glossary: a nested glossary (glossary has no sec)."""
        g = _el(parent, "glossary", id=s.id)
        if s.label:
            _el(g, "label", s.label)
        self._inline(_el(g, "title"), s.title, s.heading.style, title=True, links=False,
                     key=s.heading)
        self._nodes(g, s.nodes)
        for c in s.children:
            self._glossary_sec(g, c)

    def _notes(self, parent: etree._Element, m: Matter) -> None:
        """Endnotes: one fn-group, or notes holding an fn-group per chapter."""
        groups = [(t, fns) for t, fns in m.notes if fns]
        if len(groups) == 1 and not groups[0][0] and not m.nodes and not m.items:
            self._fn_group(parent, groups[0][1], m.title, key=m, gid=m.id, label=m.label)
            return
        el = _el(parent, "notes", id=m.id, notes_type="endnotes")
        self._mtitle(el, m)
        self._items(el, m.items)
        self._nodes(el, m.nodes)
        for t, fns in groups:
            self._fn_group(el, fns, t or None)

    def _index(self, parent: etree._Element, m: Matter) -> None:
        """Back-of-book index: index-entry (term, nav-pointer per page number, see / see also,
        sub-entries), in index-div letter groups."""
        el = _el(parent, "index", id=m.id)
        if m.title or m.label:
            self._mtitle(_el(el, "index-title-group"), m)
        else:
            self._hold(m)
        for d in m.index:
            target = el
            if d.title and len(m.index) > 1:
                target = _el(el, "index-div")
                _el(_el(target, "index-title-group"), "title", d.title)
            for e in d.entries:
                self._index_entry(target, e)
        self.stats["index_entries"] = self.stats.get("index_entries", 0) + sum(
            len(d.entries) for d in m.index)

    def _index_entry(self, parent: etree._Element, e: IndexEntry) -> None:
        el = _el(parent, "index-entry")
        self._inline(_el(el, "term"), e.term, "body", key=e, links=False)
        for loc in e.locators:
            np = _el(el, "nav-pointer", loc)
            m = re.match(r"\d+|[ivxlcdm]+", loc.lower())
            pg = self.page_by_folio.get(m.group(0)) if m else None
            if pg is not None and pg in self.sem.page_ids:
                np.set("rid", self.sem.page_ids[pg])
        for s in e.see:
            _el(el, "see-entry", s)
        for s in e.see_also:
            _el(el, "see-also-entry", s)
        for c in e.children:
            self._index_entry(el, c)

    def _hold_index(self, e: IndexEntry) -> None:
        self._hold(e)
        for c in e.children:
            self._hold_index(c)

    def _app(self, parent: etree._Element, m: Matter) -> None:
        el = _el(parent, "app", id=m.id)
        self._mtitle(el, m)
        self._content(el, m)

    def _app_group(self, parent: etree._Element, apps: Sequence[Matter]) -> None:
        if len(apps) == 1 and apps[0].children:
            g = apps[0]
            el = _el(parent, "app-group", id=g.id)
            self._mtitle(el, g)
            self._items(el, g.items)
            self._nodes(el, g.nodes)
            for c in g.children:
                self._app(el, c)
            return
        el = _el(parent, "app-group")
        for a in apps:
            if a.children:
                self._hold(a)
                for c in a.children:
                    self._app(el, c)
            else:
                self._app(el, a)

    def _book_app(self, parent: etree._Element, m: Matter) -> None:
        if m.children:
            g = _el(parent, "book-app-group", id=m.id)
            if m.title or m.label:
                self._mtitle(_el(_el(g, "book-part-meta"), "title-group"), m)
            else:
                self._hold(m)
            self._nodes(g, m.nodes)
            for s in m.sections:
                self._sec(g, s)
            for c in m.children:
                self._book_app(g, c)
            return
        el = _el(parent, "book-app", id=m.id, book_part_type="appendix")
        self._mtitle(_el(_el(el, "book-part-meta"), "title-group"), m)
        if m.items or m.nodes or m.sections:
            self._content(_el(el, "body"), m)

    # -------------------------------------------------------------- chapters of a book
    def _unit(self, parent: etree._Element, u: Unit) -> None:
        o = self.p.bits
        bp = _el(parent, "book-part", id=u.id,
                 book_part_type="part" if u.kind == "part" else o.book_part_type)
        bpm = _el(bp, "book-part-meta")
        tg = _el(bpm, "title-group")
        if u.label:
            _el(tg, "label", u.label)
        if u.title:
            self._inline(_el(tg, "title"), u.title, u.title_style, title=True, links=False,
                         key=u)
        else:
            self._hold(u)
        self._contribs(bpm, [Person(given=g or None, surname=s) for g, s in u.contributors])
        f, lp = self.sem.folios.get(u.page), self.sem.folios.get(max(u.last_page, u.page))
        if f:
            _el(bpm, "fpage", f)
            if lp:
                _el(bpm, "lpage", lp)
        self._abstract_kwds(bpm, u.abstract, u.abstract_title, u.keywords)
        if u.front:
            self._matters(_el(bp, "front-matter"), u.front, "front")
        self.ctx.scope(u.references, u.note_ids)
        if u.preamble or u.sections or u.children:
            body = _el(bp, "body")
            self._nodes(body, u.preamble)
            for s in u.sections:
                self._sec(body, s)
            for c in u.children:
                self._unit(body, c)
            self.ctx.scope(u.references, u.note_ids)
        if u.footnotes or u.references or u.back:
            self._back_items(_el(bp, "back"), u.footnotes, u.references, u.ref_title, u.back)

    # -------------------------------------------------------------- metadata
    def _authors(self) -> list[Person]:
        """Contributors from the metadata file, else the byline detected under the title."""
        return list(self.meta.part.contributors) or [
            Person(given=g or None, surname=s) for g, s in self.sem.contributors]

    def _contribs(self, parent: etree._Element, people: Sequence[Person]) -> None:
        if not people:
            return
        cg = _el(parent, "contrib-group")
        for p in people:
            c = _el(cg, "contrib", contrib_type=p.role)
            if p.orcid:
                _el(c, "contrib-id", p.orcid, contrib_id_type="orcid")
            if p.surname or p.given:
                n = _el(c, "name")
                if p.surname:
                    _el(n, "surname", p.surname)
                if p.given:
                    _el(n, "given-names", p.given)
            elif p.name:
                _el(c, "collab", p.name)
            if p.aff:
                _el(c, "aff", p.aff)
            if p.email:
                _el(c, "email", p.email)

    def _date(self, parent: etree._Element, d: DateParts | None) -> None:
        if d is None or not (d.year or d.month or d.day):
            return
        pd = _el(parent, "pub-date", date_type="pub")
        if d.day:
            _el(pd, "day", f"{d.day:02d}")
        if d.month:
            _el(pd, "month", f"{d.month:02d}")
        if d.year:
            _el(pd, "year", str(d.year))

    def _title_group(self, parent: etree._Element, title_tag: str, label: bool) -> None:
        tg = _el(parent, "title-group")
        if label and self.sem.label:
            _el(tg, "label", self.sem.label)
        t = _el(tg, title_tag)
        if self.sem.title:
            self._inline(t, self.sem.title, self.sem.title_style, title=True, links=False)
        else:
            t.text = Path(self.doc.meta.source).stem
        if self.sem.subtitle:
            _el(tg, "subtitle", self.sem.subtitle)

    def _pages(self, parent: etree._Element) -> None:
        """First/last page from the metadata file, else the printed page numbers."""
        mp = self.meta.part
        fpage, lpage = mp.fpage, mp.lpage
        folios = self.sem.folios
        if not fpage and folios:
            fpage, lpage = folios[min(folios)], folios[max(folios)]
        if fpage:
            _el(parent, "fpage", fpage)
            if lpage:
                _el(parent, "lpage", lpage)

    def _permissions(self, parent: etree._Element) -> None:
        mp = self.meta.part
        if not (mp.copyright or mp.copyright_year or mp.license_url):
            return
        pm = _el(parent, "permissions")
        if mp.copyright:
            _el(pm, "copyright-statement", mp.copyright)
        if mp.copyright_year:
            _el(pm, "copyright-year", str(mp.copyright_year))
        if mp.license_url:
            lic = _el(pm, "license")
            lic.set(HREF, mp.license_url)
            _el(lic, "license-p", mp.license_url)

    def _abstract_kwds(self, parent: etree._Element, abstract: Sequence[Paragraph] | None = None,
                       abstract_title: Sequence[Run] | None = None,
                       keywords: Sequence[tuple[str, list[Run], list[str]]] | None = None
                       ) -> None:
        sem = self.sem
        abstract = sem.abstract if abstract is None else abstract
        abstract_title = sem.abstract_title if abstract_title is None else abstract_title
        keywords = sem.keywords if keywords is None else keywords
        if abstract:
            ab = _el(parent, "abstract")
            if abstract_title:
                self._inline(_el(ab, "title"), abstract_title, None, title=True, links=False)
            for para in abstract:
                self._inline(_el(ab, "p"), para.runs, para.style)
        for kind, title, kws in keywords:
            kg = _el(parent, "kwd-group", kwd_group_type=kind)
            if title:
                self._inline(_el(kg, "title"), title, None, title=True, links=False)
            for k in kws:
                _el(kg, "kwd", k)

    # -------------------------------------------------------------- roots
    def _root(self, tag: str) -> etree._Element:
        root = etree.Element(tag, nsmap=NSMAP)
        lang = self.meta.lang or self.doc.meta.lang
        if lang:
            root.set(XML_LANG, lang)
        return root

    def article(self) -> etree._Element:
        o = self.p.jats
        art = self._root("article")
        art.set("article-type", o.article_type)
        art.set("dtd-version", o.version)
        front = _el(art, "front")
        mj, mp = self.meta.journal, self.meta.part
        if o.tag_set != "authoring":
            jm = etree.Element("journal-meta")
            if mj.id:
                _el(jm, "journal-id", mj.id, journal_id_type="publisher-id")
            if mj.nlm_ta:
                _el(jm, "journal-id", mj.nlm_ta, journal_id_type="nlm-ta")
            if mj.title:
                _el(_el(jm, "journal-title-group"), "journal-title", mj.title)
            if mj.issn:
                _el(jm, "issn", mj.issn, publication_format="print")
            if mj.eissn:
                _el(jm, "issn", mj.eissn, publication_format="electronic")
            if mj.publisher:
                _el(_el(jm, "publisher"), "publisher-name", mj.publisher)
            if len(jm) or o.tag_set == "publishing":
                front.append(jm)
        am = _el(front, "article-meta")
        if o.tag_set != "authoring":
            if mp.doi:
                _el(am, "article-id", mp.doi, pub_id_type="doi")
            if mp.id:
                _el(am, "article-id", mp.id, pub_id_type="publisher-id")
        self._title_group(am, "article-title", label=False)
        self._contribs(am, self._authors())
        if o.tag_set != "authoring":
            self._date(am, mp.pub_date)
            if mp.volume:
                _el(am, "volume", mp.volume)
            if mp.issue:
                _el(am, "issue", mp.issue)
            self._pages(am)
        self._permissions(am)
        self._abstract_kwds(am)
        self._body(art)
        self._back(art)
        return art

    def book_part_wrapper(self) -> etree._Element:
        o = self.p.bits
        root = self._root("book-part-wrapper")
        root.set("dtd-version", o.version)
        self._book_meta(root)
        mp = self.meta.part
        chapter = re.sub(r"[^\w.-]", "", str(mp.number or mp.label or "")) or "1"
        part = _el(root, "book-part", book_part_type=o.book_part_type)
        pid = re.sub(r"[^\w.-]", "_", self.p.ids.part.format(chapter=chapter, n=1))
        part.set("id", pid if re.match(r"[A-Za-z_]", pid) else "id" + pid)
        bpm = _el(part, "book-part-meta")
        if mp.doi:
            _el(bpm, "book-part-id", mp.doi, book_part_id_type="doi")
        if mp.id:
            _el(bpm, "book-part-id", mp.id, book_part_id_type="publisher-id")
        self._title_group(bpm, "title", label=True)
        self._contribs(bpm, self._authors())
        self._date(bpm, mp.pub_date)
        self._pages(bpm)
        self._permissions(bpm)
        self._abstract_kwds(bpm)
        if self.sem.front:
            self._matters(_el(part, "front-matter"), self.sem.front, "front")
        self._body(part)
        self._back(part)
        return root

    def book(self) -> etree._Element:
        """A whole book: front-matter (title and copyright pages, contents, preface …),
        book-body (a book-part per chapter or part) and book-back (appendices, glossary,
        index, endnotes …)."""
        o = self.p.bits
        sem = self.sem
        root = self._root("book")
        root.set("dtd-version", o.version)
        self._book_meta(root)
        if sem.front:
            self.ctx.scope([], {})
            self._matters(_el(root, "front-matter"), sem.front, "front")
        body = _el(root, "book-body")
        for u in sem.units:
            self._unit(body, u)
        if not len(body):
            _el(_el(body, "book-part", book_part_type=o.book_part_type), "book-part-meta")
        if sem.back or sem.footnotes:
            bb = _el(root, "book-back")
            self.ctx.scope([], {})
            for x in sem.back:
                if isinstance(x, Unit):
                    self._unit(bb, x)
                    self.ctx.scope([], {})
                else:
                    self._matter(bb, x, "book-back")
            if sem.footnotes:
                self.pending = []
                self._fn_group(bb, sem.footnotes)
        return root

    def _book_meta(self, parent: etree._Element) -> None:
        """Metadata file first; for a whole book, what the title and copyright pages say."""
        mb = self.meta.book
        sem = self.sem
        imp = sem.imprint if sem.book else None
        bm = _el(parent, "book-meta")
        if mb.doi:
            _el(bm, "book-id", mb.doi, book_id_type="doi")
        title = mb.title
        if not title and sem.book:
            title = (" ".join(runs_text(sem.book_title).split()) or sem.title_text
                     or self.doc.meta.title)
        if title:
            tg = _el(bm, "book-title-group")
            _el(tg, "book-title", title)
            subtitle = mb.subtitle or (sem.book_subtitle if sem.book and not mb.title else None)
            if subtitle:
                _el(tg, "subtitle", subtitle)
        people = list(mb.contributors) or ([Person(given=g or None, surname=s, role=r)
                                            for g, s, r in sem.book_contributors]
                                           if sem.book else [])
        self._contribs(bm, people)
        date = mb.pub_date or (DateParts(year=imp.year) if imp and imp.year else None)
        self._date(bm, date)
        if mb.isbn or mb.eisbn:
            if mb.isbn:
                _el(bm, "isbn", mb.isbn, publication_format="print")
            if mb.eisbn:
                _el(bm, "isbn", mb.eisbn, publication_format="electronic")
        elif imp:
            for i in imp.isbns:
                e = _el(bm, "isbn", i.value, publication_format=i.fmt)
                if i.kind:
                    e.set("content-type", i.kind)
        publisher = mb.publisher or (imp.publisher if imp else None)
        if publisher:
            pub = _el(bm, "publisher")
            _el(pub, "publisher-name", publisher)
            loc = mb.publisher_loc or (imp.publisher_loc if imp and not mb.publisher else None)
            if loc:
                _el(pub, "publisher-loc", loc)
        edition = mb.edition or (imp.edition if imp else None)
        if edition:
            _el(bm, "edition", edition)
        if imp and (imp.copyright or imp.copyright_year):
            pm = _el(bm, "permissions")
            if imp.copyright:
                _el(pm, "copyright-statement", imp.copyright)
            if imp.copyright_year:
                _el(pm, "copyright-year", str(imp.copyright_year))


# ---------------------------------------------------------------- serialisation

@cache
def _mixed_tags(key: str) -> frozenset[str]:
    """Elements whose content may contain text; their inside is never re-indented."""
    return frozenset(e.name for e in schemas._dtd(key).iterelements() if e.type == "mixed")


def _indent(el: etree._Element, mixed: frozenset[str], level: int = 0) -> None:
    kids = list(el)
    if not kids or el.tag in mixed or (el.text and el.text.strip()):
        return
    if any(c.tail and c.tail.strip() for c in kids):
        return
    pad = "\n" + "  " * (level + 1)
    el.text = pad
    for c in kids:
        c.tail = pad
        if isinstance(c.tag, str):
            _indent(c, mixed, level + 1)
    kids[-1].tail = "\n" + "  " * level


def _doctype(setting: str, schema: schemas.Schema, root: str) -> str | None:
    if setting == "official":
        return schema.doctype(root)
    if setting == "none":
        return None
    if not setting.lstrip().startswith("<!DOCTYPE"):
        raise ValueError(f"doctype must be 'official', 'none' or a full <!DOCTYPE …>: {setting!r}")
    return setting.strip()


def schema_key(target: Target, profile: Profile) -> str:
    return "bits" if target == "bits" else f"jats-{profile.jats.tag_set}"


def export(doc: Document, out_dir: Path, target: Target, profile: Profile, meta: Metadata,
           path: Path | None = None) -> ExportResult:
    key = schema_key(target, profile)
    schema = schemas.SCHEMAS[key]
    book: bool | None = False
    if target == "bits":
        book = {"book": True, "book-part-wrapper": False}.get(profile.bits.root)
    sem = build(doc, profile, meta, book=book)
    w = _Writer(doc, sem, profile, meta, target, schema)
    if target == "jats":
        root = w.article()
    elif sem.book:
        root = w.book()
    else:
        root = w.book_part_wrapper()
    _drop_dangling(root)
    _indent(root, _mixed_tags(key))
    setting = profile.bits.doctype if target == "bits" else profile.jats.doctype
    doctype = _doctype(setting, schema, etree.QName(root).localname)
    path = path or out_dir / _FILES[target]
    data = etree.tostring(etree.ElementTree(root), xml_declaration=True, encoding="UTF-8",
                          doctype=doctype) + b"\n"
    path.write_bytes(data)
    # validate the bytes as written, so error line numbers point into the file
    parsed = etree.fromstring(data, etree.XMLParser(load_dtd=False, no_network=True,
                                                    resolve_entities=False))
    errors = _missing_metadata(target, profile, meta, sem) + schemas.validate(parsed, key)
    stats = {**sem.stats, **w.stats, "xrefs": w.ctx.stats["xref"],
             "ext_links": w.ctx.stats["ext-link"], "doi_pub_ids": w.ctx.stats["pub-id"]}
    warnings = list(sem.warnings)
    if target == "jats" and sem.toc is not None:
        warnings = [x for x in warnings if not x.startswith("TOC:")]
        warnings.append("the printed table of contents is not written to JATS (the tag set has "
                        "no element for it)")
    if w.ctx.unresolved:
        stats["citations_unresolved"] = sum(w.ctx.unresolved.values())
        names = sorted(w.ctx.unresolved, key=lambda k: (-w.ctx.unresolved[k], k))
        more = f" (+{len(names) - 12} more)" if len(names) > 12 else ""
        warnings.append(f"{len(names)} author-year citation(s) match no reference: "
                        + "; ".join(names[:12]) + more)
    return ExportResult(target, key, path, not errors, errors, warnings, stats)


def _drop_dangling(root: etree._Element) -> None:
    """A link must point at an element that was written: a nav-pointer to a missing id loses
    its @rid (it keeps the printed page number); an xref to a missing id becomes its text."""
    ids = {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}
    for np in root.iter("nav-pointer"):
        rid = np.get("rid")
        if rid and not all(r in ids for r in rid.split()):
            del np.attrib["rid"]
    for x in list(root.iter("xref")):
        rid = x.get("rid") or ""
        if rid and not all(r in ids for r in rid.split()):
            _unwrap(x)


def _unwrap(el: etree._Element) -> None:
    """Replace ``el`` by its content (text, children, tail), in place."""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    prev = el.getprevious()

    def add_text(s: str | None) -> None:
        if not s:
            return
        if prev is not None:
            prev.tail = (prev.tail or "") + s
        else:
            parent.text = (parent.text or "") + s

    add_text(el.text)
    children = list(el)
    for k, ch in enumerate(children):
        parent.insert(idx + k, ch)
    if children:
        children[-1].tail = (children[-1].tail or "") + (el.tail or "")
    else:
        add_text(el.tail)
    el.tail = None
    parent.remove(el)


def _missing_metadata(target: Target, profile: Profile, meta: Metadata,
                      sem: SemanticDoc) -> list[str]:
    """Plain-language reasons a tag set will reject the file (the DTD errors follow them)."""
    if target != "jats":
        return []
    out = []
    j, part = meta.journal, meta.part
    if profile.jats.tag_set == "publishing":
        if not j.id and not j.nlm_ta:
            out.append("JATS Publishing requires a journal id: set journal.id in the metadata file")
        if not j.issn and not j.eissn:
            out.append("JATS Publishing requires an ISSN: set journal.issn or journal.eissn "
                       "in the metadata file")
    if profile.jats.tag_set == "authoring":
        if not part.contributors and not sem.contributors:
            out.append("JATS Authoring requires contributors: set article.contributors in the "
                       "metadata file")
        if not sem.abstract:
            out.append("JATS Authoring requires an abstract: none was found in the PDF; set "
                       "article.abstract in the metadata file")
    return out
