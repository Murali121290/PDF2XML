"""Semantic enrichment: canonical Document → the document structure BITS/JATS need.

The canonical model records what is on the page (flat blocks in reading order, heading levels,
styles). Publishing XML needs document structure: a title and front matter, nested sections,
labelled floats with their notes, a reference list, and editorial queries kept out of the text.
``build`` derives that structure. It never changes the canonical Document.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal, TypeVar

from pdf2xml.merge.lists import follows, marker_kind, ordered_kind
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
    TableCell,
)
from pdf2xml.publishing import matter as mt
from pdf2xml.publishing.citations import Parsed, parse_reference
from pdf2xml.publishing.matter import (
    ANY_KINDS,
    BACK_KINDS,
    FRONT_KINDS,
    GlossItem,
    Imprint,
    IndexDiv,
    IndexEntry,
    TocEntry,
    despace,
    kind_of,
    label_only,
    runs_text,
    strip_prefix,
    walk_toc,
)
from pdf2xml.publishing.matter import is_caps as _is_caps
from pdf2xml.publishing.matter import names as _names
from pdf2xml.publishing.matter import slice_runs as _slice
from pdf2xml.publishing.matter import trim_runs as _trim
from pdf2xml.publishing.profile import Metadata, Profile

_CAPTION_LABEL = re.compile(
    r"^\s*((?:table|figure|fig\.?|box|exhibit|chart|plate|map|scheme|diagram|illustration|"
    r"photo|panel)\s*[A-Z]?\d+(?:[.\-–]\d+)*[a-z]?)(?:\s*[.:—–|]\s*|\s+|$)",
    re.I,
)
_HEADING_LABEL = re.compile(
    r"^\s*((?i:chapter|part|section|appendix|box|unit|module|lesson)\s+"
    r"(?:\d+(?:\.\d+)*|[A-Z]|[IVXLC]+)\b\.?"
    r"|\d{1,2}(?:\.\d{1,2})*\.?(?=\s)"
    r"|[A-Z]\.(?=\s)"
    r"|[IVX]{1,4}\.(?=\s))\s*"
)
_BOX_LABEL = re.compile(r"^box\s+\S+", re.I)
# "(continued)", "Table 2.1 (continued)", "BOX 11.2 Case Study Part 1 (continued)"
_CONTINUED = re.compile(r"^\s*(?:(?:table|figure|fig\.|box|exhibit)\b[^()]{0,120})?"
                        r"\(?\s*cont(?:inued|'d|\.)\s*\)?\s*$", re.I)
_NOTE = re.compile(r"^\s*(sources?|notes?|adapted from|reprinted|abbreviations?)\b", re.I)
_KEYWORDS_INLINE = re.compile(r"^\s*(key\s?words|index terms)\s*[:.—-]\s*", re.I)
_END_PUNCT = re.compile(r"[.!?:;\"”’)\]]\s*$")
_FRONT = {"abstract": "abstract", "summary of chapter": "abstract",
          "keywords": "keywords", "key words": "keywords", "keyword": "keywords",
          "index terms": "keywords", "mesh terms": "mesh", "mesh": "mesh"}
_SEC_TYPES = {
    "introduction": "intro", "methods": "methods", "method": "methods",
    "materials and methods": "materials|methods", "methodology": "methods",
    "results": "results", "discussion": "discussion", "results and discussion":
    "results|discussion", "conclusion": "conclusions", "conclusions": "conclusions",
    "case report": "cases", "case study": "cases",
}


# ---------------------------------------------------------------- structures

@dataclass
class FloatObj:
    kind: Literal["table", "fig"]
    block: Table | Figure
    label: str | None = None
    title: list[Run] = field(default_factory=list)
    title_style: str | None = None
    notes: list[list[Run]] = field(default_factory=list)
    id: str = ""


@dataclass
class Query:
    text: str
    page: int


@dataclass
class Epigraph:
    quote: list[Run]
    attrib: list[Run]
    style: str | None
    page: int = 0


@dataclass
class BoxNode:
    """A box: drawn on the page (a canonical ``Box``), or opened by a heading labelled
    "Box 11.1". Holds its own content and sub-sections."""

    label: str | None
    title: list[Run]
    title_style: str | None
    depth: int
    nodes: list[Node] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)
    id: str = ""
    kind: str = "box"                   # @content-type: box, tip, example, warning, …
    page: int = 0
    heading: Heading | None = None      # the title heading, when there is one


Node = (Paragraph | Caption | ListBlock | Code | Formula | Box | FloatObj | Query | Epigraph
        | BoxNode)


@dataclass
class Section:
    heading: Heading
    depth: int
    label: str | None
    title: list[Run]
    nodes: list[Node] = field(default_factory=list)
    children: list[Section] = field(default_factory=list)
    id: str = ""
    sec_type: str | None = None
    drop_if_empty: bool = False


@dataclass
class Reference:
    runs: list[Run]
    label: str | None = None
    id: str = ""
    pub_type: str | None = None
    keys: set[tuple[str, str]] = field(default_factory=set)   # (name key, year)
    number: int | None = None
    queries: list[Query] = field(default_factory=list)
    parsed: Parsed | None = None        # granular tagging, when the reference was understood
    page: int = 0

    @property
    def text(self) -> str:
        return runs_text(self.runs)


@dataclass
class Footnote:
    runs: list[Run]
    label: str | None
    id: str = ""
    page: int = 0


@dataclass
class Matter:
    """Front or back matter: the contents, a preface, acknowledgments, a glossary, an index,
    endnotes, appendices, contributor notes … ``kind`` names it; untitled pages before the
    contents are ``front-matter-part`` with ``part_type`` (title-page, copyright-page …)."""

    kind: str
    heading: Heading | None = None
    label: str | None = None
    title: list[Run] = field(default_factory=list)
    title_style: str | None = None
    part_type: str | None = None
    items: list[Heading | Node] = field(default_factory=list)    # untitled pages, as printed
    nodes: list[Node] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    toc: list[TocEntry] = field(default_factory=list)
    terms: list[GlossItem] = field(default_factory=list)
    index: list[IndexDiv] = field(default_factory=list)
    notes: list[tuple[list[Run], list[Footnote]]] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    children: list[Matter] = field(default_factory=list)        # appendices in "Appendices"
    id: str = ""
    page: int = 0


@dataclass
class Unit:
    """A chapter or a part of a book with chapters in it; for a single chapter or article, the
    whole document. Holds its own reference list, footnotes and back matter."""

    kind: str = "chapter"                   # chapter | part
    heading: Heading | None = None
    label: str | None = None
    title: list[Run] = field(default_factory=list)
    title_style: str | None = None
    contributors: list[tuple[str, str]] = field(default_factory=list)
    abstract: list[Paragraph] = field(default_factory=list)
    abstract_title: list[Run] = field(default_factory=list)
    keywords: list[tuple[str, list[Run], list[str]]] = field(default_factory=list)
    preamble: list[Node] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    ref_title: list[Run] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    back: list[Matter] = field(default_factory=list)
    front: list[Matter] = field(default_factory=list)
    children: list[Unit] = field(default_factory=list)
    note_ids: dict[str, str] = field(default_factory=dict)      # printed note number → fn id
    id: str = ""
    page: int = 0
    last_page: int = 0


Item = Heading | Node | Matter


@dataclass
class SemanticDoc:
    styles: dict[str, Style]
    title: list[Run] = field(default_factory=list)
    title_style: str | None = None
    subtitle: str | None = None
    label: str | None = None
    contributors: list[tuple[str, str]] = field(default_factory=list)   # detected (given, surname)
    abstract: list[Paragraph] = field(default_factory=list)
    abstract_title: list[Run] = field(default_factory=list)
    # (group type, printed title, keywords)
    keywords: list[tuple[str, list[Run], list[str]]] = field(default_factory=list)
    preamble: list[Node] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    ref_title: list[Run] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    targets: dict[str, tuple[str, str]] = field(default_factory=dict)  # label key → (id, ref-type)
    node_ids: dict[int, str] = field(default_factory=dict)   # id(canonical block) → XML id
    # printed page numbers (folios) by PDF page, when the running heads/footers give them
    folios: dict[int, str] = field(default_factory=dict)
    # where each page begins: id(node) → [(character offset in the node's text, PDF page)];
    # offset 0 is the start of the node, a larger one a page break inside a joined paragraph
    page_marks: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    page_ids: dict[int, str] = field(default_factory=dict)   # PDF page → XML id of its marker
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    # ---- books: front matter, chapters and back matter (``book``); a chapter or article keeps
    # its body in ``preamble``/``sections`` and its back matter in ``back``
    book: bool = False
    front: list[Matter] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    back: list[Matter | Unit] = field(default_factory=list)
    note_ids: dict[str, str] = field(default_factory=dict)   # chapter/article: note number → id
    book_title: list[Run] = field(default_factory=list)      # from the title page
    book_subtitle: str | None = None
    book_contributors: list[tuple[str, str, str]] = field(default_factory=list)
    imprint: Imprint | None = None                           # from the copyright page

    @property
    def title_text(self) -> str:
        return runs_text(self.title).strip()

    @property
    def toc(self) -> Matter | None:
        return next((m for m in self.front if m.kind == "toc" and m.toc), None)


def label_key(label: str) -> str:
    """"TABLE 11.1" / "Fig. 2" → "table 11.1" / "figure 2" (used to resolve callouts)."""
    k = re.sub(r"\s+", " ", label.strip().lower()).rstrip(".:")
    k = re.sub(r"^figs?\.?\s*", "figure ", k)
    k = re.sub(r"^(tables|figures|boxes)\b", lambda m: m.group(1)[:-1] if m.group(1) != "boxes"
               else "box", k)
    return re.sub(r"\s*[–-]\s*", "-", k)


_plain = mt.plain


def _head_key(text: str) -> str:
    return re.sub(r"[^a-z]+", "", text.lower())


# ---------------------------------------------------------------- build

class _Builder:
    def __init__(self, doc: Document, profile: Profile, meta: Metadata,
                 book: bool | None = False) -> None:
        """``book``: True writes a whole book (front matter, chapters, back matter), False one
        chapter or article, None decides from the document (a printed contents page or a
        copyright page with an ISBN)."""
        self.doc = doc
        self.p = profile
        self.meta = meta
        self.book = book
        self.sem = SemanticDoc(styles=doc.styles)
        self.stats: dict[str, int] = {"running_heads_dropped": 0, "marks_dropped": 0,
                                      "paragraphs_joined": 0, "queries": 0}
        self.flow: list[Item] = []
        self.rank: dict[int, int] = {}            # id(Heading) → depth
        self.maybe_empty: set[int] = set()        # id(Heading)
        self.query_styles: set[str] = set()
        # page breaks inside joined paragraphs / list items: id(node) → [(offset, page)]
        self.breaks: dict[int, list[tuple[int, int]]] = {}
        self.tail: dict[int, Paragraph | ListItem] = {}   # id(joined node) → its last piece
        self._alive: list[object] = []            # keeps objects whose id() is a dict key
        self.margin_heads: set[int] = set()       # id(block): running heads set as text
        self.no_join: set[int] = set()            # id(block): index / glossary entry lines
        self.rev_folio: dict[str, int] = {}       # printed page number → PDF page
        self.refs_total = 0
        self.refs_tagged = 0

    # -- step 1: flat flow with floats, queries and marks resolved
    def _flow(self) -> None:
        self.query_styles = self._query_styles(re.compile(self.p.queries.pattern))
        self.margin_heads = self._margin_heads()
        self.flow = [*self._flow_blocks(self.doc.body)]

    def _in_margin(self, b: Heading | Paragraph) -> bool:
        """In the top or bottom sixth of the page, where running heads and feet are set."""
        hgt = next((p.height for p in self.doc.pages if p.n == b.page), 0.0)
        return not hgt or b.bbox[3] <= hgt / 6 or b.bbox[1] >= hgt * 5 / 6

    def _margin_heads(self) -> set[int]:
        """Running heads extracted as text blocks: short lines in the top or bottom tenth of the
        page whose words repeat there on three pages, or on two when they are also a heading or
        a running head elsewhere ("CONTENTS", "PREFACE", a chapter title). A heading is only
        dropped when it repeats on three pages, and never the first time."""
        if not self.p.drop_running_heads:
            return set()
        heights = {p.n: p.height for p in self.doc.pages}
        head_keys = {_head_key(a.text) for a in self.doc.artifacts
                     if a.kind in ("header", "footer")}
        head_keys |= {_head_key(despace(runs_text(b.runs))) for b in self.doc.body
                      if isinstance(b, Heading)}
        cand: dict[str, list[Paragraph | Heading]] = {}
        first: dict[str, int] = {}
        for b in self.doc.body:
            if not isinstance(b, Paragraph | Heading):
                continue
            t = runs_text(b.runs).strip()
            k = _head_key(despace(t))
            if len(k) >= 4:
                first.setdefault(k, id(b))
            hgt = heights.get(b.page)
            if not hgt or not t or len(t) > 80 or len(k) < 4:
                continue
            if b.bbox[3] <= 0.1 * hgt or b.bbox[1] >= 0.9 * hgt:
                cand.setdefault(k, []).append(b)
        drop: set[int] = set()
        for k, bs in cand.items():
            pages = {b.page for b in bs}
            if len(pages) < 3 and not (len(pages) == 2 and k in head_keys):
                continue
            for b in bs:
                if isinstance(b, Heading) and (len(pages) < 3 or first.get(k) == id(b)):
                    continue
                drop.add(id(b))
        return drop

    def _flow_blocks(self, body: Sequence[object]) -> list[Heading | Node]:
        p = self.p
        out: list[Heading | Node] = []
        qre = re.compile(p.queries.pattern)
        head_keys = {_head_key(a.text) for a in self.doc.artifacts
                     if a.kind in ("header", "footer")}
        captions = {b.target: b for b in body if isinstance(b, Caption) and b.target
                    and not _CONTINUED.match(runs_text(b.runs))}
        used = {id(c) for c in captions.values()}
        pending_float: FloatObj | None = None
        for k, b in enumerate(body):
            if id(b) in self.margin_heads:
                self.stats["running_heads_dropped"] += 1
                continue
            if isinstance(b, Box):
                out.append(self._box(b))
                pending_float = None
                continue
            if isinstance(b, Caption) and id(b) in used:
                continue
            if isinstance(b, Table | Figure) and b.id not in captions:
                cap = _adjacent_caption(body, k, used)
                if cap is not None:
                    captions[b.id] = cap
                    used.add(id(cap))
            text = runs_text(getattr(b, "runs", []) or [])
            if isinstance(b, Paragraph | Caption) and _CONTINUED.match(text):
                self.stats["continued_dropped"] = self.stats.get("continued_dropped", 0) + 1
                continue
            if (isinstance(b, Paragraph | Caption) and b.style in self.query_styles
                    and not qre.match(text)):
                # the rest of a query that wrapped into its own block
                out.append(Query(text.strip(), b.page))
                continue
            if isinstance(b, Table):
                b = self._table_queries(b, qre)
            # A running head repeats the page header text (usually the chapter title) but, unlike
            # the real title, carries a page number or an ornament, and sits in the page margin
            # (a contents entry "PART I … 1" matches the header text too, mid-page).
            if (p.drop_running_heads and isinstance(b, Heading | Paragraph) and head_keys
                    and len(_head_key(text)) >= 8 and _head_key(text) in head_keys
                    and re.search(r"[\d■●◆▪|]", text) and self._in_margin(b)):
                self.stats["running_heads_dropped"] += 1
                continue
            if isinstance(b, Paragraph | Caption) and qre.match(text):
                out.append(Query(text.strip(), b.page))
                self.stats["queries"] += 1
                continue
            # "Source: …" / "Note: …" lines right after a table or figure belong to it
            if (pending_float is not None and isinstance(b, Paragraph | Caption)
                    and _NOTE.match(text) and len(pending_float.notes) < 4):
                pending_float.notes.append(list(b.runs))
                continue
            if isinstance(b, Paragraph):
                b, intrusion = self._intrusions(b, qre)
                ep = self._epigraph(b)
                out.append(ep or b)
                if intrusion:
                    out.append(intrusion)
                pending_float = None
                continue
            if isinstance(b, ListBlock):
                lb, queries = self._list_queries(b, qre)
                if lb is not None:
                    out.append(lb)
                out.extend(queries)
                pending_float = None
                continue
            if isinstance(b, Figure):
                w, h = b.bbox[2] - b.bbox[0], b.bbox[3] - b.bbox[1]
                if w < p.min_figure_pt and h < p.min_figure_pt and b.id not in captions:
                    self.stats["marks_dropped"] += 1
                    continue
            if isinstance(b, Table | Figure):
                f = FloatObj("table" if isinstance(b, Table) else "fig", b)
                cap = captions.get(b.id)
                if cap is not None:
                    self._caption(f, cap)
                out.append(f)
                pending_float = f
                continue
            pending_float = None
            if isinstance(b, Paragraph | Caption | Heading | Code | Formula):
                out.append(b)
        return out

    def _box(self, b: Box) -> BoxNode:
        """A drawn box: its first heading is the box title ("Box 2.1 Checklist" → label +
        title), later headings open sections inside the box."""
        nodes = self._join_nodes(self._flow_blocks(b.children))
        box = BoxNode(None, [], None, depth=0, kind=b.role.replace("_", "-"), page=b.page)
        h = nodes[0] if nodes else None
        if isinstance(h, Heading):
            nodes.pop(0)
            label, _, runs = self._split_heading(h)
            box.label, box.title, box.title_style, box.heading = label, runs, h.style, h
        cur: Section | None = None
        for n in nodes:
            if isinstance(n, Heading):
                label, text, runs = self._split_heading(n)
                cur = Section(n, 1, label, runs)
                box.children.append(cur)
            elif cur is not None:
                cur.nodes.append(n)
            else:
                box.nodes.append(n)
        self.stats["boxes"] = self.stats.get("boxes", 0) + 1
        return box

    def _query_styles(self, qre: re.Pattern[str]) -> set[str]:
        """Catalogue styles used only for queries: small type, and every block in the style that
        is long enough to judge starts like a query."""
        body = self.doc.styles.get("body")
        if body is None:
            return set()
        seen: dict[str, list[bool]] = {}
        blocks: list[Paragraph | Caption | ListItem] = []
        for b in self.doc.body:
            for x in (b.children if isinstance(b, Box) else [b]):
                if isinstance(x, Paragraph | Caption):
                    blocks.append(x)
                elif isinstance(x, ListBlock):
                    blocks.extend(x.items)
        for b in blocks:
            st = self.doc.styles.get(b.style or "")
            if st is None or st.size > 0.8 * body.size:
                continue
            seen.setdefault(b.style or "", []).append(bool(qre.match(runs_text(b.runs))))
        return {k for k, v in seen.items() if sum(v) >= 2 and sum(v) >= 0.5 * len(v)}

    def _table_queries(self, t: Table, qre: re.Pattern[str]) -> Table:
        """Drop a column of margin queries that the table detector swept into the grid."""
        grid: dict[int, list[tuple[int, int]]] = {}       # column → [(row, cell index)]
        taken: set[tuple[int, int]] = set()
        for r, row in enumerate(t.rows):
            c = 0
            for i, cell in enumerate(row):
                while (r, c) in taken:
                    c += 1
                for rr in range(r, r + cell.rowspan):
                    for cc in range(c, c + cell.colspan):
                        taken.add((rr, cc))
                if cell.colspan == 1:
                    grid.setdefault(c, []).append((r, i))
                c += cell.colspan
        body = self.doc.styles.get("body")
        if body is None:
            return t

        def small(cell: TableCell) -> bool:
            return all((r.size or body.size) <= 0.8 * body.size
                       for r in cell.runs if r.text.strip())

        drop: set[tuple[int, int]] = set()
        for cells in grid.values():
            filled = [(ri, ci) for ri, ci in cells if runs_text(t.rows[ri][ci].runs).strip()]
            texts = {rc: runs_text(t.rows[rc[0]][rc[1]].runs).strip() for rc in filled}
            querylike = {rc for rc in filled
                         if qre.match(texts[rc]) or small(t.rows[rc[0]][rc[1]])}
            # every body cell is query text; a real header above the column ("Level") stays
            body_cells = [rc for rc in filled if not t.rows[rc[0]][rc[1]].header]
            if (any(qre.match(texts[rc]) for rc in filled) and body_cells
                    and all(rc in querylike for rc in body_cells)):
                drop.update(querylike)
        if not drop:
            return t
        rows = [[cell for i, cell in enumerate(row) if (r, i) not in drop]
                for r, row in enumerate(t.rows)]
        self.stats["queries"] += 1
        n_cols = max((sum(c.colspan for c in row) for row in rows), default=1)
        return t.model_copy(update={"rows": rows, "n_cols": n_cols})

    def _intrusions(self, b: Paragraph, qre: re.Pattern[str]) -> tuple[Paragraph, Query | None]:
        """Margin queries set in small type that the layout merged into a body paragraph."""
        style = self.doc.styles.get(b.style or "")
        if style is None:
            return b, None
        small = [i for i, r in enumerate(b.runs)
                 if r.size is not None and r.size <= 0.75 * style.size and not (r.sup or r.sub)]
        if not small or not qre.match(runs_text([b.runs[i] for i in small])):
            return b, None
        # the query's last words can also end up as a superscript word: letters only, no digits
        drop = set(small) | {i for i, r in enumerate(b.runs)
                             if r.sup and r.text.strip() and not re.search(r"\d", r.text)}
        kept: list[Run] = []
        for i, r in enumerate(b.runs):
            if i in drop:
                continue
            if kept and kept[-1].text.endswith(" ") and r.text.startswith(" "):
                r = r.model_copy(update={"text": r.text.lstrip()})
            kept.append(r)
        if kept:
            kept[-1] = kept[-1].model_copy(update={"text": kept[-1].text.rstrip()})
        q = " ".join(b.runs[i].text.strip() for i in sorted(drop))
        self.stats["queries"] += 1
        return b.model_copy(update={"runs": kept}), Query(q, b.page)

    def _epigraph(self, b: Paragraph) -> Epigraph | None:
        """An italic quotation followed by a dash and its source."""
        style = self.doc.styles.get(b.style or "")
        if not self.p.front_matter or style is None or not style.italic:
            return None
        m = re.match(r"^\s*(?P<q>.{15,}?)\s*[—―]\s*(?P<a>[A-Z].{2,250}?)\s*$",
                     runs_text(b.runs), re.S)
        if not m:
            return None
        return Epigraph(_trim(_slice(b.runs, m.start("q"), m.end("q"))),
                        _trim(_slice(b.runs, m.start("a"), m.end("a"))), b.style, b.page)

    def _list_queries(self, lb: ListBlock, qre: re.Pattern[str]) -> tuple[ListBlock | None,
                                                                          list[Query]]:
        keep: list[ListItem] = []
        queries: list[Query] = []
        for it in lb.items:
            t = runs_text(it.runs)
            if (qre.match(t) or it.style in self.query_styles) and not it.children:
                queries.append(Query(t.strip(), it.page))
                self.stats["queries"] += 1
            else:
                keep.append(it)
        if not keep:
            return None, queries
        if len(keep) < len(lb.items):
            lb = lb.model_copy(update={"items": keep})
        return lb, queries

    def _caption(self, f: FloatObj, cap: Caption) -> None:
        runs = list(cap.runs)
        f.title_style = cap.style
        if self.p.labels == "split":
            m = _CAPTION_LABEL.match(runs_text(runs))
            if m:
                f.label = m.group(1).strip()
                runs = strip_prefix(runs, m.end())
        f.title = runs

    # -- step 2: paragraphs and lists split by column/page breaks, floats or boxes
    def _join(self) -> None:
        # runs before any front/back matter is made: the flow holds headings and nodes only
        flow = [x for x in self.flow if not isinstance(x, Matter)]
        self.no_join = self._entry_lists(flow)
        self.flow = [*self._join_nodes(flow)]

    def _entry_lists(self, flow: Sequence[Heading | Node]) -> set[int]:
        """Paragraphs under an Index or Glossary heading: one entry per line ("design thinking,
        5" / "history of, 2"), never joined into running text; nor are the page numbers of a
        contents page ("23" / "oH")."""
        out: set[int] = set()
        if not self.p.matter:
            return out
        k = 0
        while k < len(flow):
            b = flow[k]
            if isinstance(b, Heading) and kind_of(runs_text(b.runs)) == "toc":
                near = [x for x in flow[:k] if _page(x) == b.page]      # read before the heading
                for x in [*near, *flow[k + 1:]]:
                    if _page(x) > b.page + 3:
                        break
                    if isinstance(x, Paragraph) and (mt.folio(runs_text(x.runs))
                                                     or mt.split_trailing(runs_text(x.runs))):
                        out.add(id(x))
            if isinstance(b, Heading) and kind_of(runs_text(b.runs)) in ("index", "glossary"):
                j = k + 1
                while j < len(flow):
                    x = flow[j]
                    if (isinstance(x, Heading) and x.level <= b.level
                            and len(runs_text(x.runs).strip()) > 3):
                        break
                    out.add(id(x))
                    j += 1
                k = j
                continue
            k += 1
        return out

    def _join_nodes(self, flow: list[Heading | Node]) -> list[Heading | Node]:
        if not self.p.join_paragraphs:
            return flow
        skip: set[int] = set()
        out: list[Heading | Node] = []
        for i, a in enumerate(flow):
            if i in skip:
                continue
            k = i
            if isinstance(a, Paragraph) and a.runs and id(a) not in self.no_join:
                while (j := self._next_same(flow, k, a.style, skip)) is not None:
                    b = flow[j]
                    assert isinstance(b, Paragraph)
                    if id(b) in self.no_join or not _continues(a, b, self._broken(a, b)):
                        break
                    a = self._concat(a, b)
                    skip.add(j)
                    self.stats["paragraphs_joined"] += 1
                    k = j
            elif isinstance(a, ListBlock) and a.items and id(a) not in self.no_join:
                while (j := self._next_list_part(flow, k, skip)) is not None:
                    merged = self._continue_list(a, flow[j])
                    if merged is None:
                        break
                    a = merged
                    skip.add(j)
                    self.stats["lists_joined"] = self.stats.get("lists_joined", 0) + 1
                    k = j
            out.append(a)
        return out

    def _broken(self, a: Paragraph | ListItem, b: Paragraph | ListItem) -> bool:
        """A page or column break lies between ``a`` (its last piece, if joined) and ``b``."""
        t = self.tail.get(id(a), a)
        return b.page != t.page or b.bbox[1] < t.bbox[1] - 2

    def _concat(self, a: P, b: Paragraph | ListItem) -> P:
        """``a`` + ``b`` as one paragraph, remembering where the page turns inside it."""
        new, offset = _concat(a, b)
        breaks = self.breaks.pop(id(a), [])
        t = self.tail.pop(id(a), a)
        if b.page != t.page:
            breaks.append((offset, b.page))
        if breaks:
            self.breaks[id(new)] = breaks
        self.tail[id(new)] = b
        self._alive.append(new)
        return new

    @staticmethod
    def _next_list_part(flow: list[Heading | Node], k: int, skip: set[int]) -> int | None:
        """The paragraph or list after a list, looking past floats, queries and boxes."""
        j = k + 1
        while j < len(flow) and j < k + 6:
            b = flow[j]
            if j in skip or isinstance(b, FloatObj | Query | Figure | Epigraph | BoxNode):
                j += 1
                continue
            return j if isinstance(b, Paragraph | ListBlock) else None
        return None

    def _continue_list(self, a: ListBlock, b: Heading | Node) -> ListBlock | None:
        """``a`` carried on in ``b``: the rest of its last item ("… the notwendigen" /
        "Personal und …"), or more items of the same list after a page or column break."""
        last = _last_item(a)
        if isinstance(b, Paragraph):
            if b.runs and _continues(last, b, self._broken(last, b)):
                return _replace_last(a, self._concat(last, b))
            return None
        if not isinstance(b, ListBlock) or not b.items:
            return None
        first = b.items[0]
        # items after a page/column break, or after the rest of our last item that ran over
        if not (self._broken(last, first) or isinstance(self.tail.get(id(last)), Paragraph)):
            return None
        if not first.marker and not first.children and _continues(last, first, True):
            # the first "item" is the rest of our last item: the remaining items are ours too
            a = _replace_last(a, self._concat(last, first))
            return a.model_copy(update={"items": [*a.items, *b.items[1:]]})
        ka, kb = marker_kind(a.items[-1].marker), marker_kind(first.marker)
        if not ka or ka != kb:
            return None
        if ordered_kind(kb) and not follows(a.items[-1].marker, first.marker):
            return None
        return a.model_copy(update={"items": [*a.items, *b.items]})

    @staticmethod
    def _next_same(flow: list[Heading | Node], k: int, style: str | None,
                   skip: set[int]) -> int | None:
        """Next paragraph of the same style, looking past floats, queries, boxes and
        other-style paragraphs (an epigraph or sidebar printed between the two halves)."""
        j = k + 1
        while j < len(flow) and j < k + 6:
            b = flow[j]
            if j in skip or isinstance(b, FloatObj | Query | Figure | Epigraph | BoxNode) or (
                    isinstance(b, Paragraph | Caption) and b.style != style):
                j += 1
                continue
            return j if isinstance(b, Paragraph) and b.style == style else None
        return None

    # -- step 3: title
    def _title(self) -> None:
        mp = self.meta.part
        heads = [b for b in self.flow if isinstance(b, Heading)]
        top = min((h.level for h in heads), default=None)
        first = next((h for h in heads if h.level == top), None)
        at: int | None = None
        if first is not None and (not mp.title
                                  or _plain(runs_text(first.runs)) == _plain(mp.title)):
            at = next(k for k, x in enumerate(self.flow) if x is first)
            self.flow.pop(at)
            self.sem.title = list(first.runs)
            self.sem.title_style = first.style
        if mp.title:
            self.sem.title = [Run(text=mp.title)]
        elif not self.sem.title and self.doc.meta.title:
            self.sem.title = [Run(text=self.doc.meta.title)]
        self.sem.subtitle = mp.subtitle
        self.sem.label = mp.label
        if at is not None and self.p.front_matter:
            self._authors(at)

    def _authors(self, start: int) -> None:
        """A byline right after the title ("Mallory Bejster and Heide Cygan")."""
        known = {(p.surname or "").lower() for p in self.meta.part.contributors}
        for j in range(start, min(start + 4, len(self.flow))):
            b = self.flow[j]
            if isinstance(b, Query | FloatObj | Epigraph):
                continue
            if isinstance(b, Paragraph):
                names = _names(runs_text(b.runs))
                if names and (not known or {s.lower() for _, s in names} <= known):
                    self.sem.contributors = names
                    del self.flow[j]
            return

    # -- step 4: heading ranks → section depth
    def _ranks(self) -> None:
        heads = [b for b in self.flow if isinstance(b, Heading)]
        cf = self.p.sections.caps_first

        def key(h: Heading) -> tuple[int, float, int]:
            # the catalogue gives many sizes one level (h6, h6-2 …): larger type ranks higher,
            # and ALL CAPS only outranks mixed case of the same size
            return (h.level, -round(self._size(h), 1),
                    0 if cf and _is_caps(runs_text(h.runs)) else 1)

        order = sorted({key(h) for h in heads})
        dense = {k: i + 1 for i, k in enumerate(order)}
        smap = self.p.sections.map if self.p.sections.mode == "style-map" else {}
        for h in heads:
            self.rank[id(h)] = max(1, smap.get(h.style or "", dense[key(h)]))

    # -- step 5: abstract, keywords, reference list, back matter
    def _kind(self, h: Heading) -> str | None:
        """The front/back-matter kind a heading names ("refs" for a reference list)."""
        text = runs_text(h.runs)
        rest = self._split_heading(h)[1]
        if self.p.references and (_plain(rest) in self._refs_heads or
                                  _plain(text) in self._refs_heads):
            return "refs"
        if not self.p.matter:
            return None
        return kind_of(text) or kind_of(rest)

    @property
    def _refs_heads(self) -> set[str]:
        return {_plain(t) for t in self.p.reference_headings}

    def _regions(self, flow: Sequence[Item], u: Unit) -> list[Item]:
        """Pull the abstract, keywords and reference list out of a chapter's flow, and its back
        matter (endnotes, glossary, appendices, contributor notes …) into ``u.back``."""
        refs_heads = self._refs_heads
        out: list[Item] = []
        i = 0
        while i < len(flow):
            b = flow[i]
            if isinstance(b, Heading):
                t = _plain(self._split_heading(b)[1])
                kind = _FRONT.get(t) if self.p.front_matter else None
                is_refs = self.p.references and t in refs_heads
                mkind = None
                if not kind and not is_refs:
                    k = self._kind(b)
                    mkind = k if k in BACK_KINDS | ANY_KINDS else None
                if kind or is_refs or mkind:
                    end = self._region_end(flow, i, refs=bool(is_refs))
                    body = flow[i + 1:end]
                    if out and isinstance(out[-1], Heading):
                        self.maybe_empty.add(id(out[-1]))
                    title = self._split_heading(b)[2]
                    if mkind:
                        u.back.append(self._matter(mkind, b, body))
                    elif is_refs:
                        u.ref_title = title
                        self._references(body, u.references)
                    elif kind == "abstract":
                        u.abstract.extend(x for x in body if isinstance(x, Paragraph))
                        u.abstract_title = title
                    else:
                        self._keywords("author" if kind == "keywords" else "MeSH", title, body,
                                       u.keywords)
                    if not mkind:
                        out.extend(x for x in body if isinstance(x, Query) and not is_refs)
                    if (not is_refs and not mkind and end < len(flow)
                            and isinstance(flow[end], Heading)):
                        self.maybe_empty.add(id(flow[end]))
                    i = end
                    continue
            elif (isinstance(b, Paragraph) and self.p.front_matter
                  and _KEYWORDS_INLINE.match(runs_text(b.runs))):
                txt = _KEYWORDS_INLINE.sub("", runs_text(b.runs))
                u.keywords.append(("author", [], _split_kwds(txt)))
                i += 1
                continue
            out.append(b)
            i += 1
        return out

    def _region_end(self, flow: Sequence[Item], i: int, refs: bool = False) -> int:
        """Up to the next heading ranked as high. A reference list also ends at a lower heading
        unless references follow it ("Primary sources"): a heading set smaller than
        "REFERENCES" can still open the next section of running text."""
        start = flow[i]
        assert isinstance(start, Heading)
        depth = self.rank.get(id(start), 1)
        j = i + 1
        while j < len(flow):
            b = flow[j]
            if isinstance(b, Matter) or (isinstance(b, Heading)
                                         and self.rank.get(id(b), 1) <= depth):
                break
            if (refs and isinstance(b, Heading) and len(runs_text(b.runs).strip()) > 3
                    and not _reference_like(self._next_text(flow, j))):
                break
            if (refs and j > i + 1 and isinstance(b, Paragraph | Caption)
                    and not _reference_like(runs_text(b.runs))
                    and len(runs_text(b.runs)) <= 150 and not _YEAR.search(runs_text(b.runs))
                    and not _reference_like(self._next_text(flow, j))):
                break               # a section title the engine did not mark as a heading
            j += 1
        return j

    @staticmethod
    def _next_text(flow: Sequence[Item], j: int) -> str:
        nxt = next((x for x in flow[j + 1:j + 4]
                    if isinstance(x, Paragraph | ListBlock | Caption)), None)
        if isinstance(nxt, ListBlock):
            return runs_text(nxt.items[0].runs) if nxt.items else ""
        return runs_text(nxt.runs) if nxt is not None else ""

    def _references(self, body: Sequence[Item], refs: list[Reference]) -> None:

        def add_items(items: Sequence[ListItem]) -> None:
            for it in items:
                refs.append(Reference(list(it.runs), label=it.marker, page=it.page))
                for ch in it.children:
                    if isinstance(ch, ListBlock):
                        add_items(ch.items)

        for b in body:
            if isinstance(b, ListBlock):
                add_items(b.items)
            elif isinstance(b, Paragraph | Caption):
                refs.append(Reference(list(b.runs), page=b.page))
            elif isinstance(b, Query):
                if refs:
                    refs[-1].queries.append(b)
            elif isinstance(b, Heading):
                continue

    def _finish_refs(self, refs: list[Reference]) -> list[Reference]:
        # Entries split across columns/pages continue in lower case, with a URL tail, or a digit.
        merged: list[Reference] = []
        for r in refs:
            t = r.text.strip()
            if not t:
                continue
            first = t[:1]
            if merged and (first.islower() or first.isdigit() and not re.match(r"^\d+[.)\]]", t)
                           or t.lower().startswith(("http", "www.", "doi"))):
                prev = merged[-1]
                sep = "" if prev.text.rstrip().endswith(("/", "-")) else " "
                prev.runs = [*prev.runs[:-1], prev.runs[-1].model_copy(
                    update={"text": prev.runs[-1].text.rstrip() + sep})] + list(r.runs)
                prev.queries.extend(r.queries)
                continue
            merged.append(r)
        for r in merged:
            m = re.match(r"^\s*\[?(\d{1,4})[.)\]]\s+", r.text)
            if m and (r.label is None or r.label.strip(".)[]") == m.group(1)):
                r.number = int(m.group(1))
                r.label = m.group(1)
                r.runs = strip_prefix(r.runs, m.end())
            elif r.label and re.fullmatch(r"\[?\d{1,4}[.)\]]?", r.label.strip()):
                r.number = int(re.sub(r"\D", "", r.label))
                r.label = str(r.number)
            else:
                r.label = None
            r.pub_type = _pub_type(r.text)
            r.keys = _ref_keys(r.text)
            if self.p.granular_references:
                r.parsed = parse_reference(r.text, self._italics(r.runs))
            if r.parsed is not None:
                r.keys |= _parsed_keys(r.parsed)
                kind = "book" if r.parsed.kind == "chapter" else r.parsed.kind
                if r.pub_type == "other" or r.parsed.kind == "chapter":
                    r.pub_type = kind
        self.refs_total += len(merged)
        self.refs_tagged += sum(r.parsed is not None for r in merged)
        return merged

    def _italics(self, runs: Sequence[Run]) -> list[tuple[int, int]]:
        """Character ranges set in italics (body style as the base)."""
        base = self.doc.styles.get("body")
        out: list[tuple[int, int]] = []
        pos = 0
        for r in runs:
            italic = r.italic if r.italic is not None else bool(base and base.italic)
            if italic and r.text:
                if out and out[-1][1] == pos:
                    out[-1] = (out[-1][0], pos + len(r.text))
                else:
                    out.append((pos, pos + len(r.text)))
            pos += len(r.text)
        return out

    def _keywords(self, kind: str, title: list[Run], body: Sequence[Item],
                  out: list[tuple[str, list[Run], list[str]]]) -> None:
        kw: list[str] = []
        for b in body:
            if isinstance(b, Paragraph):
                kw.extend(_split_kwds(runs_text(b.runs)))
            elif isinstance(b, ListBlock):
                kw.extend(runs_text(it.runs).strip() for it in b.items)
        if kw:
            out.append((kind, title, [k for k in kw if k]))

    # -- step 6: section tree
    def _split_heading(self, h: Heading) -> tuple[str | None, str, list[Run]]:
        return self._split_runs(list(h.runs))

    def _tree(self, flow: Sequence[Item]) -> tuple[list[Node], list[Section], list[Matter]]:
        """Nest sections by heading depth. Returns the text before the first heading, the
        sections, and any front matter met on the way (a contents page inside a chapter)."""
        preamble: list[Node] = []
        sections: list[Section] = []
        matters: list[Matter] = []
        stack: list[Section] = []
        box: BoxNode | None = None
        bstack: list[Section] = []           # sections inside the open box
        pending_label: str | None = None
        for k, b in enumerate(flow):
            if isinstance(b, Matter):
                matters.append(b)
                continue
            if not isinstance(b, Heading):
                if box is not None:
                    (bstack[-1].nodes if bstack else box.nodes).append(b)
                else:
                    (stack[-1].nodes if stack else preamble).append(b)
                continue
            nxt = flow[k + 1] if k + 1 < len(flow) else None
            if (label_only(runs_text(b.runs)) and isinstance(nxt, Heading) and nxt.page == b.page
                    and not label_only(runs_text(nxt.runs))):
                # "S E C T I O N 1" / "CHAPTER FOURTEEN" printed above the title it labels
                pending_label = despace(runs_text(b.runs)).strip().rstrip(".:")
                continue
            label, text, runs = self._split_heading(b)
            if pending_label:
                label, pending_label = label or pending_label, None
            depth = self.rank.get(id(b), 1)
            is_box = bool(label and _BOX_LABEL.match(label))
            if box is not None and (is_box or depth < box.depth):
                box, bstack = None, []
            if is_box:
                box = BoxNode(label, runs, b.style, depth, page=b.page, heading=b)
                (stack[-1].nodes if stack else preamble).append(box)
                continue
            sec = Section(b, depth, label, runs,
                          sec_type=_SEC_TYPES.get(_plain(text))
                          if self.p.sections.sec_types else None,
                          drop_if_empty=id(b) in self.maybe_empty)
            target_stack, top = (bstack, box.children) if box is not None else (
                stack, sections)
            while target_stack and target_stack[-1].depth >= depth:
                target_stack.pop()
            (target_stack[-1].children if target_stack else top).append(sec)
            target_stack.append(sec)
        return preamble, _prune(sections, self.stats), matters

    # -- step 6: printed table of contents
    def _toc(self) -> None:
        """A printed contents page near the front: its entries are read and it is replaced in
        the flow by a ``Matter`` of kind "toc" (other text on the page stays in the flow)."""
        if not self.p.matter:
            return
        limit = max(12, len(self.doc.pages) // 4)
        for i, b in enumerate(self.flow):
            if not isinstance(b, Heading | Paragraph) or b.page > limit:
                continue
            text = runs_text(b.runs)
            if len(text) > 60 or kind_of(text) != "toc" or (
                    isinstance(b, Paragraph) and len(text.split()) > 4):
                continue
            span = self._toc_span(i)
            if span is None:
                continue
            lo, hi = span
            blocks: list[mt.TocBlock] = []
            kept: list[Item] = []
            for k in range(lo, hi):
                if k == i:
                    continue
                x = self.flow[k]
                blk = _toc_block(x)
                if blk is None:
                    kept.append(x)
                else:
                    blocks.append(blk)
            parsed = mt.parse_toc(blocks, text)
            entries = walk_toc(parsed.entries)
            if sum(1 for e in entries if e.folio) < 2:
                continue
            m = Matter("toc", b if isinstance(b, Heading) else None, None, list(b.runs), b.style,
                       toc=parsed.entries, page=b.page)
            start = min(lo, i)
            between = self.flow[i + 1:lo] if lo > i else []
            self.flow = [*self.flow[:start], m, *between, *kept, *self.flow[max(hi, i + 1):]]
            self.stats["toc_entries"] = len(entries)
            if parsed.unpaired:
                self.sem.warnings.append(f"TOC: {parsed.unpaired} printed page number(s) could "
                                         "not be matched to an entry title")
            return

    def _toc_span(self, i: int) -> tuple[int, int] | None:
        """Flow indexes [lo, hi) of the contents entries around the heading at ``i``: the rest
        of its page and each following page that is mostly lines ending in a page number. The
        engine may read the heading after the entries of its page: those count too."""
        flow = self.flow
        head = flow[i]
        hsize = self._size(head)
        before = i
        k = i - 1
        while k >= 0 and _page(flow[k]) == _page(head) and _toc_block(flow[k]) is not None:
            if _entry_like(flow[k]):
                before = k
            k -= 1
        pages = [_page(head)]
        cands: list[int] = []
        j = i + 1
        while j < len(flow):
            pg = _page(flow[j])
            if pg not in pages:
                if pg != pages[-1] + 1:
                    break
                k = j
                while k < len(flow) and _page(flow[k]) == pg:
                    k += 1
                n, total = mt.toc_lines_with_folio(
                    [blk for x in flow[j:k] if (blk := _toc_block(x)) is not None])
                if n < 2 or n < 0.2 * total:   # author lines under entries dilute it
                    break
                pages.append(pg)
            cands.append(j)
            j += 1
        like = [k for k in cands if _entry_like(flow[k])]
        if not like and before == i:
            return None
        lo, hi = (min(like), max(like) + 1) if like else (i + 1, i + 1)
        while lo - 1 > i and self._absorbable(flow[lo - 1], hsize):
            lo -= 1
        if before < i:
            lo = before
        while hi < len(flow) and hi in cands and _page(flow[hi]) == _page(flow[hi - 1]):
            nxt = flow[hi]
            if not isinstance(nxt, Paragraph | Caption) or len(runs_text(nxt.runs)) > 100:
                break
            hi += 1
        return lo, hi

    def _size(self, b: Item) -> float:
        st = self.doc.styles.get(getattr(b, "style", None) or "")
        return st.size if st else 0.0

    def _absorbable(self, b: Item, hsize: float) -> bool:
        if isinstance(b, Paragraph | Caption | ListBlock):
            return True
        if isinstance(b, FloatObj):
            return b.kind == "table"
        return isinstance(b, Heading) and self._size(b) <= hsize + 0.5

    # -- step 7: a whole book, or one chapter / article
    def _texts(self, items: Sequence[Item]) -> list[str]:
        out: list[str] = []
        for x in items:
            if isinstance(x, Heading | Paragraph | Caption):
                out.append(runs_text(x.runs))
            elif isinstance(x, ListBlock):
                out.extend(runs_text(it.runs) for it in x.items)
            elif isinstance(x, FloatObj) and isinstance(x.block, Table):
                out.extend(" ".join(runs_text(c.runs) for c in row) for row in x.block.rows)
            elif isinstance(x, Epigraph):
                out.append(runs_text(x.quote) + " " + runs_text(x.attrib))
        return [t for t in out if t.strip()]

    def _pages_text(self, limit: int) -> dict[int, str]:
        by: dict[int, list[Item]] = {}
        for x in self.flow:
            if not isinstance(x, Matter) and _page(x) <= limit:
                by.setdefault(_page(x), []).append(x)
        return {pg: " ".join(self._texts(xs)) for pg, xs in by.items()}

    def _copyright_pages(self) -> set[int]:
        return {pg for pg, t in self._pages_text(20).items() if mt.imprint_score(t) >= 2}

    def _is_book(self) -> bool:
        if self.book is not None:
            return self.book
        if not self.p.matter:
            return False
        toc = next((b for b in self.flow if isinstance(b, Matter) and b.kind == "toc"), None)
        if toc is not None and sum(1 for e in walk_toc(toc.toc) if e.folio) >= 3:
            return True
        return any(mt.imprint_score(t) >= 3 and re.search(r"\bisbn\b", t, re.I)
                   for t in self._pages_text(15).values())

    def _chapter(self) -> None:
        """One chapter or article: title and byline, abstract, sections, reference list, back
        matter; a contents page, if printed, goes to ``front``."""
        self._title()
        self._ranks()
        u = Unit()
        rest = self._regions(self.flow, u)
        u.preamble, u.sections, self.sem.front = self._tree(rest)
        sem = self.sem
        sem.abstract, sem.abstract_title, sem.keywords = u.abstract, u.abstract_title, u.keywords
        mp = self.meta.part
        if mp.abstract:
            sem.abstract = [Paragraph(page=1, bbox=(0, 0, 0, 0), runs=[Run(text=p)])
                            for p in mp.abstract.split("\n\n") if p.strip()]
        if mp.keywords:
            title = next((k[1] for k in sem.keywords if k[0] == "author"), [])
            sem.keywords = [("author", title, list(mp.keywords)),
                            *[k for k in sem.keywords if k[0] != "author"]]
        if self.p.references:
            for rb in self.doc.references:
                if isinstance(rb, Paragraph | ListItem):
                    u.references.append(Reference(list(rb.runs), page=rb.page))
        sem.references = self._finish_refs(u.references)
        sem.ref_title = u.ref_title
        sem.preamble, sem.sections = u.preamble, u.sections
        sem.back = list(u.back)
        sem.footnotes = self._page_footnotes()

    def _book(self) -> None:
        """Front matter, chapters (or parts holding chapters) and back matter. Chapters start at
        the headings the printed contents points to; without a usable contents page, at the
        highest-ranked headings after the copyright page."""
        self._ranks()
        flow = self.flow
        toc = next((b for b in flow if isinstance(b, Matter) and b.kind == "toc"), None)
        starts = self._toc_starts(toc) if toc is not None else []
        if len(starts) < 2:
            if toc is not None:
                for e in walk_toc(toc.toc):
                    e.target_obj = None
            starts = self._rank_starts()
        sem = self.sem
        matter_kinds = FRONT_KINDS | ANY_KINDS | BACK_KINDS | {"refs", "toc"}
        body_i = next((n for n, s in enumerate(starts) if s.kind not in matter_kinds), None)
        if body_i is None:
            body_i = next((n for n, s in enumerate(starts) if s.kind not in FRONT_KINDS), None)
        front_end = starts[body_i].idx if body_i is not None else len(flow)
        sem.front = self._front(flow[:front_end])
        if body_i is None:
            return
        rest = starts[body_i:]
        back_kinds = ANY_KINDS | BACK_KINDS | {"refs"}
        back_i = next((n for n, s in enumerate(rest) if n and s.kind in back_kinds), len(rest))
        for n, s in enumerate(rest):
            end = rest[n + 1].idx if n + 1 < len(rest) else len(flow)
            if n < back_i:
                sem.units.append(self._make_unit(s, end))
            elif s.kind in back_kinds:
                m = self._matter(s.kind or "front-matter-part", s.heading,
                                 flow[s.idx + s.n:end], s)
                if s.entry is not None:
                    s.entry.target_obj = m
                sem.back.append(m)
            else:
                sem.back.append(self._make_unit(s, end))
        units = self._all_units()
        for fn in self._page_footnotes():
            u = self._unit_for(fn.page, units)
            (u.footnotes if u is not None else sem.footnotes).append(fn)
        leftover: list[Reference] = []
        if self.p.references:
            for rb in self.doc.references:
                if isinstance(rb, Paragraph | ListItem):
                    r = Reference(list(rb.runs), page=rb.page)
                    u = self._unit_for(rb.page, units)
                    (u.references if u is not None else leftover).append(r)
        for u in units:
            u.references = self._finish_refs(u.references)
        if leftover:
            sem.back.append(Matter("refs", references=self._finish_refs(leftover),
                                   page=leftover[0].page))

    def _all_units(self) -> list[Unit]:
        out: list[Unit] = []

        def walk(u: Unit) -> None:
            out.append(u)
            for c in u.children:
                walk(c)

        for u in self.sem.units:
            walk(u)
        for x in self.sem.back:
            if isinstance(x, Unit):
                walk(x)
        return out

    @staticmethod
    def _unit_for(page: int, units: Sequence[Unit]) -> Unit | None:
        hit = None
        for u in units:
            if u.page <= page <= max(u.last_page, u.page) and not u.children:
                hit = u
        return hit

    def _make_unit(self, s: _Start, end: int) -> Unit:
        flow = self.flow
        u = Unit(kind="part" if s.children else "chapter", heading=s.heading, label=s.label,
                 title=s.title, title_style=s.style, page=s.page)
        if s.entry is not None:
            s.entry.target_obj = u
            for c in s.entry.contributors:
                u.contributors.extend(_names(c))
        first = s.children[0].idx if s.children else end
        items = list(flow[s.idx + s.n:first])
        if items and isinstance(items[0], Paragraph) and not u.contributors:
            byline = _names(runs_text(items[0].runs))
            if byline:
                u.contributors = byline                 # "Paul Rand" under the title
                items = items[1:]
        self._unit_body(u, items)
        for n, child in enumerate(s.children):
            cend = s.children[n + 1].idx if n + 1 < len(s.children) else end
            u.children.append(self._make_unit(child, cend))
        u.last_page = max([u.page] + [_page(x) for x in flow[s.idx:end]])
        if end < len(flow):             # up to the page before the next chapter (blank pages too)
            u.last_page = max(u.last_page, _page(flow[end]) - 1)
        return u

    def _unit_body(self, u: Unit, items: Sequence[Item]) -> None:
        rest = self._regions(items, u)
        u.preamble, u.sections, u.front = self._tree(rest)

    # -- chapter starts
    def _toc_starts(self, toc: Matter) -> list[_Start]:
        """The heading each top-level contents entry points to, in order. A "Part" entry holds
        the chapters listed under it."""
        flow = self.flow
        cursor = next(k for k, x in enumerate(flow) if x is toc) + 1
        out: list[_Start] = []
        for e in toc.toc:
            s = self._match(e, cursor)
            part = e.children and (e.division or bool(
                mt.DIVISION.match(despace(f"{e.label or ''} {e.text}").strip())))
            if part:
                kids: list[_Start] = []
                c2 = s.idx + s.n if s is not None else cursor
                for ch in e.children:
                    cs = self._match(ch, c2)
                    if cs is not None:
                        kids.append(cs)
                        c2 = cs.idx + cs.n
                if s is None:
                    out.extend(kids)
                else:
                    s.children = kids
                    out.append(s)
                cursor = max(cursor, c2)
            elif s is not None:
                out.append(s)
                cursor = s.idx + s.n
        return out

    def _match(self, e: TocEntry, cursor: int) -> _Start | None:
        """The heading (with the label printed above it and title lines after it) that a
        contents entry names: on its printed page (±1) with a close title, else anywhere after
        ``cursor`` with the same title."""
        want = _entry_keys(e)
        if not want:
            return None
        pdf = self.rev_folio.get(e.folio.lower()) if e.folio else None
        flow = self.flow
        # an entry without a readable page number may still match a close title (the heading
        # drops a "(TFA)" the contents prints), in order after the previous chapter
        for near in ((True, False) if pdf is not None else (True,)):
            for j in range(cursor, len(flow)):
                b = flow[j]
                if not isinstance(b, Heading):
                    continue
                if near and pdf is not None:
                    if b.page > pdf + 2:
                        break
                    if b.page < pdf - 1:
                        continue
                s = self._start_at(j, want, fuzzy=near)
                if s is not None:
                    s.entry = e
                    return s
        return None

    def _start_at(self, j: int, want: set[str], fuzzy: bool) -> _Start | None:
        flow = self.flow
        b = flow[j]
        assert isinstance(b, Heading)
        label: str | None = None
        k = j
        nxt = flow[j + 1] if j + 1 < len(flow) else None
        if label_only(runs_text(b.runs)) and isinstance(nxt, Heading) and nxt.page == b.page:
            label = despace(runs_text(b.runs)).strip().rstrip(".:")
            k = j + 1
        runs: list[Run] = []
        best: tuple[int, int, _Start] | None = None
        for n in range(3):
            h = flow[k + n] if k + n < len(flow) else None
            if not isinstance(h, Heading) or h.page != b.page:
                break
            if n and label_only(runs_text(h.runs)):
                break
            runs = mt.join_runs(runs, h.runs)
            lab, rest, rest_runs = self._split_runs(runs)
            got = {mt.key(runs_text(runs)), mt.key(rest)}
            if label:
                got.add(mt.key(f"{label} {runs_text(runs)}"))
            score = _keys_score(want, got - {""}, fuzzy)
            if score and (best is None or score >= best[0]):
                head = flow[k]
                assert isinstance(head, Heading)
                best = (score, n, _Start(j, k - j + n + 1, head, rest_runs if lab else runs,
                                         head.style, label or lab,
                                         self._kind_text(runs_text(runs), rest), b.page))
        return best[2] if best else None

    def _kind_text(self, text: str, rest: str) -> str | None:
        if self.p.references and (_plain(rest) in self._refs_heads
                                  or _plain(text) in self._refs_heads):
            return "refs"
        return kind_of(text) or kind_of(rest)

    def _label_head(self, k: int) -> bool:
        """A heading that is only a label ("CHAPTER FOURTEEN") with its title right after it."""
        flow = self.flow
        b = flow[k]
        nxt = flow[k + 1] if k + 1 < len(flow) else None
        return (isinstance(b, Heading) and label_only(runs_text(b.runs))
                and isinstance(nxt, Heading) and nxt.page == b.page
                and not label_only(runs_text(nxt.runs)))

    def _rank_starts(self) -> list[_Start]:
        """Chapters without a contents page: the highest-ranked headings after the last contents
        or copyright page (front-matter headings such as Preface do not count)."""
        flow = self.flow
        signal = -1
        cp = self._copyright_pages()
        for k, b in enumerate(flow):
            if isinstance(b, Matter) or _page(b) in cp:
                signal = k
        body = [k for k, b in enumerate(flow) if k > signal and isinstance(b, Heading)
                and not self._label_head(k) and self._kind(b) not in FRONT_KINDS]
        if not body:
            return []
        top = min(self.rank.get(id(flow[k]), 1) for k in body)
        out: list[_Start] = []
        for k in body:
            h = flow[k]
            assert isinstance(h, Heading)
            if self.rank.get(id(h), 1) != top:
                continue
            j, label = k, None
            if k - 1 > signal and self._label_head(k - 1):
                prev = flow[k - 1]
                assert isinstance(prev, Heading)
                j, label = k - 1, despace(runs_text(prev.runs)).strip().rstrip(".:")
            lab, _, runs = self._split_heading(h)
            out.append(_Start(j, k - j + 1, h, runs, h.style, label or lab, self._kind(h),
                              h.page))
        return out

    # -- front matter
    def _front(self, items: Sequence[Item]) -> list[Matter]:
        """Everything before the first chapter: titled parts (Preface, Acknowledgments …), the
        contents, and untitled pages classed as cover, half-title, title page, copyright page,
        dedication …"""
        starts = [k for k, b in enumerate(items) if isinstance(b, Matter) or (
            isinstance(b, Heading) and self._kind(b) in FRONT_KINDS | ANY_KINDS)]
        self.title_page = self._find_title_page(items[:starts[0] if starts else len(items)])
        out = self._front_pages(items[:starts[0] if starts else len(items)])
        for n, k in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(items)
            b = items[k]
            if isinstance(b, Matter):
                out.append(b)
                out.extend(self._front_pages(items[k + 1:end]))
            elif isinstance(b, Heading):
                kind = self._kind(b)
                if kind in (None, "toc"):          # a contents heading whose entries were unread
                    out.extend(self._front_pages(items[k:end]))
                else:
                    out.append(self._matter(kind, b, items[k + 1:end]))
        return out

    def _find_title_page(self, items: Sequence[Item]) -> int | None:
        best: tuple[float, int] | None = None
        for x in items:
            if isinstance(x, Heading | Paragraph) and runs_text(x.runs).strip():
                size = self._size(x)
                if best is None or size > best[0]:
                    best = (size, _page(x))
        return best[1] if best else None

    def _front_pages(self, items: Sequence[Item]) -> list[Matter]:
        groups: list[tuple[int, list[Heading | Node]]] = []
        for x in items:
            if isinstance(x, Matter):
                continue
            pg = _page(x)
            if groups and groups[-1][0] == pg:
                groups[-1][1].append(x)
            else:
                groups.append((pg, [x]))
        out: list[Matter] = []
        for pg, xs in groups:
            cls = self._page_class(pg, xs)
            if cls == "copyright-page":
                self._imprint(xs)
            elif cls == "title-page":
                self._title_page_meta(xs)
            if out and out[-1].part_type == cls and cls in ("other", "series-page",
                                                            "copyright-page"):
                out[-1].items.extend(xs)
                continue
            kind = "dedication" if cls == "dedication" else "front-matter-part"
            out.append(Matter(kind, part_type=cls, items=list(xs), page=pg))
            self.stats["front_pages_classified"] = (
                self.stats.get("front_pages_classified", 0) + 1)
        for m in out:
            # pages of running text (a summary, a series list) keep their sections; title and
            # copyright pages stay line by line
            if m.part_type in ("other", "series-page") and any(
                    isinstance(x, Heading) for x in m.items):
                items = list(m.items)
                first = items[0]
                if isinstance(first, Heading):
                    items.pop(0)
                    m.heading = first
                    m.label, _, m.title = self._split_heading(first)
                    m.title_style = first.style
                m.nodes, m.sections, _ = self._tree(items)
                m.items = []
        return out

    def _page_class(self, pg: int, xs: Sequence[Heading | Node]) -> str:
        texts = self._texts(xs)
        joined = " ".join(texts)
        if xs and all(isinstance(x, FloatObj) and x.kind == "fig" for x in xs) and pg <= 2:
            return "cover"
        if mt.imprint_score(joined) >= 2:
            return "copyright-page"
        if pg == self.title_page:
            return "title-page"
        if mt.dedication_like(texts):
            return "dedication"
        if xs and all(isinstance(x, Epigraph) for x in xs):
            return "epigraph"
        if mt.series_like(texts):
            return "series-page"
        if (self.title_page is not None and pg < self.title_page and len(texts) <= 3
                and len(joined) <= 120):
            return "half-title"
        return "other"

    def _imprint(self, xs: Sequence[Heading | Node]) -> None:
        imp = mt.parse_imprint(self._texts(xs))
        cur = self.sem.imprint
        if cur is None:
            self.sem.imprint = imp
            return
        have = {re.sub(r"\D", "", i.value) for i in cur.isbns}
        cur.isbns.extend(i for i in imp.isbns if re.sub(r"\D", "", i.value) not in have)
        for f in ("publisher", "publisher_loc", "year", "copyright", "copyright_year", "edition"):
            if getattr(cur, f) is None:
                setattr(cur, f, getattr(imp, f))

    def _title_page_meta(self, xs: Sequence[Heading | Node]) -> None:
        """Book title, subtitle and people from the title page: the lines in the largest type
        are the title ("Title:" + next line → title and subtitle); "by …" lines name people."""
        lines = [x for x in xs if isinstance(x, Heading | Paragraph) and runs_text(x.runs).strip()]
        if not lines:
            return
        top = max(self._size(x) for x in lines)
        k = 0
        while k < len(lines) and self._size(lines[k]) < top:
            k += 1
        first = k
        while (k < len(lines) and self._size(lines[k]) == top
               and not mt.bylines(runs_text(lines[k].runs))):
            k += 1
        head = lines[first:k]
        if not head:
            return
        t0 = runs_text(head[0].runs).rstrip()
        subtitle: str | None = None
        if len(head) >= 2 and t0.endswith(":"):
            title = _trim(list(head[0].runs))
            title[-1] = title[-1].model_copy(update={"text": title[-1].text.rstrip(" :")})
            subtitle = " ".join(" ".join(runs_text(h.runs).split()) for h in head[1:])
        else:
            title = []
            for h in head:
                title = mt.join_runs(title, h.runs)
            nxt = lines[k] if k < len(lines) else None
            if isinstance(nxt, Heading) and not mt.bylines(runs_text(nxt.runs)):
                subtitle = " ".join(runs_text(nxt.runs).split())
        self.sem.book_title = [r for r in title if r.text]
        self.sem.book_subtitle = subtitle
        people: list[tuple[str, str, str]] = []
        for x in lines:
            for p in mt.bylines(runs_text(x.runs)):
                if p not in people:
                    people.append(p)
        self.sem.book_contributors = people

    # -- a front- or back-matter part
    def _split_runs(self, runs: list[Run]) -> tuple[str | None, str, list[Run]]:
        text = runs_text(runs)
        m = _HEADING_LABEL.match(text)
        if m and _BOX_LABEL.match(m.group(1)):
            return m.group(1).strip(), text[m.end():].strip(), strip_prefix(runs, m.end())
        if self.p.sections.split_labels and m and text[m.end():].strip():
            return m.group(1).strip(), text[m.end():].strip(), strip_prefix(runs, m.end())
        return None, text.strip(), runs

    def _matter(self, kind: str, h: Heading | None, body: Sequence[Item],
                start: _Start | None = None) -> Matter:
        label, text, runs = self._split_heading(h) if h is not None else (None, "", [])
        if start is not None:
            label, runs = start.label, start.title
        page = h.page if h is not None else (_page(body[0]) if body else 0)
        m = Matter(kind, h, label, runs, h.style if h is not None else None, page=page)
        blocks = [x for x in body if not isinstance(x, Matter)]
        if kind == "notes":
            self._notes(m, blocks)
        elif kind == "glossary":
            canon = [_entry_block(x) for x in blocks]
            usable = [(k, blk) for k, blk in enumerate(canon) if blk is not None]
            intro, terms = mt.glossary([blk for _, blk in usable], self.doc.styles)
            if terms:
                m.terms = terms
                keep = {usable[i][0] for i in intro}
                m.nodes = [x for k, x in enumerate(blocks) if (k in keep or canon[k] is None)
                           and not isinstance(x, Heading | Matter)]
            else:
                m.nodes, m.sections, _ = self._tree(blocks)
        elif kind == "index":
            m.index = mt.index_entries([blk for x in blocks if (blk := _entry_block(x)) is not None
                                        and not isinstance(blk, Table)])
            m.nodes, m.sections, _ = self._tree(blocks)
        elif kind in ("further", "refs"):
            self._references(blocks, m.references)
            m.references = self._finish_refs(m.references)
        elif kind == "app" and _plain(text) == "appendices":
            m.nodes, secs, _ = self._tree(blocks)
            m.children = [Matter("app", s.heading, s.label, s.title, s.heading.style,
                                 nodes=s.nodes, sections=s.children, page=s.heading.page)
                          for s in secs]
        else:
            m.nodes, m.sections, _ = self._tree(blocks)
        key_ = "front_matter" if kind in FRONT_KINDS else "back_matter"
        self.stats[key_] = self.stats.get(key_, 0) + 1
        return m

    def _notes(self, m: Matter, blocks: Sequence[Item]) -> None:
        """Endnotes: numbered list items or paragraphs, in groups under sub-headings
        ("Chapter 3") when the notes of a whole book are printed together."""
        title: list[Run] = []
        cur: list[Footnote] = []
        num = re.compile(r"^\s*(\d{1,3}|[*†‡§¶])[.)]?\s+")

        def add_items(lb: ListBlock) -> None:
            for it in lb.items:
                runs = list(it.runs)
                label = (it.marker or "").strip().strip("().[]") or None
                if label is None and (mm := num.match(runs_text(runs))):
                    label, runs = mm.group(1), strip_prefix(runs, mm.end())
                for ch in it.children:
                    if isinstance(ch, ListBlock):
                        add_items(ch)
                    elif isinstance(ch, Paragraph | ListItem):
                        runs = mt.join_runs(runs, ch.runs)
                cur.append(Footnote(runs, label, page=it.page))

        for b in blocks:
            if isinstance(b, Heading):
                if cur:
                    m.notes.append((title, cur))
                title, cur = list(b.runs), []
            elif isinstance(b, ListBlock):
                add_items(b)
            elif isinstance(b, Paragraph | Caption):
                text = runs_text(b.runs)
                mm = num.match(text)
                if mm:
                    cur.append(Footnote(strip_prefix(list(b.runs), mm.end()), mm.group(1),
                                        page=b.page))
                elif cur:
                    cur[-1].runs = mt.join_runs(cur[-1].runs, b.runs)
                else:
                    m.nodes.append(b)
            elif not isinstance(b, Matter):
                m.nodes.append(b)
        if cur:
            m.notes.append((title, cur))
        m.notes = [(t, _split_merged_notes(fns)) for t, fns in m.notes]

    def _page_footnotes(self) -> list[Footnote]:
        out: list[Footnote] = []
        for b in self.doc.footnotes:
            if isinstance(b, Paragraph | ListItem | Caption):
                runs = list(b.runs)
                m = re.match(r"^\s*([0-9]{1,3}|[*†‡§¶#a-z])[.)]?\s+", runs_text(runs))
                label = m.group(1) if m else None
                if m:
                    runs = strip_prefix(runs, m.end())
                out.append(Footnote(runs, label, page=b.page))
        return out

    # -- step 8: ids, callout targets, footnotes
    def _ids(self) -> None:
        ids = self.p.ids
        chapter = self.meta.part.number or self.meta.part.label or ""
        chapter = re.sub(r"[^\w.-]", "", str(chapter))
        counters: dict[str, int] = {}
        used: set[str] = set()

        def make(counter: str, pattern: str, **kw: object) -> str:
            counters[counter] = counters.get(counter, 0) + 1
            raw = pattern.format(n=counters[counter], chapter=chapter, **kw)
            val = re.sub(r"[^\w.-]", "_", raw)
            if not re.match(r"[A-Za-z_]", val):
                val = "id" + val
            base, k = val, 1
            while val in used:
                k += 1
                val = f"{base}_{k}"
            used.add(val)
            return val

        def nodes(ns: Sequence[Node | Heading]) -> None:
            for n in ns:
                if isinstance(n, FloatObj):
                    n.id = make(n.kind, ids.table if n.kind == "table" else ids.fig)
                    if n.label:
                        self.sem.targets[label_key(n.label)] = (n.id, n.kind)
                elif isinstance(n, Formula):
                    self.sem.node_ids[id(n)] = make("formula", ids.formula)
                elif isinstance(n, Box):
                    self.sem.node_ids[id(n)] = make("box", ids.box)
                elif isinstance(n, BoxNode):
                    n.id = make("box", ids.box)
                    if n.label:
                        self.sem.targets[label_key(n.label)] = (n.id, "boxed-text")
                    nodes(n.nodes)
                    secs(n.children)

        def secs(ss: Sequence[Section]) -> None:
            for s in ss:
                s.id = make("sec", ids.sec, level=s.depth)
                nodes(s.nodes)
                secs(s.children)

        endnotes: list[Footnote] = []

        def matter(m: Matter) -> None:
            m.id = make("matter-" + m.kind, ids.matter, kind=_ID_KIND.get(m.kind, "fm"))
            nodes(m.items)
            nodes(m.nodes)
            secs(m.sections)
            for r in m.references:
                r.id = make("ref", ids.ref)
            for _, fns in m.notes:
                endnotes.extend(fns)
            for c in m.children:
                matter(c)

        def unit(u: Unit) -> None:
            u.id = make(u.kind, ids.chapter if u.kind == "chapter" else ids.book_part)
            for m in u.front:
                matter(m)
            nodes(u.preamble)
            secs(u.sections)
            for c in u.children:
                unit(c)
            for r in u.references:
                r.id = make("ref", ids.ref)
            for m in u.back:
                matter(m)

        sem = self.sem
        for m in sem.front:
            matter(m)
        if sem.book:
            for u in sem.units:
                unit(u)
            for x in sem.back:
                matter(x) if isinstance(x, Matter) else unit(x)
        else:
            nodes(sem.preamble)
            secs(sem.sections)
            for r in sem.references:
                r.id = make("ref", ids.ref)
            for x in sem.back:
                matter(x) if isinstance(x, Matter) else unit(x)
        for page in sorted({pg for marks in sem.page_marks.values() for _, pg in marks}):
            folio = sem.folios.get(page, str(page))
            sem.page_ids[page] = make("page", ids.page, folio=folio, page=page)
        for fn in [*sem.footnotes, *(f for u in self._all_units() for f in u.footnotes)]:
            fn.id = make("fn", ids.fn)
        for fn in endnotes:
            fn.id = make("fn", ids.fn)

    def _note_maps(self) -> None:
        """Which footnote or endnote a superscript number in the text points to, per chapter.
        Book-level endnotes grouped under "Chapter 3" (or the chapter's title) go with it."""
        def build(page_fns: Sequence[Footnote], matters: Sequence[Matter | Unit]
                  ) -> dict[str, str]:
            out: dict[str, str] = {}
            count = Counter(f.label for f in page_fns if f.label)
            for f in page_fns:
                if f.label and count[f.label] == 1:
                    out[f.label] = f.id
            for m in matters:
                if isinstance(m, Matter) and m.kind == "notes":
                    for _, fns in m.notes:
                        for f in fns:
                            if f.label:
                                out.setdefault(f.label, f.id)
            return out

        sem = self.sem
        if not sem.book:
            sem.note_ids = build(sem.footnotes, sem.back)
            return
        book_notes = [m for m in sem.back if isinstance(m, Matter) and m.kind == "notes"]
        for u in self._all_units():
            u.note_ids = build(u.footnotes, u.back)
            names = {mt.key(x) for x in (u.label or "", runs_text(u.title),
                                         f"{u.label or ''} {runs_text(u.title)}",
                                         f"chapter {u.label or ''}") if mt.key(x)}
            for m in book_notes:
                for title, fns in m.notes:
                    if title and mt.key(runs_text(title)) in names:
                        for f in fns:
                            if f.label:
                                u.note_ids.setdefault(f.label, f.id)

    # -- step 9: printed page numbers and where each page begins
    def _folios(self) -> None:
        """Printed page numbers, from page-number artifacts and the number at either end of a
        running head/footer ("136 ■ EVALUATION …", "… OUTCOMES ■ 137"). The most common
        offset between printed and PDF page numbers wins, so a chapter number in the head
        ("11 EVALUATION …") is outvoted. Front matter numbered in roman figures (i, ii … xii)
        before the arabic pages gets its own offset."""
        arabic: Counter[int] = Counter()
        roman: Counter[int] = Counter()
        first: dict[int, int] = {}
        for a in self.doc.artifacts:
            t = a.text.strip()
            nums: list[str] = []
            if a.kind == "page_number":
                m = re.fullmatch(r"(?:page\s*)?(\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})"
                                 r"(?:\s*(?:of|/)\s*\d+)?", t, re.I)
                nums = [m.group(1)] if m else []
            elif a.kind in ("header", "footer"):
                lead = re.match(r"(\d{1,4}|[ivxlc]{1,7})(?![\w.,:/-])", t)
                trail = re.search(r"(?<![\w.,:/-])(\d{1,4}|[ivxlc]{1,7})$", t)
                nums = [x.group(1) for x in (lead, trail) if x]
            for num in dict.fromkeys(nums):
                f = mt.folio(num)
                if f is None:
                    continue
                kind, v = f
                if kind == "arabic":
                    arabic[v - a.page] += 1
                    first[v - a.page] = min(first.get(v - a.page, a.page), a.page)
                elif a.kind == "page_number" or num.islower():
                    roman[v - a.page] += 1
        folios: dict[int, str] = {}
        single = len(self.doc.pages) <= 1
        off_a = arabic.most_common(1)[0] if arabic else None
        off_r = roman.most_common(1)[0] if roman else None
        a_off = off_a[0] if off_a and (off_a[1] >= 2 or single) else None
        r_off = off_r[0] if off_r and off_r[1] >= 2 else None
        start = first.get(a_off, 0) if a_off is not None else 0
        for p in self.doc.pages:
            if a_off is not None and p.n + a_off >= 1 and (r_off is None or p.n >= start):
                folios[p.n] = str(p.n + a_off)
            elif r_off is not None and p.n + r_off >= 1 and (a_off is None or p.n < start):
                folios[p.n] = mt.to_roman(p.n + r_off)
        self.sem.folios = folios
        self.rev_folio = {}
        for pg, printed in sorted(folios.items()):
            self.rev_folio.setdefault(printed.lower(), pg)

    def _page_marks(self) -> None:
        """Where each page begins, in output order: at the start of its first node, or inside a
        paragraph (or list item) that runs on from the page before. Pages only move forward, so
        a box or float placed out of page order does not repeat a page. The walk follows the
        order the writer emits nodes in."""
        if self.p.page_markers == "none":
            return
        marks = self.sem.page_marks
        last = 0

        def add(key: object, off: int, page: int) -> None:
            nonlocal last
            if page > last:
                marks.setdefault(id(key), []).append((off, page))
                last = page

        def text_node(n: Paragraph | Caption | ListItem | Heading) -> None:
            add(n, 0, n.page)
            for off, pg in self.breaks.get(id(n), []):
                add(n, off, pg)

        def items(lb: ListBlock) -> None:
            for it in lb.items:
                text_node(it)
                for ch in it.children:
                    if isinstance(ch, ListBlock):
                        items(ch)
                    elif isinstance(ch, Paragraph | Caption | ListItem):
                        text_node(ch)

        def node(n: Node | Heading) -> None:
            if isinstance(n, Paragraph | Caption | Heading):
                text_node(n)
            elif isinstance(n, ListBlock):
                items(n)
            elif isinstance(n, FloatObj):
                add(n, 0, n.block.page)
            elif isinstance(n, BoxNode):
                add(n, 0, n.page)
                for x in n.nodes:
                    node(x)
                for s in n.children:
                    sec(s)
            elif isinstance(n, Query | Epigraph | Code | Formula | Box):
                add(n, 0, n.page)

        def sec(s: Section) -> None:
            add(s.heading, 0, s.heading.page)
            for x in s.nodes:
                node(x)
            for c in s.children:
                sec(c)

        def refs(rs: Sequence[Reference]) -> None:
            for r in rs:
                add(r, 0, r.page)

        def index_entry(e: IndexEntry) -> None:
            add(e, 0, e.page)
            for c in e.children:
                index_entry(c)

        def matter(m: Matter) -> None:
            if m.page and m.title:
                add(m, 0, m.page)
            for e in walk_toc(m.toc):
                add(e, 0, e.page)
            for x in m.items:
                node(x)
            if not m.index:
                for x in m.nodes:
                    node(x)
                for s in m.sections:
                    sec(s)
            for g in m.terms:
                add(g, 0, g.page)
            for _, fns in m.notes:
                for f in fns:
                    add(f, 0, f.page)
            refs(m.references)
            for d in m.index:
                for ie in d.entries:
                    index_entry(ie)
            for c in m.children:
                matter(c)

        def unit(u: Unit) -> None:
            if u.title:
                add(u, 0, u.page)
            for m in u.front:
                matter(m)
            for x in u.preamble:
                node(x)
            for s in u.sections:
                sec(s)
            for c in u.children:
                unit(c)
            refs(u.references)
            for m in u.back:
                matter(m)

        sem = self.sem
        for m in sem.front:
            matter(m)
        if sem.book:
            for u in sem.units:
                unit(u)
        else:
            for n in sem.preamble:
                node(n)
            for s in sem.sections:
                sec(s)
            refs(sem.references)
        for b in sem.back:
            matter(b) if isinstance(b, Matter) else unit(b)

    # -- step 10: contents entries → the chapters and sections they name
    def _anchors(self) -> list[tuple[str, set[str], int]]:
        out: list[tuple[str, set[str], int]] = []

        def keys(label: str | None, title: Sequence[Run]) -> set[str]:
            t = runs_text(title)
            got = {mt.key(t)}
            if label:
                got.add(mt.key(f"{label} {t}"))
            return got - {""}

        def sec(s: Section) -> None:
            out.append((s.id, keys(s.label, s.title), s.heading.page))
            for c in s.children:
                sec(c)

        def matter(m: Matter) -> None:
            if m.title:
                out.append((m.id, keys(m.label, m.title), m.page))
            for s in m.sections:
                sec(s)
            for c in m.children:
                matter(c)

        def unit(u: Unit) -> None:
            if u.title:
                out.append((u.id, keys(u.label, u.title), u.page))
            for s in u.sections:
                sec(s)
            for c in u.children:
                unit(c)
            for m in u.back:
                matter(m)

        sem = self.sem
        for m in sem.front:
            matter(m)
        for u in sem.units:
            unit(u)
        for s in sem.sections:
            sec(s)
        for x in sem.back:
            matter(x) if isinstance(x, Matter) else unit(x)
        return out

    def _link_toc(self) -> None:
        """Point each contents entry at its chapter or section (``nav-pointer``); an entry whose
        heading is not found points at its printed page. Mismatches go to the warnings."""
        toc = self.sem.toc
        if toc is None:
            return
        anchors = self._anchors()
        by_id = {a[0]: n for n, a in enumerate(anchors)}
        cursor = 0
        linked = page_only = 0
        missing: list[str] = []
        entries = walk_toc(toc.toc)
        for e in entries:
            pdf = self.rev_folio.get(e.folio.lower()) if e.folio else None
            obj_id = getattr(e.target_obj, "id", "") if e.target_obj is not None else ""
            if obj_id:
                e.target = obj_id
                cursor = max(cursor, by_id.get(obj_id, cursor - 1) + 1)
            else:
                want = _entry_keys(e)
                hit = None
                if pdf is not None:
                    # the printed page pins it down: the best title on that page (±), wherever
                    # the previous entry matched
                    best = (0, 0)
                    for n, (_aid, akeys, apage) in enumerate(anchors):
                        if not pdf - 1 <= apage <= pdf + 2 or not want:
                            continue
                        score = _keys_score(want, akeys, True)
                        rank = (score, 1 if n >= cursor else 0)
                        if score and rank > best:
                            best, hit = rank, n
                else:
                    for n in range(cursor, len(anchors)):
                        if want and _keys_score(want, anchors[n][1], False):
                            hit = n
                            break
                if hit is not None:
                    e.target = anchors[hit][0]
                    cursor = hit + 1
            if e.target:
                linked += 1
            elif pdf is not None and pdf in self.sem.page_ids:
                e.target = self.sem.page_ids[pdf]
                page_only += 1
            elif e.folio or not e.division:
                missing.append(e.text or e.label or "?")
        s = self.stats
        s["toc_linked"] = linked
        if page_only:
            s["toc_linked_to_page"] = page_only
            self.sem.warnings.append(f"TOC: {page_only} of {len(entries)} entries point at their "
                                     "printed page only (no heading with that title was found)")
        if missing:
            more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
            self.sem.warnings.append(f"TOC: {len(missing)} of {len(entries)} entries match no "
                                     "heading or page: " + "; ".join(missing[:8]) + more)
        if self.sem.book:
            # chapters, and headings ranked like chapters that ended up inside one, that no
            # entry points to
            targeted = {e.target for e in entries if e.target}
            units = [u for u in self._all_units() if u.heading is not None and u.title]
            top = min((self.rank.get(id(u.heading), 1) for u in units if u.heading), default=0)
            unlisted: list[str] = []
            for u in units:
                if u.id not in targeted:
                    unlisted.append(" ".join(runs_text(u.title).split()))
                unlisted.extend(" ".join(runs_text(s.title).split()) for s in u.sections
                                if self.rank.get(id(s.heading), 99) <= top
                                and s.id not in targeted)
            if unlisted:
                more = f" (+{len(unlisted) - 8} more)" if len(unlisted) > 8 else ""
                self.sem.warnings.append(f"{len(unlisted)} chapter(s) are not listed in the "
                                         "printed contents: " + "; ".join(unlisted[:8]) + more)

    def build(self) -> SemanticDoc:
        self._flow()
        self._join()
        self._folios()
        self._toc()
        self.sem.book = self._is_book()
        if self.sem.book:
            self._book()
        else:
            self._chapter()
        self._page_marks()
        self._ids()
        self._note_maps()
        self._link_toc()
        s = self.stats
        units = self._all_units()
        s["sections"] = _count_secs(self.sem.sections) + sum(_count_secs(u.sections)
                                                             for u in units)
        s["references"] = self.refs_total
        s["footnotes"] = len(self.sem.footnotes) + sum(len(u.footnotes) for u in units)
        if self.sem.book:
            s["chapters"] = sum(1 for u in units if u.kind == "chapter")
        self.sem.stats = s
        if self.refs_total and self.p.granular_references:
            s["references_tagged"] = self.refs_tagged
            if self.refs_tagged < self.refs_total:
                self.sem.warnings.append(
                    f"{self.refs_total - self.refs_tagged} of {self.refs_total} reference(s) "
                    "not tagged granularly (kept as plain mixed-citation text)")
        if s["running_heads_dropped"]:
            self.sem.warnings.append(f"{s['running_heads_dropped']} running head(s) were "
                                     "dropped")
        return self.sem


_ID_KIND = {"toc": "toc", "dedication": "ded", "foreword": "fwd", "preface": "pref",
            "front-matter-part": "fm", "ack": "ack", "bio": "bio", "glossary": "gloss",
            "notes": "notes", "app": "app", "index": "idx", "further": "reading",
            "refs": "refs"}


@dataclass
class _Start:
    """Where a chapter (or part, or back-matter part) begins in the flow."""

    idx: int                    # its first heading (the label printed above it, if any)
    n: int                      # headings it takes: label and title lines
    heading: Heading
    title: list[Run]
    style: str | None
    label: str | None
    kind: str | None
    page: int
    entry: TocEntry | None = None
    children: list[_Start] = field(default_factory=list)


def _reference_like(text: str) -> bool:
    """A bibliography entry: a year, and a start like "Smith J," / "1." / "[3]"."""
    t = text.strip()
    return bool(_YEAR.search(t)) and bool(
        re.match(r"(?:\[?\d{1,3}[.\])]\s*)?[A-Z][\w'’\-]+(?:,|\s+[A-Z]{1,3}\b|\s+[A-Z]\.)", t))


def _split_merged_notes(fns: Sequence[Footnote]) -> list[Footnote]:
    """Notes run together into one ("… 1994. 65. Seaton, …"): where the numbering jumps
    (64 → 74), split a note at the number that should come next."""
    out: list[Footnote] = []
    for i, f in enumerate(fns):
        nxt = fns[i + 1].label if i + 1 < len(fns) else None
        cur = f
        while cur.label and cur.label.isdigit():
            want = int(cur.label) + 1
            if nxt == str(want):
                break
            text = runs_text(cur.runs)
            m = re.search(rf"(?<=\S)\s+{want}\.\s+(?=\S)", text)
            if not m:
                break
            out.append(Footnote(_trim(_slice(cur.runs, 0, m.start())), cur.label, page=cur.page))
            cur = Footnote(_trim(_slice(cur.runs, m.end(), len(text))), str(want), page=cur.page)
        out.append(cur)
    return out


def _page(x: object) -> int:
    if isinstance(x, FloatObj):
        return x.block.page
    return int(getattr(x, "page", 0) or 0)


def _toc_block(x: object) -> mt.TocBlock | None:
    if isinstance(x, Heading | Paragraph | Caption | ListBlock | Code):
        return x
    if isinstance(x, FloatObj) and isinstance(x.block, Table):
        return x.block
    return None


def _entry_block(x: object) -> Heading | Paragraph | Caption | ListBlock | Table | None:
    """A block a glossary or index is read from (code is kept as it is)."""
    blk = _toc_block(x)
    return None if isinstance(blk, Code) else blk


def _entry_like(x: object) -> bool:
    """A block that is (part of) a contents entry: a line ending in a page number, a page
    number on its own, or a table row with a page-number column."""
    blk = _toc_block(x)
    if blk is None:
        return False
    if isinstance(blk, Table):
        return any(mt.folio(runs_text(c.runs)) for row in blk.rows for c in row[1:])
    if isinstance(blk, Heading | Paragraph | Caption) and mt.folio(runs_text(blk.runs)):
        return True
    return mt.toc_lines_with_folio([blk])[0] > 0


def _entry_keys(e: TocEntry) -> set[str]:
    got = {mt.key(e.text)}
    if e.label:
        got.add(mt.key(f"{e.label} {e.text}"))
    return got - {""}


def _keys_score(want: set[str], got: set[str], fuzzy: bool) -> int:
    """2: the same title; 1: a close one (one starts the other, or they differ by a few
    letters, as extraction and OCR noise do); 0: different."""
    score = 0
    for w in want:
        for g in got:
            if w == g:
                return 2
            if not fuzzy or min(len(w), len(g)) < 6:
                continue
            if w.startswith(g) or g.startswith(w) or (
                    len(w) >= 8 and SequenceMatcher(None, w, g).ratio() >= 0.85):
                score = 1
    return score


def _adjacent_caption(body: Sequence[object], k: int, used: set[int]) -> Caption | None:
    """An unattached caption right before or after a table/figure whose label names its kind."""
    want = "table" if isinstance(body[k], Table) else "fig"
    for j in (k + 1, k - 1, k + 2):
        if 0 <= j < len(body):
            c = body[j]
            if isinstance(c, Caption) and id(c) not in used and not c.target:
                m = _CAPTION_LABEL.match(runs_text(c.runs))
                if m and label_key(m.group(1)).startswith("table" if want == "table" else "fig"):
                    return c
    return None


def _continues(a: Paragraph | ListItem, b: Paragraph | ListItem, broken: bool = False) -> bool:
    """``b`` continues ``a``: ``a`` ends without closing punctuation and ``b`` starts lower-case.
    Across a page or column break (``broken``) a sentence can also go on with a capital or a
    digit ("… the World Health" / "Organization …"), after a full line of text."""
    at = runs_text(a.runs).rstrip()
    if not at or _END_PUNCT.search(at):
        return False
    bt = runs_text(b.runs)
    # "$89.1bn primary …" is a new item, not a continuation: the lower-case letter must come first
    if re.match(r"\s*[(\[“\"‘']?[a-z]", bt):
        return True
    return (broken and len(at) >= 60 and (at[-1].isalnum() or at[-1] in ",-–—")
            and bool(re.match(r"\s*[(\[“\"‘']?[A-Z0-9]", bt)))


P = TypeVar("P", Paragraph, ListItem)


def _concat(a: P, b: Paragraph | ListItem) -> tuple[P, int]:
    """``a`` + ``b``, and the offset where ``b``'s text starts. A word hyphenated at the break
    ("pre-" / "vention") is rejoined, as the merger does at line ends."""
    tail = a.runs[-1].text.rstrip()
    first = runs_text(b.runs).lstrip()
    if tail.endswith("-") and len(tail) > 2 and tail[-2].isalpha() and first[:1].islower():
        tail, sep = tail[:-1], ""
    else:
        sep = "" if tail.endswith(("-", "/")) else " "
    head: list[Run] = [*a.runs[:-1], a.runs[-1].model_copy(update={"text": tail + sep})]
    offset = sum(len(r.text) for r in head)
    runs = [*head, b.runs[0].model_copy(update={"text": b.runs[0].text.lstrip()}), *b.runs[1:]]
    return a.model_copy(update={"runs": runs}), offset


def _last_item(lb: ListBlock) -> ListItem:
    """The last item of a list, inside its sub-lists."""
    it = lb.items[-1]
    while it.children and isinstance(it.children[-1], ListBlock) and it.children[-1].items:
        it = it.children[-1].items[-1]
    return it


def _replace_last(lb: ListBlock, new: ListItem) -> ListBlock:
    """A copy of ``lb`` with its (deepest) last item replaced by ``new``."""
    last = lb.items[-1]
    if last.children and isinstance(last.children[-1], ListBlock) and last.children[-1].items:
        sub = _replace_last(last.children[-1], new)
        new = last.model_copy(update={"children": [*last.children[:-1], sub]})
    return lb.model_copy(update={"items": [*lb.items[:-1], new]})


def _prune(secs: list[Section], stats: dict[str, int]) -> list[Section]:
    """Drop a heading next to extracted front matter that is left with nothing under it, e.g. a
    "Digital-only content" heading printed with the Abstract/Keywords. Other empty headings keep
    their text. Queries they held move to the previous section."""
    out: list[Section] = []
    for s in secs:
        s.children = _prune(s.children, stats)
        if s.drop_if_empty and not s.children and all(isinstance(n, Query) for n in s.nodes):
            if out:
                _last(out[-1]).nodes.extend(s.nodes)
            stats["empty_sections_dropped"] = stats.get("empty_sections_dropped", 0) + 1
            continue
        out.append(s)
    return out


def _last(s: Section) -> Section:
    while s.children:
        s = s.children[-1]
    return s


def _count_secs(secs: Sequence[Section]) -> int:
    return sum(1 + _count_secs(s.children) for s in secs)


def _split_kwds(text: str) -> list[str]:
    parts = re.split(r"\s*[;,•·|]\s*", text.strip().rstrip("."))
    return [p.strip() for p in parts if p.strip()]


_YEAR = re.compile(r"\(((?:19|20)\d{2}[a-z]?|n\.\s?d\.)[^)]*\)|\b((?:19|20)\d{2}[a-z]?)\b")
_STOP = {"of", "and", "for", "the", "on", "in", "&", "to", "at"}


def _ref_keys(text: str) -> set[tuple[str, str]]:
    """Keys a citation can use to point at this reference: (surname or acronym, year)."""
    m = _YEAR.search(text)
    if not m:
        return set()
    year = (m.group(1) or m.group(2) or "").replace(" ", "").lower()
    head = text[:m.start()].strip().rstrip(".,(").strip()
    keys = set()
    first = re.split(r",|\s\(", head, maxsplit=1)[0].strip()
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", first)
    if words:
        keys.add((words[0].lower(), year))                     # personal surname
        caps = [w for w in words if w.lower() not in _STOP]
        if len(caps) >= 2:
            keys.add(("".join(w[0] for w in caps).lower(), year))   # organisation acronym
        keys.add((" ".join(w.lower() for w in words), year))
    return keys


def _parsed_keys(p: Parsed) -> set[tuple[str, str]]:
    """Citation keys from a tagged reference: the first author's surname, or the organisation's
    name, its words run together (text extracted without spaces) and its acronyms."""
    if not p.year:
        return set()
    year = p.year.replace(" ", "").lower()
    keys: set[tuple[str, str]] = set()
    names = p.surnames[:1] or p.collabs[:1]
    for name in names:
        words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", name)
        if not words:
            continue
        low = [w.lower() for w in words]
        keys |= {(low[0], year), (" ".join(low), year), ("".join(low), year)}
        if not p.surnames:
            caps = [w for w in words if w.lower() not in _STOP]
            if len(caps) >= 2:
                keys.add(("".join(w[0] for w in caps).lower(), year))
            upper = "".join(ch for ch in name if ch.isupper())
            if len(upper) >= 2:
                keys.add((upper.lower(), year))
    return keys


def _pub_type(text: str) -> str:
    t = text.lower()
    # volume(issue), pages — "57(2), 123–130" or "26, 229." or "12(3):e45"
    if (re.search(r"\b\d{1,4}\s*\(\d+[^)]*\)\s*[,:]\s*[a-z]?\d+", t)
            or re.search(r",\s*\d{1,4}\s*,\s*[a-z]?\d+(?:\s*[–-]\s*\d+)?\s*\.", t)
            or "journal" in t):
        return "journal"
    if re.search(r"\(eds?\.\)|\beds?\.,|\b\d+(?:st|nd|rd|th) ed\.|\b(press|publishing|"
                 r"publishers|edition)\b", t):
        return "book"
    if re.search(r"\b(report|white paper|technical report)\b", t):
        return "report"
    if "http" in t or "www." in t:
        return "webpage"
    return "other"


def build(doc: Document, profile: Profile, meta: Metadata, *, book: bool | None = False
          ) -> SemanticDoc:
    """``book``: True for a whole book, False for one chapter or article, None to decide from
    the document (a printed contents page, or a copyright page with an ISBN)."""
    return _Builder(doc, profile, meta, book).build()
