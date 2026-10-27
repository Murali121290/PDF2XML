"""GROBID (https://github.com/grobidOrg/grobid, Apache-2.0) as an optional second reader.

GROBID is a local Java service trained on scholarly documents. Two of its answers are used:

- ``/api/processCitationList``: a printed reference → its fields (authors, year, titles,
  journal, volume, pages, publisher …). GROBID's values are normalised, so ``align`` finds each
  of them in the printed text and returns ``citations.Span``s over it. Every printed character
  stays where it was; a field that cannot be found is left untagged.
- ``/api/processHeaderDocument``: the authors of an article or chapter, with affiliations,
  e-mail addresses and ORCIDs.

Answers are kept in ``12_grobid.json`` in the conversion folder, so a re-export gives the same
XML without the server, and a server that is down only means the references are read by
``citations.parse_reference`` alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from pdf2xml.model import Document
from pdf2xml.publishing.citations import Parsed, Span, _nested_ok, _tail_links
from pdf2xml.publishing.profile import Metadata, Person

log = logging.getLogger(__name__)

TEI = "{http://www.tei-c.org/ns/1.0}"
CACHE_FILE = "12_grobid.json"
_BATCH = 100                                 # reference strings per request


# ---------------------------------------------------------------- GROBID's answers

@dataclass
class Name:
    surname: str = ""
    given: list[str] = field(default_factory=list)      # forenames as printed: ["J", "A"]
    org: str = ""                                       # an organisation as author


@dataclass
class Bibl:
    """One reference as GROBID read it (normalised values, not positions)."""

    authors: list[Name] = field(default_factory=list)
    editors: list[Name] = field(default_factory=list)
    title_a: str = ""           # article or chapter title
    title_m: str = ""           # book title
    title_j: str = ""           # journal title
    year: str = ""
    volume: str = ""
    issue: str = ""
    fpage: str = ""
    lpage: str = ""
    publisher: str = ""
    place: str = ""
    edition: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Bibl:
        names = {k: [Name(**n) for n in d.get(k, [])] for k in ("authors", "editors")}
        return Bibl(**{**d, **names})


def _text(el: etree._Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _names(parent: etree._Element | None, tag: str) -> list[Name]:
    out: list[Name] = []
    if parent is None:
        return out
    for a in parent.findall(TEI + tag):
        pn = a.find(TEI + "persName")
        if pn is not None:
            n = Name(surname=_text(pn.find(TEI + "surname")),
                     given=[_text(f) for f in pn.findall(TEI + "forename") if _text(f)])
            if n.surname:
                out.append(n)
            continue
        org = _text(a.find(TEI + "orgName"))
        if org:
            out.append(Name(org=org))
    return out


def _title(parent: etree._Element | None, level: str) -> str:
    if parent is None:
        return ""
    titles = [t for t in parent.findall(TEI + "title") if t.get("level") == level]
    main = [t for t in titles if t.get("type") in (None, "main")]
    return _text((main or titles or [None])[0])


def read_bibl(bs: etree._Element) -> Bibl:
    """A TEI ``biblStruct`` → ``Bibl``."""
    analytic = bs.find(TEI + "analytic")
    monogr = bs.find(TEI + "monogr")
    b = Bibl()
    b.authors = _names(analytic, "author") or _names(monogr, "author")
    b.editors = _names(monogr, "editor")
    b.title_a = _title(analytic, "a")
    b.title_m = _title(monogr, "m")
    b.title_j = _title(monogr, "j")
    imprint = monogr.find(TEI + "imprint") if monogr is not None else None
    if imprint is not None:
        for sc in imprint.findall(TEI + "biblScope"):
            unit = sc.get("unit")
            if unit in ("volume", "issue"):
                setattr(b, unit, _text(sc))
            elif unit == "page":
                b.fpage = sc.get("from") or _text(sc)
                b.lpage = sc.get("to") or ""
        for d in imprint.findall(TEI + "date"):
            m = re.match(r"\d{4}", d.get("when") or _text(d))
            if m and d.get("type") in (None, "published"):
                b.year = m.group(0)
                break
        b.publisher = _text(imprint.find(TEI + "publisher"))
        b.place = _text(imprint.find(TEI + "pubPlace"))
    if monogr is not None:
        b.edition = _text(monogr.find(TEI + "edition"))
    return b


def read_citations(xml: bytes) -> list[tuple[str, Bibl]]:
    """``processCitationList``'s TEI → (raw reference, Bibl) in document order."""
    root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True))
    out = []
    for bs in root.iter(TEI + "biblStruct"):
        if any(a.tag == TEI + "biblStruct" for a in bs.iterancestors()):
            continue
        raw = next((_text(n) for n in bs.iter(TEI + "note") if n.get("type") == "raw_reference"),
                   "")
        out.append((raw, read_bibl(bs)))
    return out


def read_header(xml: bytes) -> list[Person]:
    """``processHeaderDocument``'s TEI → the authors, with affiliation, e-mail and ORCID."""
    root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True))
    src = root.find(f".//{TEI}sourceDesc/{TEI}biblStruct")
    if src is None:
        return []
    people: list[Person] = []
    analytic = src.find(TEI + "analytic")
    for a in (analytic.findall(TEI + "author") if analytic is not None else []):
        pn = a.find(TEI + "persName")
        if pn is None:
            continue
        surname = _text(pn.find(TEI + "surname"))
        if not surname:
            continue
        given = " ".join(_text(f) for f in pn.findall(TEI + "forename")) or None
        affs = []
        for af in a.findall(TEI + "affiliation"):
            raw = next((n for n in af.findall(TEI + "note") if n.get("type") == "raw_affiliation"),
                       None)
            if raw is not None:
                for lab in raw.iter(TEI + "label"):       # the printed marker ("1", "a")
                    lab.text = ""
                text = _text(raw)
            else:
                parts = [_text(o) for o in af.findall(TEI + "orgName")]
                addr = af.find(TEI + "address")
                if addr is not None:
                    parts += [_text(x) for x in addr]
                text = ", ".join(p for p in parts if p)
            if text and text not in affs:
                affs.append(text)
        orcid = next((_text(i) for i in a.iter(TEI + "idno") if (i.get("type") or "").upper()
                      == "ORCID"), None)
        people.append(Person(surname=surname, given=given, email=_text(a.find(TEI + "email"))
                             or None, aff="; ".join(affs) or None, orcid=orcid or None))
    return people


# ---------------------------------------------------------------- client

def _key(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


class Grobid:
    """A GROBID server, with the answers kept in ``cache`` (``12_grobid.json``)."""

    def __init__(self, url: str, cache: Path | None = None, reuse: bool = True,
                 timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self._checked = False
        self.version: str | None = None     # the server's version; None while unreachable
        self.errors: list[str] = []         # for the export warnings, each said once
        self.asked = 0                      # references sent to the server
        self.data: dict[str, Any] = {"version": None, "citations": {}, "header": None}
        if reuse and cache is not None and cache.exists():
            try:
                self.data = {**self.data, **json.loads(cache.read_text(encoding="utf-8"))}
            except (OSError, ValueError):
                log.warning("ignoring unreadable %s", cache)
        host = urllib.parse.urlsplit(self.url).hostname or ""
        handlers = [urllib.request.ProxyHandler({})] if host in ("localhost", "127.0.0.1",
                                                                  "::1") else []
        self._opener = urllib.request.build_opener(*handlers)

    # -- transport
    def _request(self, path: str, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> bytes:
        req = urllib.request.Request(self.url + path, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        with self._opener.open(req, timeout=self.timeout) as resp:
            body: bytes = resp.read()
            return body

    def available(self) -> bool:
        """Ask the server once for its version. A cache written by another GROBID version is
        dropped, since its answers may differ."""
        if not self._checked:
            self._checked = True
            try:
                answer = self._request("/api/version").decode("utf-8", "replace").strip()
                # 0.9 answers {"version": "0.9.1", "revision": …}; older servers plain text
                if answer.startswith("{"):
                    answer = str(json.loads(answer).get("version") or "")
                self.version = answer or "unknown"
            except (OSError, urllib.error.URLError, ValueError) as e:
                self._fail(f"GROBID is not reachable at {self.url} ({_reason(e)}); references "
                           "are read without it" + (", from the answers kept earlier"
                                                    if self.data["citations"] else ""))
                return False
            if self.data.get("version") not in (None, self.version):
                self.data = {"version": self.version, "citations": {}, "header": None}
            self.data["version"] = self.version
        return self.version is not None

    # -- references
    def citations(self, texts: Sequence[str]) -> list[Bibl | None]:
        """GROBID's reading of each printed reference, None where it has none."""
        online = self.available()           # first, so another version's answers are dropped
        store: dict[str, Any] = self.data["citations"]
        todo = list(dict.fromkeys(t for t in texts if _key(t) not in store))
        if todo and online:
            for i in range(0, len(todo), _BATCH):
                batch = todo[i:i + _BATCH]
                try:
                    got = self._citation_batch(batch)
                except (OSError, urllib.error.URLError, ValueError, etree.XMLSyntaxError) as e:
                    self._fail(f"GROBID could not read references ({_reason(e)}); they are "
                               "read without it")
                    break
                self.asked += len(batch)
                for t, b in zip(batch, got, strict=True):
                    store[_key(t)] = asdict(b) if b is not None else None
        out: list[Bibl | None] = []
        for t in texts:
            d = store.get(_key(t))
            out.append(Bibl.from_dict(d) if d else None)
        return out

    def _citation_batch(self, batch: list[str]) -> list[Bibl | None]:
        body = urllib.parse.urlencode(
            [("citations", t) for t in batch]
            + [("consolidateCitations", "0"), ("includeRawCitations", "1")]).encode("utf-8")
        xml = self._request("/api/processCitationList", body, {
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/xml"})
        got = read_citations(xml)
        if len(got) == len(batch):
            return [b for _, b in got]
        # answers missing for some strings: pair them by the raw text GROBID echoes back
        by_raw = {_norm_key(raw): b for raw, b in got if raw}
        return [by_raw.get(_norm_key(t)) for t in batch]

    # -- header
    def header(self, pdf: Path | None, sha256: str) -> list[Person]:
        """The PDF's authors, from the cache when it was made from the same PDF."""
        online = self.available()
        cached = self.data.get("header")
        if cached and cached.get("sha256") == sha256:
            return [Person(**p) for p in cached.get("people", [])]
        if pdf is None or not online:
            return []
        boundary = uuid.uuid4().hex
        parts = [(b"--" + boundary.encode() + b"\r\nContent-Disposition: form-data; "
                  b'name="' + k.encode() + b'"\r\n\r\n' + v.encode() + b"\r\n")
                 for k, v in (("consolidateHeader", "0"), ("includeRawAffiliations", "1"))]
        parts.append(b"--" + boundary.encode() + b'\r\nContent-Disposition: form-data; '
                     b'name="input"; filename="' + pdf.name.encode("ascii", "replace")
                     + b'"\r\nContent-Type: application/pdf\r\n\r\n' + pdf.read_bytes() + b"\r\n")
        body = b"".join(parts) + b"--" + boundary.encode() + b"--\r\n"
        try:
            xml = self._request("/api/processHeaderDocument", body, {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/xml"})
            people = read_header(xml)
        except (OSError, urllib.error.URLError, ValueError, etree.XMLSyntaxError) as e:
            self._fail(f"GROBID could not read the PDF's authors ({_reason(e)}); the byline "
                       "under the title is used")
            return []
        self.data["header"] = {"sha256": sha256,
                               "people": [p.model_dump(exclude_none=True) for p in people]}
        return people

    def _fail(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)
            log.warning("%s", message)

    def save(self) -> None:
        if self.cache is not None and (self.data["citations"] or self.data["header"]):
            self.cache.write_text(json.dumps(self.data, ensure_ascii=False, indent=1,
                                             sort_keys=True), encoding="utf-8")


def with_authors(meta: Metadata, people: Sequence[Person], doc: Document) -> Metadata:
    """``meta`` with GROBID's authors as the article's or chapter's contributors, unless the
    metadata file names them. Only people whose surname is printed in the first pages count
    (a header model can take a name from a running head or a series page)."""
    if meta.part.contributors or not people:
        return meta
    first = min((p.n for p in doc.pages), default=1)
    text = " ".join(r.text for b in doc.body if b.page <= first + 2
                    for r in getattr(b, "runs", []))
    folded, _ = _fold(text)
    kept = [p for p in people if p.surname and _fold(p.surname)[0] in folded]
    if not kept:
        return meta
    return meta.model_copy(update={"part": meta.part.model_copy(update={"contributors": kept})})


def _reason(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    return str(e) or type(e).__name__


def _norm_key(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


# ---------------------------------------------------------------- printed positions

def _fold(text: str) -> tuple[str, list[int]]:
    """Letters and digits only, compatibility-folded and case-folded, with each one's index
    in ``text``: "Smith, J. (2010)" and "smith j 2010" look alike."""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if c.isalnum():
                chars.append(c)
                idx.append(i)
    return "".join(chars), idx


class _Text:
    """The printed reference, with the ranges already given to an element."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.folded, self.idx = _fold(text)
        self.taken: list[tuple[int, int]] = []

    def free(self, s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in self.taken)

    def take(self, s: int, e: int) -> tuple[int, int]:
        self.taken.append((s, e))
        return s, e

    def bounded(self, s: int, e: int) -> bool:
        """A whole word: "Smith" is not found inside "Smithson", nor "12" inside "2012"."""
        t = self.text
        return (s == 0 or not t[s - 1].isalnum()) and (e >= len(t) or not t[e].isalnum())

    def find(self, value: str, lo: int = 0, hi: int | None = None) -> tuple[int, int] | None:
        """The first free, whole-word place of ``value`` in ``[lo, hi)``, compared on letters and
        digits only. The result runs from its first to its last letter or digit (a "?" ending a
        title is kept)."""
        hi = len(self.text) if hi is None else hi
        needle, _ = _fold(value)
        if not needle:
            return None
        pos = 0
        while (k := self.folded.find(needle, pos)) != -1:
            pos = k + 1
            s, e = self.idx[k], self.idx[k + len(needle) - 1] + 1
            if s < lo or e > hi:
                if s >= hi:
                    break
                continue
            if not self.bounded(s, e):
                continue
            if value.rstrip()[-1:] in ("?", "!") and self.text[e:e + 1] in ("?", "!"):
                e += 1
            if self.free(s, e):
                return s, e
        return None

    def match(self, pattern: str, at: int) -> re.Match[str] | None:
        return re.compile(pattern).match(self.text, at)


def _initials(given: Sequence[str]) -> str:
    """"J", "A" → a pattern for "J. A.", "J.A.", "JA", "J.-A."; "John" for "John" (the period
    after a whole name ends the sentence, not the name)."""
    words = [re.escape(w) + (r"\.?" if len(w) <= 2 else "")
             for g in given for w in re.findall(r"\w+", g)]
    return r"[\s.\-‐]*".join(words)


def _person(t: _Text, n: Name, lo: int, hi: int) -> tuple[Span, str] | None:
    """One person or organisation, searched in ``[lo, hi)`` → (span, name for citation keys)."""
    if n.org:
        r = t.find(n.org, lo, hi)
        return (Span(*t.take(*r), "collab"), n.org) if r else None
    r = t.find(n.surname, lo, hi)
    if r is None:
        return None
    s, e = r
    kids = [Span(s, e, "surname")]
    pat = _initials(n.given)
    if pat:
        after = t.match(r"\s*(,?)\s*(" + pat + r")", e)
        before = re.search("(" + pat + r")[\s,]*$", t.text[max(lo, s - 40):s])
        if after and after.end() <= hi and t.free(e, after.end()):
            g0, g1 = after.start(2), after.end(2)
            given = t.text[g0:g1]
            # "Smith JA." (Vancouver, no comma, no periods in the initials): the period ends
            # the author list; "Smith, J. A." and "Smith J.A." keep theirs
            if not after.group(1) and given.endswith(".") and "." not in given[:-1]:
                g1 -= 1
            kids.append(Span(g0, g1, "given-names"))
            e = g1
        elif before and t.free(max(lo, s - 40) + before.start(1), s):
            g0 = max(lo, s - 40) + before.start(1)
            g1 = max(lo, s - 40) + before.end(1)
            kids.insert(0, Span(g0, g1, "given-names"))
            s = g0
    t.take(s, e)
    return Span(s, e, "string-name", {}, kids), n.surname


def _group(t: _Text, people: Sequence[Name], kind: str, lo: int, hi: int
           ) -> tuple[Span | None, list[str], list[str]]:
    """``person-group`` over the names found, in order, with "et al." and "(Eds.)"."""
    kids: list[Span] = []
    surnames: list[str] = []
    collabs: list[str] = []
    pos = lo
    for n in people:
        got = _person(t, n, pos, hi)
        if got is None:
            continue
        sp, name = got
        kids.append(sp)
        (collabs if sp.tag == "collab" else surnames).append(name)
        pos = sp.end
    if not kids:
        return None, [], []
    s, e = kids[0].start, kids[-1].end
    etal = t.match(r"[\s,]*(et\s+al\.?)", e)
    if etal and etal.end(1) <= hi and t.free(etal.start(1), etal.end(1)):
        kids.append(Span(etal.start(1), etal.end(1), "etal"))
        e = etal.end(1)
    role = t.match(r"\s*,?\s*\(\s*((?:eds?|editors?|hrsg|coords?)\.?)\s*\)", e)
    if role and role.end() <= hi and t.free(role.start(), role.end()):
        kids.append(Span(role.start(1), role.end(1), "role"))
        e = role.end()
    # the names were taken one by one; nothing else may lie between them
    own = [(k.start, k.end) for k in kids]
    if any(not (b <= s or a >= e) and (a, b) not in own for a, b in t.taken):
        return None, [], []
    t.take(s, e)
    return Span(s, e, "person-group", {"person-group-type": kind}, kids), surnames, collabs


def fill(primary: Parsed, other: Parsed | None) -> Parsed:
    """``primary`` plus the elements ``other`` found that it lacks (an edition, an issue, an
    e-locator …), where they overlap nothing ``primary`` tagged."""
    if other is None:
        return primary
    have = {sp.tag for sp in primary.spans}
    extra = [sp for sp in other.spans if sp.tag not in have
             and all(sp.end <= p.start or sp.start >= p.end for p in primary.spans)]
    if not extra:
        return primary
    spans = sorted([*primary.spans, *extra], key=lambda sp: sp.start)
    if not _nested_ok(spans):
        return primary
    return Parsed(spans, primary.kind, primary.surnames, primary.collabs, primary.year)


def align(text: str, b: Bibl) -> Parsed | None:
    """GROBID's fields placed on the printed reference, as ``citations.Span``s (the same tree
    ``parse_reference`` returns). None when not enough of it can be found to be useful: names
    (or an organisation) and a title or source."""
    t = _Text(text)
    _, links = _tail_links(text, 0)
    for ln in links:
        t.take(ln.start, ln.end)
    spans: list[Span] = list(links)

    def place(value: str, tag: str, lo: int = 0, hi: int | None = None) -> Span | None:
        if not value:
            return None
        r = t.find(value, lo, hi)
        if r is None:
            return None
        sp = Span(*t.take(*r), tag)
        spans.append(sp)
        return sp

    chapter = bool(b.title_a and b.title_m)
    kind = ("chapter" if chapter else "journal" if b.title_j
            else "book" if b.title_m else "other")
    first_title = place(b.title_a, "chapter-title" if chapter else "article-title")
    src_value = b.title_j or b.title_m
    if first_title is None and not b.title_a:
        first_title = place(src_value, "source")
        src_value = ""
    head_end = first_title.start if first_title else len(text)
    people = b.authors or ([] if chapter else b.editors)
    ptype = "author" if b.authors or chapter else "editor"
    group, surnames, collabs = _group(t, people, ptype, 0, head_end)
    if group is not None:
        spans.append(group)
    after_names = group.end if group else 0
    if b.year:
        m = next((m for m in re.finditer(rf"(?<!\d){b.year}[a-z]?(?![\da-z])", text)
                  if t.free(m.start(), m.end()) and m.start() >= after_names), None)
        if m is None:
            m = next((m for m in re.finditer(rf"(?<!\d){b.year}[a-z]?(?![\da-z])", text)
                      if t.free(m.start(), m.end())), None)
        if m is not None:
            spans.append(Span(*t.take(m.start(), m.end()), "year"))
    after = first_title.end if first_title else after_names
    source = place(src_value, "source", after) if src_value else None
    if chapter and b.editors:
        # "In A. Smith (Eds.), Book" or "In Book, edited by Anne Smith"
        eds, _, _ = _group(t, b.editors, "editor", after, len(text))
        if eds is not None:
            spans.append(eds)
    tail = source.end if source else after
    # volume, pages and publisher come before a URL; what follows it ("accessed 22 January
    # 2018") is the access note, not a page number
    stop = min((ln.start for ln in links if ln.start >= tail), default=len(text))
    place(b.edition, "edition", tail, stop)
    vol = place(b.volume, "volume", tail, stop)
    place(b.issue, "issue", vol.end if vol else tail, stop)
    fp = place(b.fpage, "fpage", vol.end if vol else tail, stop)
    if fp is not None:
        lp = t.match(r"\s*[–\-‐—]\s*([A-Za-z]?\d+)", fp.end)
        if lp and lp.end(1) <= stop and t.free(lp.start(1), lp.end(1)):
            spans.append(Span(*t.take(lp.start(1), lp.end(1)), "lpage"))
    place(b.place, "publisher-loc", tail, stop)
    place(b.publisher, "publisher-name", tail, stop)
    spans.sort(key=lambda sp: sp.start)
    has_names = group is not None
    has_title = any(sp.tag in ("article-title", "chapter-title", "source") for sp in spans)
    if not (has_names and has_title) or not _nested_ok(spans):
        return None
    year = next((text[sp.start:sp.end] for sp in spans if sp.tag == "year"), None)
    return Parsed(spans, kind, surnames, collabs, year)
