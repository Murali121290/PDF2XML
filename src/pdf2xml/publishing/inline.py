"""Inline content: canonical runs → JATS/BITS inline markup, with link detection.

Runs in the canonical model store only differences from their block's catalogue style, so the
effective formatting is resolved against that style first. Links are found on the joined text of a
block, so a URL or citation that spans several differently formatted runs is still one link.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from lxml import etree

from pdf2xml.model import Run, Style
from pdf2xml.publishing.citations import Span as CSpan
from pdf2xml.publishing.semantic import Reference, label_key
from pdf2xml.styles.fonts import parse_font

XLINK = "http://www.w3.org/1999/xlink"
HREF = f"{{{XLINK}}}href"

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'“”‘’]*", re.I)
_URL_CHARS = r"[^\s<>\"'“”‘’]+"
# A line break inside a URL leaves a space: ".../Downloads/ Essentials/x", "10.1186 /s1287…"
_URL_CONT = re.compile(r"\s{1,2}(" + _URL_CHARS + ")")
_DOI = re.compile(r"\b(?:doi:\s*)?(10\.\d{4,9}/[^\s<>\"'“”‘’]+)", re.I)
_CALLOUT = re.compile(
    r"\b(Tables?|Figures?|Figs?\.|Boxes|Box|Exhibits?|Charts?)\s+([A-Z]?\d+(?:\.\d+)*[a-z]?)\b",
    re.I)
_PAREN = re.compile(r"\(([^()]{3,400})\)")
_YEAR_RE = r"(?:19|20)\d{2}[a-z]?|n\.\s?d\."
_PART = re.compile(
    r"\s*(?:(?:e\.g\.|i\.e\.|see(?:\s+also)?|cf\.|as cited in|adapted from)\s*,?\s*)?"
    r"(?P<name>[A-Z][^;()]*?),?\s+(?P<year>" + _YEAR_RE + ")")
# "Smith (2019)", "Smith and Lee (2019)", "Smith et al. (2019, 2020)",
# "the World Health Organization (2021)"
_NAME_WORD = r"[A-Z][\w'’\-]+"
_NARRATIVE = re.compile(
    r"\b(?P<name>" + _NAME_WORD + r"(?:(?:\s+(?:of|for|and|the|on|in|&)){0,2}\s+" + _NAME_WORD
    + r"){0,6}(?:\s+et\s+al\.)?)(?:’s|'s)?\s+"
    r"\((?P<year>" + _YEAR_RE + r")(?P<more>[^)]*)\)")
# further years after the first: "Smith, 2019, 2020b" / "Smith, 2019a, b"
_MORE_YEARS = re.compile(r"\s*,\s*(?P<y>(?:19|20)\d{2}[a-z]?|[a-z](?![\w.]))")
_ETAL_RE = re.compile(r"\s+et\s+al\.?", re.I)
_BRACKET = re.compile(r"\[(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\]")
_TRAIL = ".,;:)]}>'\"”’"
_REF_TYPES = {"table": "table", "fig": "fig", "sec": "sec", "boxed-text": "boxed-text"}


@dataclass(frozen=True)
class Fmt:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    sc: bool = False
    mono: bool = False
    sup: bool = False
    sub: bool = False
    color: str | None = None


PLAIN = Fmt()
_WRAP = (("bold", "bold"), ("italic", "italic"), ("underline", "underline"),
         ("strike", "strike"), ("sc", "sc"), ("mono", "monospace"))


@dataclass
class LinkContext:
    targets: dict[str, tuple[str, str]] = field(default_factory=dict)
    references: Sequence[Reference] = ()
    citations: Literal["none", "author-year", "numeric", "auto"] = "auto"
    callouts: bool = True
    ext_links: bool = True
    keep_visual: bool = False
    stats: Counter[str] = field(default_factory=Counter)
    unresolved: dict[str, int] = field(default_factory=dict)   # "Glanz 2025" → times seen
    notes: dict[str, str] = field(default_factory=dict)        # note number → fn id

    def __post_init__(self) -> None:
        self.scope(self.references, self.notes)

    def scope(self, references: Sequence[Reference], notes: dict[str, str]) -> None:
        """Link citations and note numbers to this reference list and these notes (each
        chapter of a book has its own)."""
        self.references = references
        self.notes = notes
        self.by_key: dict[tuple[str, str], str] = {}
        self.by_number: dict[int, str] = {}
        for r in self.references:
            for k in sorted(r.keys):
                self.by_key.setdefault(k, r.id)
            if r.number is not None:
                self.by_number.setdefault(r.number, r.id)
        mode = self.citations
        if mode == "auto" and self.references:
            numbered = sum(r.number is not None for r in self.references)
            mode = "numeric" if numbered > len(self.references) / 2 else "author-year"
        self.mode = mode if self.references else "none"


# ---------------------------------------------------------------- formatting

def segments(runs: Sequence[Run], style: Style | None, suppress: frozenset[str] = frozenset(),
             drop: frozenset[str] = frozenset()) -> list[tuple[str, Fmt]]:
    """Resolve each run's effective formatting against the block style.

    ``suppress``: ignore what the *style* contributes (a heading is bold because of its style;
    an italic word inside it is still italic). ``drop``: remove the property entirely.
    """
    inherited = {
        "bold": bool(style and style.bold), "italic": bool(style and style.italic),
        "sc": bool(style and style.small_caps),
        "mono": bool(style and style.generic == "monospace"),
    }
    for name in suppress:
        inherited[name] = False
    base_color = style.color if style and "color" not in suppress else None

    out: list[tuple[str, Fmt]] = []
    for r in runs:
        if not r.text:
            continue
        bold = r.bold if r.bold is not None else inherited["bold"]
        italic = r.italic if r.italic is not None else inherited["italic"]
        sc = r.small_caps if r.small_caps is not None else inherited["sc"]
        mono = parse_font(r.font).mono if r.font else inherited["mono"]
        color = r.color or base_color
        f = Fmt(bold=bold and "bold" not in drop, italic=italic and "italic" not in drop,
                underline=bool(r.underline), strike=bool(r.strike), sc=sc and "sc" not in drop,
                mono=mono, sup=bool(r.sup), sub=bool(r.sub),
                color=None if not color or color == "#000000" else color)
        out.append((r.text, f))
    return out


def _append_text(parent: etree._Element, text: str) -> None:
    if not text:
        return
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _write_piece(parent: etree._Element, text: str, f: Fmt, keep_visual: bool) -> None:
    # colour alone is not semantic: plain text unless the profile keeps visual styling
    if f == PLAIN or (not keep_visual and f == Fmt(color=f.color)):
        _append_text(parent, text)
        return
    el = parent
    for attr, tag in _WRAP:
        if getattr(f, attr):
            el = etree.SubElement(el, tag)
    if keep_visual and f.color:
        el = etree.SubElement(el, "styled-content", style=f"color:{f.color}")
    if f.sup:
        el = etree.SubElement(el, "sup")
    elif f.sub:
        el = etree.SubElement(el, "sub")
    if el is parent:
        _append_text(parent, text)
    else:
        el.text = text


# ---------------------------------------------------------------- links

Span = tuple[int, int, str, dict[str, str]]


def _strip_url(u: str) -> str:
    while u and u[-1] in _TRAIL:
        if u[-1] == ")" and u.count("(") >= u.count(")"):
            break
        u = u[:-1]
    return u


def _continue_url(text: str, end: int, url: str) -> tuple[int, str]:
    """Extend a URL across spaces left by line breaks. A following token belongs to the URL when
    it starts with URL punctuation, or when the URL so far ends in a separator (or is only a
    scheme) and the token looks like a path or host."""
    for _ in range(4):
        m = _URL_CONT.match(text, end)
        if not m:
            break
        tok = m.group(1)
        starts = tok[0] in "/.-_?#=&%~"
        open_end = url.endswith(("/", "-", "_", ".", "=", "?", "&", "://"))
        if not (starts or (open_end and re.search(r"[/.]", tok) and not tok.endswith(":"))):
            break
        stripped = _strip_url(tok)
        if not stripped:
            break
        url += stripped
        end = m.start(1) + len(stripped)
        if stripped != tok:
            break
    return end, url


def _overlaps(spans: list[Span], s: int, e: int) -> bool:
    return any(s < b and a < e for a, b, _, _ in spans)


def find_links(text: str, ctx: LinkContext, *, citations: bool = True,
               callouts: bool = True) -> list[Span]:
    spans: list[Span] = []
    if ctx.ext_links:
        for m in _URL.finditer(text):
            u = _strip_url(m.group(0))
            end, u = _continue_url(text, m.start() + len(u), u)
            if len(u) < 11 or _overlaps(spans, m.start(), end):
                continue
            href = u if u.lower().startswith("http") else "https://" + u
            spans.append((m.start(), end, "ext-link", {"ext-link-type": "uri", HREF: href}))
        for m in _DOI.finditer(text):
            d = _strip_url(m.group(1))
            s, e = m.start(), m.start(1) + len(d)
            e, d = _continue_url(text, e, d)
            if not _overlaps(spans, s, e):
                spans.append((s, e, "ext-link", {"ext-link-type": "doi",
                                                 HREF: f"https://doi.org/{d}"}))
    if citations and ctx.mode == "author-year":
        _author_year_links(text, ctx, spans)
    elif citations and ctx.mode == "numeric":
        for m in _BRACKET.finditer(text):
            rids = [ctx.by_number[n] for n in _expand(m.group(1)) if n in ctx.by_number]
            if rids and not _overlaps(spans, m.start(), m.end()):
                spans.append((m.start(), m.end(), "xref",
                              {"ref-type": "bibr", "rid": " ".join(dict.fromkeys(rids))}))
    if callouts and ctx.callouts and ctx.targets:
        for m in _CALLOUT.finditer(text):
            tgt = ctx.targets.get(label_key(f"{m.group(1)} {m.group(2)}"))
            if tgt and not _overlaps(spans, m.start(), m.end()):
                spans.append((m.start(), m.end(), "xref",
                              {"ref-type": _REF_TYPES.get(tgt[1], tgt[1]), "rid": tgt[0]}))
    return sorted(spans)


_NOTE_CALL = re.compile(r"\s*(?:\d{1,3}(?:\s*[–-]\s*\d{1,3})?(?:\s*,?\s*|$))+|\s*[*†‡§¶]{1,3}\s*")
_NOTE_TOKEN = re.compile(r"\d{1,3}(?:\s*[–-]\s*\d{1,3})?|[*†‡§¶]{1,3}")


def _note_links(segs: Sequence[tuple[str, Fmt]], ctx: LinkContext, spans: list[Span]) -> None:
    """Superscript note numbers ("…in 2019.³", "³ ⁴", "²⁻⁴") → an ``xref ref-type="fn"`` per
    number (a range is one link to each note in it)."""
    pos = 0
    for t, f in segs:
        if f.sup and t.strip() and _NOTE_CALL.fullmatch(t):
            for m in _NOTE_TOKEN.finditer(t):
                spec = m.group(0)
                keys = [spec] if not spec[0].isdigit() else [str(n) for n in _expand(spec)]
                rids = [ctx.notes[k] for k in keys if k in ctx.notes]
                s, e = pos + m.start(), pos + m.end()
                if rids and not _overlaps(spans, s, e):
                    spans.append((s, e, "xref", {"ref-type": "fn",
                                                 "rid": " ".join(dict.fromkeys(rids))}))
        pos += len(t)


def _bibr(spans: list[Span], s: int, e: int, rid: str) -> None:
    if not _overlaps(spans, s, e):
        spans.append((s, e, "xref", {"ref-type": "bibr", "rid": rid}))


def _extra_years(ctx: LinkContext, spans: list[Span], text: str, pos: int, end: int,
                 name: str, year: str) -> None:
    """Link each further year of one author group on its own: "Smith, 2019, 2020b" and
    "Smith, 2019a, b" (a bare letter repeats the year before it)."""
    while (m := _MORE_YEARS.match(text, pos, end)) is not None:
        y = m.group("y")
        if len(y) == 1:
            y = year[:4] + y
        hit = _resolve_at(ctx, name, y)
        if hit:
            _bibr(spans, m.start("y"), m.end("y"), hit[0])
        else:
            _unresolved(ctx, name, y)
        year = y
        pos = m.end()


def _unresolved(ctx: LinkContext, name: str, year: str) -> None:
    key = " ".join(name.split()) + " " + year
    ctx.unresolved[key] = ctx.unresolved.get(key, 0) + 1


def _author_year_links(text: str, ctx: LinkContext, spans: list[Span]) -> None:
    """Parenthetical "(Smith, 2019; Lee & Park, 2020a, b)" and narrative "Smith et al. (2019)"
    citations: one ``xref`` per author–year pair, each pointing at its own reference."""
    for m in _PAREN.finditer(text):
        base = m.start(1)
        pos = 0
        for part in m.group(1).split(";"):
            pm = _PART.match(part)
            if pm:
                name, year = pm.group("name"), pm.group("year")
                s, e = base + pos + pm.start("name"), base + pos + pm.end("year")
                hit = _resolve_at(ctx, name, year)
                if hit:
                    _bibr(spans, s, e, hit[0])
                elif _looks_like_author(name):
                    _unresolved(ctx, name, year)
                _extra_years(ctx, spans, text, e, base + pos + len(part), name,
                             year.replace(" ", ""))
            pos += len(part) + 1
    for m in _NARRATIVE.finditer(text):
        name, year = m.group("name"), m.group("year")
        if _overlaps(spans, m.start(), m.end()):
            continue
        hit = _resolve_at(ctx, name, year)
        if hit is None:
            if _looks_like_author(name) and not m.group("more").strip(" ,").isalpha():
                _unresolved(ctx, name, year)
            continue
        rid, off = hit
        more = m.group("more")
        if re.match(r"\s*,\s*(?:(?:19|20)\d{2}|[a-z]\b)", more):
            # "Smith (2019, 2020)": the name and first year, then each further year
            _bibr(spans, m.start("name") + off, m.end("year"), rid)
            _extra_years(ctx, spans, text, m.end("year"), m.end() - 1, name, year)
        else:
            _bibr(spans, m.start("name") + off, m.end(), rid)


def _looks_like_author(name: str) -> bool:
    """A name worth reporting when it matches no reference: capitalised words, no digits."""
    return bool(re.fullmatch(r"[A-Z][\w'’.\-]*(?:[ ,&]+(?:and|et al\.?|[A-Z][\w'’.\-]*))*",
                             name.strip()))


def _expand(spec: str) -> list[int]:
    """"1, 3–5" → [1, 3, 4, 5]."""
    out: list[int] = []
    for part in re.split(r"\s*,\s*", spec.strip()):
        m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if m:
            lo, hi = int(m[1]), int(m[2])
            if 0 < hi - lo < 50:
                out.extend(range(lo, hi + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _resolve(ctx: LinkContext, name: str, year: str) -> str | None:
    hit = _resolve_at(ctx, name, year)
    return hit[0] if hit else None


def _resolve_at(ctx: LinkContext, name: str, year: str) -> tuple[str, int] | None:
    """The reference cited as ``name``/``year`` and where in ``name`` the author starts
    (words before it, as in "Recently Smith (2019)", are not part of the citation)."""
    y = year.replace(" ", "").lower()
    # the first author: "Smith and Lee" / "Smith, Lee, & Park" / "Smith et al."
    first = re.split(r"\s*,\s*|\s+(?:&|and)\s+", _ETAL_RE.sub("", name).strip(), maxsplit=1)[0]
    found = list(re.finditer(r"[A-Za-z][A-Za-z'’\-]*", first))
    if not found:
        return None
    words = [m.group(0) for m in found]
    for k in range(len(words)):
        sub = [w.lower() for w in words[k:]]
        for key in (sub[0], " ".join(sub), "".join(sub)):
            rid = ctx.by_key.get((key, y))
            if rid:
                return rid, found[k].start()
    initial = words[0]
    if initial.isupper() and len(initial) >= 2:   # "CDC" for "Centers for Disease Control …"
        cands = [rid for (k, yy), rid in ctx.by_key.items()
                 if yy == y and k.startswith(initial.lower()) and " " not in k]
        if cands:
            return cands[0], found[0].start()
    return None


# ---------------------------------------------------------------- entry

def emit(parent: etree._Element, runs: Sequence[Run], style: Style | None, ctx: LinkContext, *,
         suppress: frozenset[str] = frozenset(), drop: frozenset[str] = frozenset(),
         citations: bool = True, callouts: bool = True, links: bool = True,
         marks: Sequence[tuple[int, etree._Element]] = ()) -> None:
    """Append ``runs`` to ``parent`` as JATS inline content.

    ``marks``: empty elements (page-break targets) to place at character offsets of the text;
    one that falls inside a link goes right after it.
    """
    segs = segments(runs, style, suppress, drop)
    text = "".join(t for t, _ in segs)
    spans = find_links(text, ctx, citations=citations, callouts=callouts) if links else []
    if links and callouts and ctx.notes:
        _note_links(segs, ctx, spans)
    # explicit link targets carried on runs
    pos = 0
    for r in runs:
        if r.link and r.text and not _overlaps(spans, pos, pos + len(r.text)):
            spans.append((pos, pos + len(r.text), "ext-link", {"ext-link-type": "uri",
                                                               HREF: r.link}))
        pos += len(r.text)
    spans.sort()
    for _, _, kind, _ in spans:
        ctx.stats[kind] += 1

    bounds: list[tuple[int, int, etree._Element]] = []
    cursor = 0
    for s, e, kind, attrs in spans:
        if s > cursor:
            bounds.append((cursor, s, parent))
        el = etree.Element(kind, attrs)
        bounds.append((s, e, el))
        cursor = e
    if cursor < len(text):
        bounds.append((cursor, len(text), parent))

    offsets = []
    o = 0
    for t, f in segs:
        offsets.append((o, o + len(t), t, f))
        o += len(t)

    def write(target: etree._Element, s: int, e: int) -> None:
        for a, b, t, f in offsets:
            lo, hi = max(a, s), min(b, e)
            if lo < hi:
                _write_piece(target, t[lo - a:hi - a], f, ctx.keep_visual)

    placed: list[tuple[int, etree._Element]] = []
    for off, el in marks:
        for s, e, _, _ in spans:
            if s < off < e:
                off = e
                break
        placed.append((min(max(off, 0), len(text)), el))
    placed.sort(key=lambda m: m[0])
    mi = 0
    for s, e, target in bounds:
        while mi < len(placed) and placed[mi][0] <= s:
            parent.append(placed[mi][1])
            mi += 1
        if target is not parent:
            parent.append(target)
            write(target, s, e)
            continue
        pos = s
        while mi < len(placed) and placed[mi][0] < e:
            write(parent, pos, placed[mi][0])
            parent.append(placed[mi][1])
            pos = placed[mi][0]
            mi += 1
        write(parent, pos, e)
    for _, el in placed[mi:]:
        parent.append(el)


def emit_tagged(parent: etree._Element, runs: Sequence[Run], style: Style | None,
                ctx: LinkContext, spans: Sequence[CSpan]) -> None:
    """Append ``runs`` with the element tree of a parsed reference (``citations.Span``). Text
    inside an element is written without typographic markup (``<source>`` says what the italics
    said); sub/superscripts are kept, and so is the formatting of text between elements."""
    segs = segments(runs, style)
    offsets = []
    o = 0
    for t, f in segs:
        offsets.append((o, o + len(t), t, f))
        o += len(t)

    def write(target: etree._Element, s: int, e: int, plain: bool) -> None:
        for a, b, t, f in offsets:
            lo, hi = max(a, s), min(b, e)
            if lo < hi:
                _write_piece(target, t[lo - a:hi - a], Fmt(sup=f.sup, sub=f.sub) if plain else f,
                             ctx.keep_visual)

    def fill(target: etree._Element, s: int, e: int, children: Sequence[CSpan],
             plain: bool) -> None:
        pos = s
        for sp in children:
            write(target, pos, sp.start, plain)
            el = etree.SubElement(target, sp.tag, sp.attrs)
            ctx.stats[sp.tag] += sp.tag in ("ext-link", "pub-id")
            fill(el, sp.start, sp.end, sp.children, True)
            pos = sp.end
        write(target, pos, e, plain)

    fill(parent, 0, o, spans, False)


def plain_text(runs: Sequence[Run]) -> str:
    return re.sub(r"\s+", " ", "".join(r.text for r in runs)).strip()
