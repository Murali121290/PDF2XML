"""Front and back matter: the parts of a book around its chapters.

Pure helpers for ``semantic``: which part a heading names (Contents, Preface, Glossary, Index …),
printed page numbers in arabic or roman figures, and readers for a printed table of contents, a
glossary, a back-of-book index, the copyright page and the title page. Nothing here knows about
the XML; the results are plain dataclasses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pdf2xml.model import (
    Caption,
    Code,
    Heading,
    ListBlock,
    ListItem,
    Paragraph,
    Run,
    Style,
    Table,
)

BBox = tuple[float, float, float, float]

# ---------------------------------------------------------------- run helpers


def runs_text(runs: Sequence[Run]) -> str:
    return "".join(r.text for r in runs)


def strip_prefix(runs: Sequence[Run], n: int) -> list[Run]:
    """Remove the first ``n`` characters (across runs) and the whitespace after them."""
    out: list[Run] = []
    for r in runs:
        if n >= len(r.text):
            n -= len(r.text)
            continue
        out.append(r.model_copy(update={"text": r.text[n:]}) if n else r)
        n = 0
    while out and not out[0].text.strip():
        out.pop(0)
    if out:
        out[0] = out[0].model_copy(update={"text": out[0].text.lstrip()})
    return out


def slice_runs(runs: Sequence[Run], start: int, end: int) -> list[Run]:
    """Characters ``start:end`` of the joined text, keeping run formatting."""
    out: list[Run] = []
    pos = 0
    for r in runs:
        a, b = max(start, pos), min(end, pos + len(r.text))
        if a < b:
            out.append(r.model_copy(update={"text": r.text[a - pos:b - pos]}))
        pos += len(r.text)
    return out


def trim_runs(runs: Sequence[Run]) -> list[Run]:
    out = [r for r in runs if r.text]
    if out:
        out[0] = out[0].model_copy(update={"text": out[0].text.lstrip()})
        out[-1] = out[-1].model_copy(update={"text": out[-1].text.rstrip()})
    return [r for r in out if r.text]


def join_runs(a: Sequence[Run], b: Sequence[Run], sep: str = " ") -> list[Run]:
    """``a`` + ``sep`` + ``b``, trimmed where they meet."""
    a, b = trim_runs(a), trim_runs(b)
    if not a:
        return list(b)
    if not b:
        return list(a)
    last = a[-1].model_copy(update={"text": a[-1].text + sep})
    return [*a[:-1], last, *b]


def despace(text: str) -> str:
    """Undo letter-spacing: "C O N T E N T S" → "CONTENTS", "S E C T I O N 1 0" → "SECTION 10"."""
    t = " ".join(text.split())
    if re.fullmatch(r"(?:\S ){2,}\S", t):
        return re.sub(r"(?<=[A-Za-z])(?=\d)", " ", t.replace(" ", ""))
    # "S E C T I O N 1" style runs inside longer text
    return re.sub(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b", lambda m: m.group(0).replace(" ", ""), t)


def plain(text: str) -> str:
    """Lower-case letters and single spaces only: the words of a heading."""
    return re.sub(r"[^a-z ]+", "", re.sub(r"\s+", " ", despace(text).lower())).strip()


def key(text: str) -> str:
    """Letters and digits only, for comparing titles printed in different places."""
    return re.sub(r"[^a-z0-9]+", "", despace(text).lower())


def is_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


# ---------------------------------------------------------------- names

_PARTICLES = {"de", "van", "von", "der", "den", "da", "di", "del", "la", "le", "du", "bin", "al"}


def names(text: str) -> list[tuple[str, str]]:
    """"Ann B. Smith, PhD, and Carl Doe" → [("Ann B.", "Smith"), ("Carl", "Doe")]; [] otherwise."""
    text = text.strip()
    if not text or len(text) > 200 or re.search(r"\d|[;:!?]", text) or text.endswith("."):
        return []
    out: list[tuple[str, str]] = []
    for part in re.split(r"\s*,\s*(?:and\s+|&\s+)?|\s+(?:and|&)\s+", text):
        toks = part.split()
        if not toks:
            continue
        if len(toks) == 1 and re.fullmatch(r"[A-Z][A-Za-z]{1,5}", toks[0]) and (
                toks[0].isupper() or toks[0] in ("PhD", "MSc", "MPH", "DNP", "MD")):
            continue                                   # credentials: PhD, RN, FAAN
        if not 2 <= len(toks) <= 5 or not all(
                re.fullmatch(r"[A-Z][\w'’.\-]*", t) or t.lower() in _PARTICLES for t in toks):
            return []
        out.append((" ".join(toks[:-1]), toks[-1]))
    return out


_ROLE_BY = re.compile(
    r"\b(?P<role>(?i:edited|compiled|translated|selected|introduced|written|illustrated))?"
    r"(?:[\w ,]{0,60}?)\bby\s+(?P<who>[A-Z][^,;:]*?)(?=\s+(?:and|with)\s+[a-z]|\s*[,;:]|\s*$)")
_ROLES = {"edited": "editor", "compiled": "compiler", "translated": "translator",
          "illustrated": "illustrator"}


def bylines(text: str) -> list[tuple[str, str, str]]:
    """People named on a title page: "Edited by D. J. Huppatz" → [("D. J.", "Huppatz", "editor")];
    "With introduction by A and commentary by B" names both."""
    out: list[tuple[str, str, str]] = []
    for m in _ROLE_BY.finditer(" ".join(text.split())):
        role = _ROLES.get((m.group("role") or "").lower(), "author")
        for given, surname in names(m.group("who")):
            out.append((given, surname, role))
    return out


# ---------------------------------------------------------------- kinds of matter

FRONT_KINDS = frozenset({"toc", "dedication", "foreword", "preface"})
BACK_KINDS = frozenset({"app", "index", "notes", "further"})
ANY_KINDS = frozenset({"ack", "bio", "glossary"})

_KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("toc", r"(table of )?contents?( in brief| at a glance| overview)?"
            r"|(brief|detailed|summary of|short|full|expanded|analytical) contents"),
    ("dedication", r"dedications?"),
    ("foreword", r"forewords?( to .*)?"),
    ("preface", r"preface( to .*)?|(authors?|editors?)s? preface"),
    ("ack", r"acknowledge?ments?|acknowledge?ments? and .*"),
    ("glossary", r"glossary( of .*)?|(list of )?(abbreviations|acronyms)( and (symbols|acronyms))?"
                 r"|(key )?terms and definitions|definitions of terms"),
    ("bio", r"about the (authors?|editors?|contributors?)|(list of |notes on (the )?)?contributors"
            r"|contributor details|author biographies|biographies|about this author"
            r"|editors? and contributors"),
    ("app", r"appendix( [a-z]| [ivx]+)?( .*)?|appendices"),
    ("index", r"(general |subject |author |name |names |topic |topical )?index"
              r"( of (names|subjects|authors|terms|places))?"),
    ("notes", r"(end ?)?notes|notes to (the )?chapters?|chapter notes"),
    ("further", r"(further|suggested|recommended|additional|selected) (reading|readings|resources)"
                r"|for further reading"),
)
_KIND_RE = [(k, re.compile(rf"^(?:{p})$")) for k, p in _KIND_PATTERNS]


def kind_of(text: str) -> str | None:
    """The kind of front or back matter a heading names, or None for an ordinary heading."""
    t = plain(text)
    if not t or len(t) > 60:
        return None
    for k, rx in _KIND_RE:
        if rx.match(t):
            return k
    return None


# "CHAPTER FOURTEEN", "S E C T I O N 1", "Part II": a label printed as a heading of its own
_NUM_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
              "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
              "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty"]
_LABEL_WORD = r"(?:chapter|part|section|unit|module|lesson|book|appendix|volume)"
_LABEL_NUM = (r"(?:\d{1,3}(?:\.\d{1,3})*|[ivxlc]{1,6}|[a-z]|(?:" + "|".join(_NUM_WORDS)
              + r")(?:[- ](?:" + "|".join(_NUM_WORDS[:9]) + r"))?)")
# also a bare chapter number ("4") printed above its title
LABEL_ONLY = re.compile(rf"^\s*(?:{_LABEL_WORD}\s+{_LABEL_NUM}\s*[.:]?|\d{{1,3}})\s*$", re.I)
# a label in front of a title: "Chapter 3 Methods", "3.2 Results", "IV. Discussion"
_ENTRY_LABEL = re.compile(
    rf"^\s*(?P<label>{_LABEL_WORD}\s+{_LABEL_NUM}\b[.:]?|\d{{1,3}}(?:\.\d{{1,3}})*\.?"
    r"|[IVXLC]{1,6}\.)\s+(?P<rest>\S.*)$", re.I)
# a division above chapters ("Part II", "Unit 3"); "Section" is not one: books use it for the
# level below a chapter as often as above it
DIVISION = re.compile(r"^\s*(?:part|unit|book|volume)\b", re.I)


def label_only(text: str) -> bool:
    return bool(LABEL_ONLY.match(despace(text)))


# ---------------------------------------------------------------- page numbers

_ROMAN_RE = re.compile(r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_ROMAN_VAL = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
FOLIO = r"(?:\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})"


def roman_value(s: str) -> int | None:
    t = s.strip().lower()
    if not t or not _ROMAN_RE.match(t):
        return None
    total = 0
    for a, b in zip(t, t[1:] + " ", strict=True):
        v = _ROMAN_VAL[a]
        total += -v if b != " " and _ROMAN_VAL[b] > v else v
    return total


def to_roman(n: int) -> str:
    out = ""
    for v, s in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                 (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += s
            n -= v
    return out


def folio(text: str) -> tuple[str, int] | None:
    """("arabic", 12) for "12", ("roman", 7) for "vii"; None for anything else."""
    t = text.strip()
    if re.fullmatch(r"\d{1,4}", t):
        return "arabic", int(t)
    if re.fullmatch(r"[ivxlcdm]{1,7}|[IVXLCDM]{1,7}", t):
        v = roman_value(t)
        if v:
            return "roman", v
    return None


# ---------------------------------------------------------------- table of contents

@dataclass
class TocEntry:
    title: list[Run]
    label: str | None = None
    folio: str | None = None            # the printed page number
    level: int = 1
    contributors: list[str] = field(default_factory=list)
    children: list[TocEntry] = field(default_factory=list)
    page: int = 0                       # PDF page the entry is printed on
    division: bool = False              # "Part 2 …": holds the entries after it
    style: str | None = None
    target: str | None = None           # XML id it points at, once ids exist
    target_obj: object | None = None    # the node it points at (a chapter, section …)
    indent: float = 0.0
    merged: bool = False                # split out of a block of several printed lines
    src: int = -1                       # the block it was printed in

    @property
    def text(self) -> str:
        return " ".join(runs_text(self.title).split())


@dataclass
class _Line:
    runs: list[Run]
    page: int
    bbox: BBox
    style: str | None
    marker: str | None = None
    cells: list[list[Run]] | None = None
    folio: str | None = None
    label: str | None = None
    heading: bool = False
    src: int = 0                        # index of the block it came from
    merged: bool = False                # one of several printed lines the engine ran together

    @property
    def text(self) -> str:
        return " ".join(runs_text(self.runs).split())


TocBlock = Heading | Paragraph | Caption | ListBlock | Table | Code

_LEADERS = r"(?:\s*(?:\.\s?){2,}|\s*[·•…_]{2,}|\s*…+)"
_TRAILING = re.compile(rf"^(?P<t>.*?\S)(?:{_LEADERS}\s*|\s+)(?P<f>{FOLIO})\s*$")
_NOT_TITLE_END = re.compile(r"\b(?:part|chapter|volume|vol|book|section|unit|no|edition)\.?$",
                            re.I)
_END_PUNCT = re.compile(r"[.!?;]\s*$")


def _lines(blocks: Sequence[TocBlock]) -> list[_Line]:
    out: list[_Line] = []

    def items(lb: ListBlock, src: int) -> None:
        for it in lb.items:
            out.append(_Line(list(it.runs), it.page, it.bbox, it.style, marker=it.marker,
                             src=src))
            for ch in it.children:
                if isinstance(ch, ListBlock):
                    items(ch, src)
                elif isinstance(ch, Paragraph | ListItem):
                    out.append(_Line(list(ch.runs), ch.page, ch.bbox, ch.style, src=src))

    for i, b in enumerate(blocks):
        if isinstance(b, ListBlock):
            items(b, i)
        elif isinstance(b, Table):
            for row in b.rows:
                cells = [list(c.runs) for c in row if runs_text(c.runs).strip()]
                if cells:
                    out.append(_Line([r for c in cells for r in c], b.page, b.bbox, None,
                                     cells=cells, src=i))
        else:
            out.append(_Line(list(b.runs), b.page, b.bbox, b.style,
                             heading=isinstance(b, Heading), src=i))
    return _split_merged(out)


# a page number that ends one entry and the start of the next: "… Overview 2 Section 1. …"
_ENTRY_BREAK = re.compile(r"(?<=[A-Za-z)?’”:])\s+(\d{1,4}|[ivxlc]{1,7})\s+(?=[A-Z“\"‘(])")


def _split_merged(lines: list[_Line]) -> list[_Line]:
    """Lines of a contents page that the engine ran together into one block: split at line
    breaks kept in the text (a block read as code), and in a block taller than one line, after
    each page number that is followed by the start of the next entry."""
    heights = sorted(ln.bbox[3] - ln.bbox[1] for ln in lines
                     if ln.cells is None and ln.bbox[3] - ln.bbox[1] > 3)
    one = heights[0] if heights else 0.0
    out: list[_Line] = []
    for ln in lines:
        if ln.cells is not None:
            out.append(ln)
            continue
        joined = runs_text(ln.runs)
        cuts: list[int] = [m.end() for m in re.finditer(r"\n", joined)]
        tall = one > 0 and ln.bbox[3] - ln.bbox[1] > 1.7 * one
        if tall or cuts:
            for m in _ENTRY_BREAK.finditer(joined):
                if folio(m.group(1)):
                    cuts.append(m.end(1))
        cuts = sorted(set(c for c in cuts if 0 < c < len(joined)))
        if not cuts:
            out.append(ln)
            continue
        for a, b in zip([0, *cuts], [*cuts, len(joined)], strict=True):
            piece = trim_runs(slice_runs(ln.runs, a, b))
            if piece and runs_text(piece).strip():
                out.append(_Line(piece, ln.page, ln.bbox, ln.style, src=ln.src,
                                 heading=ln.heading, merged=True))
    return out


def split_trailing(text: str) -> tuple[str, str] | None:
    """"Introduction ........ 12" → ("Introduction", "12"); a title must stay behind."""
    m = _TRAILING.match(text)
    f = folio(m.group("f")) if m else None
    if m is None or f is None:
        return None
    t = m.group("t").strip()
    leaders = bool(re.search(_LEADERS + r"\s*" + FOLIO + r"\s*$", text))
    # front-matter pages are numbered i … lx in lower case: "Malcolm X" is not a page number
    if f[0] == "roman" and (f[1] > 60 or (not leaders and not m.group("f").islower())):
        return None
    if not re.search(r"[A-Za-z]{2}", t) or _NOT_TITLE_END.search(t):
        return None
    if not leaders and len(t.split()) < 2 and not re.match(r"^[A-Z][a-z]", t):
        return None
    return t, m.group("f")


def _pair_columns(lines: list[_Line]) -> list[_Line]:
    """Page numbers (or labels) set in their own column: attach each to the text on its line."""
    loose = [ln for ln in lines if ln.cells is None and folio(ln.text)]
    rest = [ln for ln in lines if ln not in loose]
    dropped: set[int] = set()
    for f in loose:
        fy0, fy1 = f.bbox[1], f.bbox[3]

        def overlap(t: _Line, fy0: float = fy0, fy1: float = fy1) -> float:
            return min(t.bbox[3], fy1 + 2) - max(t.bbox[1], fy0 - 2)

        same = [t for t in rest if t.page == f.page and overlap(t) > 0]
        right = [t for t in same if t.bbox[0] >= f.bbox[2] - 2 and t.bbox[0] - f.bbox[2] < 60
                 and t.label is None and not folio(t.text)]
        left = [t for t in same if t.bbox[2] <= f.bbox[0] + 2 and t.folio is None]
        if right and f.text.isdigit():
            t = min(right, key=lambda t: t.bbox[0] - f.bbox[2])
            t.label = f.text
            t.bbox = (f.bbox[0], t.bbox[1], t.bbox[2], t.bbox[3])   # the line starts at its label
            dropped.add(id(f))
        elif left:
            max(left, key=lambda t: (overlap(t), t.bbox[2])).folio = f.text
            dropped.add(id(f))
    return [ln for ln in lines if id(ln) not in dropped]


def _page_order(lines: list[_Line]) -> list[_Line]:
    """Printed order on a one-column contents page: top to bottom (a layout engine can read a
    block out of place). Lines split from one block keep their order; two-column pages and
    tables are left as read."""
    out: list[_Line] = []
    k = 0
    while k < len(lines):
        j = k
        while j < len(lines) and lines[j].page == lines[k].page:
            j += 1
        page = lines[k:j]
        one_column = all(ln.cells is None for ln in page) and not _side_by_side(page)
        out.extend(sorted(page, key=lambda ln: ln.bbox[1]) if one_column else page)
        k = j
    return out


def _side_by_side(lines: Sequence[_Line]) -> bool:
    """Two different blocks at the same height, one beside the other: two columns."""
    for a in lines:
        for b in lines:
            if a.src >= b.src:
                continue
            h = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
            overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
            apart = a.bbox[2] <= b.bbox[0] or b.bbox[2] <= a.bbox[0]
            if h > 0 and overlap > 0.5 * h and apart:
                return True
    return False


@dataclass
class ParsedToc:
    entries: list[TocEntry]
    n_lines: int = 0
    unpaired: int = 0               # page numbers that could not be matched to a title


def _entry_label(text: str) -> tuple[str | None, str]:
    m = _ENTRY_LABEL.match(text)
    if m and len(m.group("rest")) >= 2:
        return m.group("label").rstrip(".:").strip(), m.group("rest")
    return None, text


def _row_entry(ln: _Line) -> TocEntry:
    cells = ln.cells or []
    texts = [" ".join(runs_text(c).split()) for c in cells]
    f_at = next((i for i in range(len(texts) - 1, -1, -1) if folio(texts[i])), None)
    fol = texts[f_at] if f_at is not None and f_at > 0 else None
    body = [c for i, c in enumerate(cells) if i != f_at or fol is None]
    btexts = [t for i, t in enumerate(texts) if i != f_at or fol is None]
    label = None
    if len(body) >= 2 and (re.fullmatch(r"\d{1,3}(?:\.\d{1,3})*\.?|[IVXLC]{1,6}\.?", btexts[0])
                           or LABEL_ONLY.match(btexts[0])):
        label, body = btexts[0].rstrip("."), body[1:]
    title: list[Run] = []
    for c in body:
        title = join_runs(title, c)
    if fol is None:
        st = split_trailing(runs_text(title))
        if st:
            fol = st[1]
            title = trim_runs(slice_runs(title, 0, len(st[0])))
    return TocEntry(title, label, fol, page=ln.page)


def parse_toc(blocks: Sequence[TocBlock], toc_title: str = "contents") -> ParsedToc:
    """Entries of a printed table of contents: title, label, printed page number, level and the
    authors printed under a chapter title (edited volumes)."""
    lines = _page_order(_pair_columns(_lines(blocks)))
    n_lines = len(lines)
    unpaired = sum(1 for ln in lines if ln.cells is None and folio(ln.text))
    lines = [ln for ln in lines if not (ln.cells is None and folio(ln.text))]
    tkey = key(toc_title)
    flat: list[TocEntry] = []
    prefix: list[_Line] = []
    for i, ln in enumerate(lines):
        text = ln.text
        if not text or key(text) in (tkey, "contents", "tableofcontents"):
            continue                                    # the title, repeated as a running head
        if ln.cells is not None:
            e = _row_entry(ln)
        else:
            fol = ln.folio
            title_text = text
            if fol is None:
                st = split_trailing(text)
                if st:
                    title_text, fol = st
            if fol is None:
                nxt = lines[i + 1] if i + 1 < len(lines) else None
                nxt_has = nxt is not None and (nxt.folio is not None or (
                    nxt.cells is None and split_trailing(nxt.text) is not None))
                if (flat and not prefix and names(text) and len(text.split()) <= 8
                        and not flat[-1].division):
                    flat[-1].contributors.append(text)  # the chapter's authors
                    continue
                if (nxt_has and nxt is not None and nxt.page == ln.page
                        and not _END_PUNCT.search(text) and not DIVISION.match(despace(text))
                        and _entry_label(nxt.text)[0] is None and len(prefix) < 3):
                    prefix.append(ln)                   # a title that wraps onto the next line
                    continue
            runs = list(ln.runs)
            if fol is not None and title_text != text:
                runs = trim_runs(_cut_to(runs, title_text))
            for p in reversed(prefix):
                runs = join_runs(p.runs, runs)
            label = ln.label
            if ln.marker and re.fullmatch(r"\(?\d{1,3}[.)]?|[IVXLC]{1,6}\.", ln.marker.strip()):
                label = ln.marker.strip().strip("().")
            lead = prefix[0] if prefix else ln
            prefix = []
            if label is None:
                lab, rest = _entry_label(runs_text(runs))
                if lab:
                    label = lab
                    runs = strip_prefix(runs, len(runs_text(runs)) - len(rest))
            division = fol is None and bool(
                DIVISION.match(despace((label or "") + " " + runs_text(runs)).strip()))
            e = TocEntry(runs, label, fol, page=ln.page, division=division, style=ln.style,
                         indent=lead.bbox[0], merged=lead.merged, src=lead.src)
        if e.title or e.label:
            flat.append(e)
    _levels(flat, lines)
    return ParsedToc(_tree(flat), n_lines, unpaired)


def _cut_to(runs: list[Run], text: str) -> list[Run]:
    """The runs up to where ``text`` (the joined text, whitespace-normalised) ends."""
    joined = runs_text(runs)
    words = text.split()
    pos = 0
    for w in words:
        pos = joined.find(w, pos)
        if pos < 0:
            return runs
        pos += len(w)
    return slice_runs(runs, 0, pos)


def _levels(flat: list[TocEntry], lines: list[_Line]) -> None:
    """Level of each entry: the depth of a dotted label ("2.1" → 2); else, for a labelled
    entry, the typical indent of its kind of label ("PART #", "#", "Section #": right-aligned
    numbers make "10" start left of "7"); else its own indent. Entries after a division
    ("Part 1" without a page) sit one level below it. An unlabelled line split out of a block
    of several lines sits below the labelled line before it in that block."""
    placed = [e for e in flat if e.indent and not e.division]
    # Facing pages have different margins: shift each page so that entries with the same kind
    # of label ("3", "Section 2.", "PART IV") line up with where that kind first appeared.
    # Word labels ("PART VI", "Section 2.") are set flush left; bare numbers are often
    # right-aligned ("14" starts left of "1"), so they only count on a page without words.
    ref: dict[str, float] = {}
    word_shifts: dict[int, list[float]] = {}
    num_shifts: dict[int, list[float]] = {}
    for e in placed:
        if e.label and not e.merged:
            k = _label_kind(e.label)
            ref.setdefault(k, e.indent)
            bucket = word_shifts if re.search(r"[a-z]", k) else num_shifts
            bucket.setdefault(e.page, []).append(e.indent - ref[k])
    shifts = {pg: word_shifts.get(pg) or num_shifts.get(pg, [])
              for pg in {*word_shifts, *num_shifts}}
    first_min: dict[int, float] = {}
    for e in placed:
        first_min[e.page] = min(first_min.get(e.page, e.indent), e.indent)
    base_page = min(first_min, default=0)

    def shift(page: int) -> float:
        s = sorted(shifts.get(page, []))
        if s:
            return s[len(s) // 2]
        return first_min.get(page, 0.0) - first_min.get(base_page, 0.0)

    corrected: dict[int, float] = {}
    for e in placed:
        if e.merged and e.label and _label_kind(e.label) in ref:
            corrected[id(e)] = ref[_label_kind(e.label)]
        else:
            corrected[id(e)] = e.indent - shift(e.page)
    by_kind: dict[str, list[float]] = {}
    for e in placed:
        if e.label and not e.merged:
            by_kind.setdefault(_label_kind(e.label), []).append(corrected[id(e)])
    kind_pos = {k: sorted(v)[len(v) // 2] for k, v in by_kind.items()}
    pos: dict[int, float] = {}
    for e in placed:
        lk = _label_kind(e.label) if e.label else None
        pos[id(e)] = kind_pos[lk] if lk is not None and lk in kind_pos else corrected[id(e)]
    low = min(pos.values(), default=0.0)
    rel: dict[int, int] = {k: round(v - low) for k, v in pos.items()}
    steps: list[int] = []
    for r in sorted(set(rel.values())):
        if not steps or r - steps[-1] > 8:      # contents indents are ≥ 10 pt apart
            steps.append(r)
    in_div = False
    last: dict[int, int] = {}                   # block → level of its last labelled line
    for e in flat:
        if e.division:
            e.level = 1
            in_div = True
            continue
        if e.label and re.fullmatch(r"\d{1,3}(?:\.\d{1,3})+", e.label):
            lvl = e.label.count(".") + 1
        elif id(e) in rel and steps:
            r = rel[id(e)]
            lvl = 1 + max((i for i, s in enumerate(steps) if r >= s - 3), default=0)
        else:
            lvl = 1
        e.level = min(lvl, 4) + (1 if in_div else 0)
        if e.merged and not e.label and e.src in last:
            e.level = min(last[e.src] + 1, 5)
        if e.label:
            last[e.src] = e.level


def _label_kind(label: str) -> str:
    """"Section 12" → "section #", "PART IV" → "part #", "7" → "#"."""
    return re.sub(r"\b(?:\d+|[ivxlc]+)\b", "#", label.lower().rstrip(".:"))


def _tree(flat: list[TocEntry]) -> list[TocEntry]:
    roots: list[TocEntry] = []
    stack: list[TocEntry] = []
    for e in flat:
        while stack and stack[-1].level >= e.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(e)
        stack.append(e)
    return roots


def walk_toc(entries: Sequence[TocEntry]) -> list[TocEntry]:
    out: list[TocEntry] = []
    for e in entries:
        out.append(e)
        out.extend(walk_toc(e.children))
    return out


def toc_lines_with_folio(blocks: Sequence[TocBlock]) -> tuple[int, int]:
    """(lines that end in a page number, lines) on a page: how much it looks like a contents
    page."""
    lines = _pair_columns(_lines(blocks))
    n = 0
    total = 0
    for ln in lines:
        if not ln.text:
            continue
        total += 1
        if ln.cells is not None:
            n += any(folio(runs_text(c)) for c in ln.cells[1:])
        elif ln.folio or split_trailing(ln.text):
            n += 1
    return n, total


# ---------------------------------------------------------------- glossary

@dataclass
class GlossItem:
    term: list[Run]
    defs: list[list[Run]]
    page: int = 0


def _bold_lead(runs: Sequence[Run], style: Style | None) -> int:
    """Length of the bold text a paragraph starts with (0 when it is all bold or not bold)."""
    base = bool(style and style.bold)
    n = 0
    for r in runs:
        b = r.bold if r.bold is not None else base
        if not b:
            break
        n += len(r.text)
    total = len(runs_text(runs))
    return n if 0 < n < total else 0


_GLOSS_SEP = re.compile(r"^(?P<t>[^:–—]{1,80}?)\s*(?::|\s[–—-]\s|\s*[–—]\s*)\s*(?P<d>\S.*)$", re.S)


def glossary(blocks: Sequence[Heading | Paragraph | Caption | ListBlock | Table],
             styles: dict[str, Style]) -> tuple[list[int], list[GlossItem]]:
    """Terms and definitions: two-column tables, "Term: definition" / bold-term paragraphs, and a
    short term on its own line followed by its definition. Returns the indexes of blocks that are
    not part of the list (an introduction) and the items; no items when fewer than two read."""
    items: list[GlossItem] = []
    intro: list[int] = []
    pending_term: list[Run] | None = None
    for i, b in enumerate(blocks):
        if isinstance(b, Table):
            for row in b.rows:
                cells = [list(c.runs) for c in row if runs_text(c.runs).strip()]
                if len(cells) >= 2:
                    d: list[Run] = []
                    for c in cells[1:]:
                        d = join_runs(d, c)
                    items.append(GlossItem(trim_runs(cells[0]), [d], b.page))
                elif cells and items:
                    items[-1].defs.append(trim_runs(cells[0]))
            continue
        paras: list[Paragraph | ListItem | Heading | Caption] = []
        if isinstance(b, ListBlock):
            paras.extend(b.items)
        else:
            paras.append(b)
        for p in paras:
            text = runs_text(p.runs)
            if not text.strip():
                continue
            if isinstance(p, Heading):
                if len(text.strip()) <= 2:
                    continue                        # an "A", "B" … divider
                pending_term = trim_runs(p.runs)
                continue
            if pending_term is not None:
                items.append(GlossItem(pending_term, [trim_runs(p.runs)], p.page))
                pending_term = None
                continue
            style = styles.get(p.style or "")
            n = _bold_lead(p.runs, style)
            m = _GLOSS_SEP.match(text)
            if n and len(text[:n].strip(" :–—-")) <= 80 and text[n:].strip(" :–—-"):
                term = trim_runs(slice_runs(p.runs, 0, n))
                rest = text[n:]
                skip = len(rest) - len(rest.lstrip(" :–—-\t"))
                items.append(GlossItem(_strip_sep(term),
                                       [trim_runs(slice_runs(p.runs, n + skip, len(text)))],
                                       p.page))
            elif m and len(m.group("t").split()) <= 8:
                items.append(GlossItem(trim_runs(slice_runs(p.runs, 0, m.end("t"))),
                                       [trim_runs(slice_runs(p.runs, m.start("d"), len(text)))],
                                       p.page))
            elif items:
                items[-1].defs.append(trim_runs(p.runs))
            else:
                intro.append(i)
    if len(items) < 2:
        return list(range(len(blocks))), []
    return intro, items


def _strip_sep(runs: list[Run]) -> list[Run]:
    if runs:
        runs[-1] = runs[-1].model_copy(update={"text": runs[-1].text.rstrip(" :–—-")})
    return [r for r in runs if r.text]


# ---------------------------------------------------------------- back-of-book index

@dataclass
class IndexEntry:
    term: list[Run]
    locators: list[str] = field(default_factory=list)
    see: list[str] = field(default_factory=list)
    see_also: list[str] = field(default_factory=list)
    children: list[IndexEntry] = field(default_factory=list)
    page: int = 0


@dataclass
class IndexDiv:
    title: str
    entries: list[IndexEntry] = field(default_factory=list)


_LOC = (r"(?:\d{1,4}(?:\s*[–-]\s*\d{1,4})?(?:\s*n\.?\s?\d{1,3})?[fn]?"
        r"|[ivxlc]{1,7}(?:\s*[–-]\s*[ivxlc]{1,7})?)")
_LOCS = re.compile(rf"(?:,\s*|\s+)(?P<l>{_LOC}(?:\s*,\s*{_LOC})*)\s*\.?\s*$")
_SEE = re.compile(r"[.;,]?\s*\(?\b(?P<k>see also|see)\b\s*(?P<v>[^)]*?)\)?\.?\s*$", re.I)
_SPLIT_ENTRIES = re.compile(r"(?<=\d)[.;]?\s+(?!see\b)(?=[A-Za-z(“\"'‘])")


def _parse_index_line(runs: list[Run], page: int) -> IndexEntry | None:
    text = " ".join(runs_text(runs).split())
    if not text:
        return None
    see: list[str] = []
    see_also: list[str] = []
    rest = text
    m = _SEE.search(rest)
    if m and m.start() > 0:
        targets = [t.strip() for t in re.split(r";", m.group("v")) if t.strip()]
        (see_also if m.group("k").lower() == "see also" else see).extend(targets)
        rest = rest[:m.start()].rstrip(" ,.;")
    locs: list[str] = []
    m = _LOCS.search(rest)
    if m and m.start() > 0:
        locs = [x.strip() for x in m.group("l").split(",") if x.strip()]
        rest = rest[:m.start()].rstrip(" ,")
    if not rest:
        return None
    term = trim_runs(_cut_to(runs, rest)) if rest != text else trim_runs(runs)
    return IndexEntry(term, locs, see, see_also, page=page)


def index_entries(blocks: Sequence[Heading | Paragraph | Caption | ListBlock]) -> list[IndexDiv]:
    """Entries of a back-of-book index: term, page numbers, "see"/"see also" cross-references,
    sub-entries (indented), in letter groups when the index prints "A", "B" … dividers."""
    lines = _lines(list(blocks))
    col_min: dict[tuple[int, int], float] = {}
    col_of: dict[int, tuple[int, int]] = {}
    by_page: dict[int, list[_Line]] = {}
    for ln in lines:
        by_page.setdefault(ln.page, []).append(ln)
    for page, lns in by_page.items():
        xs = sorted({round(ln.bbox[0]) for ln in lns})
        starts: list[int] = []
        for x in xs:
            if not starts or x - starts[-1] > 60:
                starts.append(x)
        for ln in lns:
            c = max(i for i, s in enumerate(starts) if round(ln.bbox[0]) >= s)
            col_of[id(ln)] = (page, c)
            col_min[(page, c)] = min(col_min.get((page, c), ln.bbox[0]), ln.bbox[0])
    divs: list[IndexDiv] = [IndexDiv("")]
    last_main: IndexEntry | None = None
    for ln in lines:
        text = ln.text
        if not text:
            continue
        if re.fullmatch(r"[A-Z]|[A-Z]\s*[–-]\s*[A-Z]", text) or (ln.heading and len(text) <= 3):
            divs.append(IndexDiv(text))
            last_main = None
            continue
        pieces = [text]
        if len(text) > 60:
            pieces = [p for p in _SPLIT_ENTRIES.split(text) if p.strip()]
        pos = 0
        joined = runs_text(ln.runs)
        indented = ln.bbox[0] - col_min.get(col_of.get(id(ln), (0, 0)), ln.bbox[0]) > 6
        for k, piece in enumerate(pieces):
            start = joined.find(piece.split()[0], pos) if piece.split() else pos
            end = start + len(piece)
            runs = slice_runs(ln.runs, max(start, 0), end) if len(pieces) > 1 else list(ln.runs)
            pos = end
            sub = (indented and k == 0 and len(pieces) == 1) or bool(
                re.match(r"^\s*[–—-]\s+", piece))
            if sub:
                runs = strip_prefix(runs, len(runs_text(runs))
                                    - len(runs_text(runs).lstrip(" –—-")))
            e = _parse_index_line(runs, ln.page)
            if e is None:
                continue
            if sub and last_main is not None:
                last_main.children.append(e)
            else:
                divs[-1].entries.append(e)
                last_main = e
    return [d for d in divs if d.entries]


# ---------------------------------------------------------------- copyright page

@dataclass
class Isbn:
    value: str
    fmt: str                    # "print" | "electronic"
    kind: str | None = None     # "hardback", "paperback", "epdf", "epub" …


@dataclass
class Imprint:
    isbns: list[Isbn] = field(default_factory=list)
    publisher: str | None = None
    publisher_loc: str | None = None
    year: int | None = None
    copyright: str | None = None
    copyright_year: int | None = None
    edition: str | None = None


_ISBN = re.compile(r"(?<![\d-])(97[89][-\s]?(?:\d[-\s]?){9}\d|(?:\d[-\s]?){9}[\dX])(?![\d-])")
_ISBN_KIND = (
    (r"e-?pdf|pdf", "electronic", "epdf"), (r"e-?pub", "electronic", "epub"),
    (r"e-?book|ebk|electronic|online|kindle|mobi|web pdf", "electronic", "ebook"),
    (r"hb|hbk|hardback|hardcover|cloth|hc", "print", "hardback"),
    (r"pb|pbk|paperback|softcover|paper|sc", "print", "paperback"),
    (r"set", "print", "set"),
)
_IMPRINT_SIGNALS = (
    r"\bisbn\b", r"©|\bcopyright\b", r"\ball rights reserved\b",
    r"\b(first )?published (in|by)\b|\bfirst (published|edition)\b",
    r"\b(british library|library of congress|cataloging|cataloguing)\b",
    r"\b(printed (and bound )?in|typeset (by|in))\b",
)


def imprint_score(text: str) -> int:
    t = text.lower()
    return sum(1 for s in _IMPRINT_SIGNALS if re.search(s, t))


def _isbn_ok(digits: str) -> bool:
    if len(digits) == 13 and digits.isdigit():
        s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
        return (10 - s % 10) % 10 == int(digits[12])
    if len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] == "X"):
        s = sum(int(d) * (10 - i) for i, d in enumerate(digits[:9]))
        check = (11 - s % 11) % 11
        return (str(check) if check < 10 else "X") == digits[9]
    return False


def parse_imprint(lines: Sequence[str]) -> Imprint:
    """ISBNs (with their format), publisher, year, copyright and edition from a copyright page."""
    imp = Imprint()
    text = "\n".join(" ".join(x.split()) for x in lines)
    seen: set[str] = set()
    for im in _ISBN.finditer(text):
        digits = re.sub(r"[-\s]", "", im.group(1))
        if digits in seen or not _isbn_ok(digits):
            continue
        seen.add(digits)
        # the label printed next to this ISBN: "PB: 978…", "978… (pbk.)", "ePDF: 978…"
        before = text[max(0, im.start() - 24):im.start()].lower()
        paren = re.match(r"\s*(\([^)]{1,16}\))", text[im.end():im.end() + 24])
        after = paren.group(1).lower() if paren else ""     # only "(pbk.)" after the number
        fmt, kind = "print", None
        best = 99
        for pat, f, k in _ISBN_KIND:
            rx = re.compile(rf"\b(?:{pat})\b")
            dists = [len(before) - km.end() for km in rx.finditer(before)]
            dists += [km.start() for km in rx.finditer(after)]
            if dists and min(dists) < best:
                best, fmt, kind = min(dists), f, k
        imp.isbns.append(Isbn(im.group(1).strip(), fmt, kind))
    m = re.search(r"\b(?:first )?published(?: in [A-Z][\w ]{2,30}?)?(?: by [^\n,]{2,60}?)?"
                  r",? (?:in )?((?:19|20)\d{2})\b", text, re.I)
    if m:
        imp.year = int(m.group(1))
    # Library of Congress CIP: "Description: London ; New York : Bloomsbury Academic, an imprint
    # of Bloomsbury Publishing Plc, 2018."
    m = re.search(r"(?:Description|Published):\s*(?P<loc>[A-Z][^:\n]{2,60}?)\s*:\s*"
                  r"(?P<pub>[^,\n]{2,80}?)(?:,[^,\n]{0,80}?)?,\s*\[?c?(?P<y>(?:19|20)\d{2})", text)
    if m:
        imp.publisher_loc = " ".join(m.group("loc").replace(" ;", ";").split())
        imp.publisher = imp.publisher or m.group("pub").strip()
        imp.year = imp.year or int(m.group("y"))
    if imp.publisher is None:
        m = re.search(r"((?:[A-Z][a-z][\w&'’.-]*\s+){1,4}(?:Publishing|Press|Publishers|Books)"
                      r"(?:\s+(?:Plc|PLC|Ltd\.?|Limited|Inc\.?|LLC|Group|Company|Co\.))?)", text)
        if m:
            imp.publisher = " ".join(m.group(1).split())
    for ln in lines:
        t = " ".join(ln.split())
        if "©" in t or re.match(r"(?i)copyright\b", t):
            imp.copyright = t if imp.copyright is None else imp.copyright
            y = re.search(r"\b((?:19|20)\d{2})\b", t)
            if y and imp.copyright_year is None:
                imp.copyright_year = int(y.group(1))
    m = re.search(r"\b((?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                  r"\d{1,2}(?:st|nd|rd|th))\s+edition)\b", text, re.I)
    if m:
        imp.edition = m.group(1)
    return imp


# ---------------------------------------------------------------- pages before the contents

def dedication_like(texts: Sequence[str]) -> bool:
    joined = " ".join(" ".join(t.split()) for t in texts)
    return (0 < len(joined) <= 250 and len(texts) <= 4
            and bool(re.match(r"(?i)(to|for|in (loving )?memory|dedicated)\b", joined)))


def series_like(texts: Sequence[str]) -> bool:
    joined = " ".join(texts).lower()
    return bool(re.search(r"also available|titles (are )?(now )?available|titles in (this|the) "
                          r"series|other titles|also by|series editors?|in the same series|"
                          r"forthcoming titles", joined))
