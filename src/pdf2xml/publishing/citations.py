"""Granular reference tagging: a printed reference → JATS ``mixed-citation`` element spans.

``parse_reference`` reads author–year references (APA, Harvard, Chicago author-date) and
numbered Vancouver-style references and returns a tree of spans over the reference text:
``person-group`` > ``string-name`` > ``surname``/``given-names``, ``collab``, ``etal``, ``year``,
``month``, ``day``, ``article-title``, ``chapter-title``, ``source``, ``edition``, ``volume``,
``issue``, ``fpage``, ``lpage``, ``elocation-id``, ``publisher-loc``, ``publisher-name``,
``pub-id`` and ``ext-link``. Every character of the printed reference stays in place (the
punctuation between elements is kept as text), so the tagged citation reads exactly as printed.

It returns ``None`` when the text does not look like a reference it understands; the caller then
keeps the reference as untagged text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


@dataclass
class Span:
    start: int
    end: int
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)


@dataclass
class Parsed:
    spans: list[Span]
    kind: str                                   # journal | book | chapter | webpage | other
    surnames: list[str] = field(default_factory=list)
    collabs: list[str] = field(default_factory=list)
    year: str | None = None


_YEAR_PAREN = re.compile(
    r"\(\s*(?P<year>(?:1[5-9]|20)\d{2}[a-z]?|n\.\s?d\.|in press|forthcoming)"
    r"(?:\s*,\s*(?P<month>[A-Z][a-z]+\.?)(?:\s+(?P<day>\d{1,2})(?:\s*[–-]\s*\d{1,2})?)?)?\s*\)",
    re.I)
_YEAR_BARE = re.compile(r"(?<=[.,]\s)(?P<year>(?:1[5-9]|20)\d{2}[a-z]?)(?=[.,]\s|\.$|$)")
_INITIALS = re.compile(r"^(?:[A-Z][a-z]?\.?\s?-?\s?){1,5}$")           # "J.", "J. C.", "J.-P."
_FIRST_NAMES = re.compile(r"^[A-Z][a-z'’\-]+(?:\s+(?:[A-Z]\.|[A-Z][a-z'’\-]+)){0,3}$")
_SURNAME = re.compile(r"^(?:(?:van|von|de|der|den|da|di|del|la|le|du|bin|al|el|st\.?)\s+)*"
                      r"[A-ZÀ-ɏ][\w'’.\-]*(?:[\s-][A-ZÀ-ɏ][\w'’\-]*){0,2}$")
_VANC_NAME = re.compile(r"^(?P<sur>[A-ZÀ-ɏ][\w'’\-]+(?:\s[A-ZÀ-ɏ][\w'’\-]+)?)"
                        r"\s(?P<giv>[A-Z]{1,4})$")
_SEP = re.compile(r"\s*,\s*(?:(?:&|and)\s+)?|\s+(?:&|and)\s+")
_ETAL = re.compile(r"\bet\s+al\.?", re.I)
_URL = re.compile(r"(?:https?://\s?|www\.)\S+|\bdoi:\s*10\.\d{4,9}/\S+", re.I)
_DOI_IN_URL = re.compile(r"10\.\d{4,9}/\S+")
_EDITORS = re.compile(r"\s*\((?:eds?|editors?|hrsg|coords?)\.?\)\s*[.,]?\s*$", re.I)
_EDITION = re.compile(r"\((?P<ed>\d+(?:st|nd|rd|th)\.?\s+(?:ed|edn|edition)\.?|"
                      r"(?:rev|revised)\.?\s+ed\.?)\)", re.I)
_PAGES_PP = re.compile(r"\(pp?\.\s*(?P<f>[A-Za-z]?\d+)(?:\s*[–-]\s*(?P<l>[A-Za-z]?\d+))?\)")
_JOURNAL_TAIL = re.compile(
    r"^(?P<src>[^,]+?(?:,\s*[^,\d][^,]*?)*?),\s*(?P<vol>\d+[A-Za-z]?)"
    r"(?:\s*\((?P<iss>[^)]{1,20})\))?"
    r"(?:\s*[,:]\s*(?:pp?\.\s*)?(?P<f>[A-Za-z]?\d+)(?:\s*[–-]\s*(?P<l>[A-Za-z]?\d+))?)?"
    r"\s*\.?\s*$")
_VANC_TAIL = re.compile(
    r"^(?P<src>[^.]+(?:\.[^.;\d]+)*?)\.\s*(?P<year>(?:1[5-9]|20)\d{2})"
    r"(?:\s+[A-Z][a-z]{2}(?:\s+\d{1,2})?)?"
    r"\s*;\s*(?P<vol>\d+)(?:\s*\((?P<iss>[^)]+)\))?"
    r"(?:\s*:\s*(?P<f>[A-Za-z]?\d+)(?:\s*[–-]\s*(?P<l>[A-Za-z]?\d+))?)?\s*\.?\s*$")
_PUBLISHER = re.compile(r"^(?:(?P<loc>[A-Z][\w .'’\-]+(?:,\s*[A-Z]{2,}|,\s*[A-Z][\w .]+)?)\s*:\s*)?"
                        r"(?P<name>[A-Z][^.]*?(?:\.\s*(?:Inc|Ltd|Co|Corp|LLC)\.?)?)\s*\.?\s*$")


def _strip(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s] in " \t,;":
        s += 1
    while e > s and text[e - 1] in " \t,;":
        e -= 1
    return s, e


def _sentence_end(text: str, start: int, stop: int) -> int:
    """End of the sentence starting at ``start`` (index of its closing punctuation), or -1.
    Initials ("J. Smith"), "et al.", "vs.", "No." and similar do not end a sentence."""
    for m in re.finditer(r"[.?!](?=\s+\S|\s*$)", text[start:stop]):
        i = start + m.start()
        before = text[max(start, i - 4):i]
        if text[i] == "." and (re.search(r"(?:^|[\s(])(?:[A-Z]|et al|vs|No|Vol|Ed|eds?|pp?|St|Dr|"
                                          r"Inc|Jr|Sr|U\.S|e\.g|i\.e)$", before)):
            continue
        return i
    return -1


def _people(text: str, s: int, e: int, group_type: str) -> tuple[Span | None, list[str],
                                                                  list[str]]:
    """Authors or editors "Smith, J. A., Doe, B., & Lee, C." → person-group span."""
    seg = text[s:e]
    toks: list[tuple[int, int]] = []
    pos = 0
    for m in _SEP.finditer(seg):
        if m.start() > pos:
            toks.append((pos, m.start()))
        pos = m.end()
    if pos < len(seg):
        toks.append((pos, len(seg)))
    toks = [(a, b) for a, b in (_strip(seg, a, b) for a, b in toks) if b > a]
    if not toks:
        return None, [], []
    group = Span(s, e, "person-group", {"person-group-type": group_type})
    surnames: list[str] = []
    collabs: list[str] = []
    words = [seg[a:b] for a, b in toks]
    etal = [i for i, w in enumerate(words) if _ETAL.fullmatch(w.strip(". ") + ".")]

    def add_etal(i: int) -> None:
        a, b = toks[i]
        m = _ETAL.search(seg, a, b + 1) if b < len(seg) else _ETAL.search(seg, a)
        if m:
            group.children.append(Span(s + m.start(), s + m.end(), "etal"))

    # "Surname, I." pairs (APA, Harvard)
    names = [i for i in range(len(words)) if i not in etal]
    if len(names) >= 2 and len(names) % 2 == 0 and all(
            _SURNAME.match(words[names[k]]) for k in range(0, len(names), 2)) and (
            all(_INITIALS.match(words[names[k]]) for k in range(1, len(names), 2))
            or all(_FIRST_NAMES.match(words[names[k]]) for k in range(1, len(names), 2))):
        for k in range(0, len(names), 2):
            (a1, b1), (a2, b2) = toks[names[k]], toks[names[k + 1]]
            sn = Span(s + a1, s + b2, "string-name", {}, [
                Span(s + a1, s + b1, "surname"), Span(s + a2, s + b2, "given-names")])
            group.children.append(sn)
            surnames.append(words[names[k]])
        for i in etal:
            add_etal(i)
        group.children.sort(key=lambda x: x.start)
        return group, surnames, collabs
    # "Smith JA, Doe B" (Vancouver)
    if names and all(_VANC_NAME.match(words[i]) for i in names):
        for i in names:
            a, b = toks[i]
            vm = _VANC_NAME.match(words[i])
            assert vm
            group.children.append(Span(s + a, s + b, "string-name", {}, [
                Span(s + a + vm.start("sur"), s + a + vm.end("sur"), "surname"),
                Span(s + a + vm.start("giv"), s + a + vm.end("giv"), "given-names")]))
            surnames.append(vm.group("sur"))
        for i in etal:
            add_etal(i)
        group.children.sort(key=lambda x: x.start)
        return group, surnames, collabs
    # "A. Smith & B. Jones" (editors in APA are printed given-names first)
    if names and all(re.match(r"^(?:[A-Z][a-z]?\.\s?-?){1,4}\s*\S", words[i]) for i in names):
        for i in names:
            a, b = toks[i]
            gm = re.match(r"^((?:[A-Z][a-z]?\.\s?-?){1,4})\s*(.+)$", words[i])
            assert gm
            group.children.append(Span(s + a, s + b, "string-name", {}, [
                Span(s + a, s + a + len(gm.group(1).rstrip()), "given-names"),
                Span(s + a + gm.start(2), s + b, "surname")]))
            surnames.append(gm.group(2))
        for i in etal:
            add_etal(i)
        return group, surnames, collabs
    # organisations: no initials anywhere. "Centers for Disease Control and Prevention" is one
    # (the split at " and " is undone); ", &" joins two ("WHO, & Commission on …").
    if (not any(_INITIALS.match(w) for w in words) and all(w[:1].isupper() for w in words)
            and len(words) <= 6 and len(seg) <= 200):
        parts: list[tuple[int, int]] = []
        pos = 0
        for m in re.finditer(r",\s*(?:&|and)\s+", seg):
            parts.append(_strip(seg, pos, m.start()))
            pos = m.end()
        parts.append(_strip(seg, pos, len(seg)))
        for a, b in parts:
            if b > a:
                group.children.append(Span(s + a, s + b, "collab"))
                collabs.append(seg[a:b])
        return group, surnames, collabs
    return None, [], []


def _link(text: str, s: int, e: int) -> Span:
    """A DOI (as ``doi:`` or a doi.org URL) → ``pub-id``; any other URL → ``ext-link``."""
    url = text[s:e]
    clean = re.sub(r"\s+", "", url)            # a line break can leave a space inside
    m = _DOI_IN_URL.search(url)
    if m and " " not in m.group(0) and (url.lower().startswith("doi:")
                                        or re.search(r"doi\.org/", clean, re.I)):
        return Span(s + m.start(), e, "pub-id", {"pub-id-type": "doi"})
    href = clean if clean.lower().startswith("http") else "https://" + clean
    return Span(s, e, "ext-link", {"ext-link-type": "uri", XLINK_HREF: href})


def _tail_links(text: str, start: int) -> tuple[int, list[Span]]:
    """URLs/DOIs from ``start`` on; returns where the first one begins (or len) and spans."""
    spans = []
    first = len(text)
    for m in _URL.finditer(text, start):
        u = m.group(0).rstrip(".,;)")
        # a space left by a line break inside the URL ("https://doi .org/10…")
        e = m.start() + len(u)
        while True:
            m2 = re.match(r"\s(\.?[\w./\-?=&%#~]+)", text[e:])
            if not m2 or not (u.endswith(("/", ".", "-", "_")) or m2.group(1).startswith(
                    (".", "/", "-", "_", "?", "#"))):
                break
            u2 = m2.group(1).rstrip(".,;)")
            e += 1 + len(u2)
            u = text[m.start():e]
        first = min(first, m.start())
        spans.append(_link(text, m.start(), e))
    return first, spans


def _title_and_rest(text: str, s: int, stop: int, italic: Sequence[tuple[int, int]]
                    ) -> tuple[tuple[int, int], int, bool]:
    """The title after the date: (title span, where the rest starts, title set in italics)."""
    s, _ = _strip(text, s, stop)
    it = next(((a, b) for a, b in italic if a <= s + 1 and b > s + 3), None)
    if it is not None:
        e = min(it[1], stop)
        # the italic run may carry the period or stop before a "?" title end
        end = _sentence_end(text, s, stop)
        if end != -1 and end < e:
            e = end
        t_end = e
        while t_end > s and text[t_end - 1] in " .,":
            t_end -= 1
        if text[t_end:t_end + 1] in "?!":
            t_end += 1
        return (s, t_end), e + 1 if e < len(text) and text[e] in ".?!" else e, True
    end = _sentence_end(text, s, stop)
    if end == -1:
        end = stop
    t_end = end + 1 if end < len(text) and text[end] in "?!" else end
    # "Modern epidemiology (4th ed.)": the edition follows the title
    ed = re.search(r"\s\((?:\d+(?:st|nd|rd|th)\.?\s+(?:ed|edn|edition)|rev\.?\s+ed|vol\.)",
                   text[s:t_end], re.I)
    if ed:
        return (s, s + ed.start()), s + ed.start(), False
    return (s, t_end), min(end + 1, stop), False


def parse_reference(text: str, italic: Sequence[tuple[int, int]] = ()) -> Parsed | None:
    """Tag a printed reference. ``italic``: character ranges set in italics (a strong hint for
    the journal or book title)."""
    if len(text.strip()) < 15:
        return None
    return _author_year(text, italic) or _vancouver(text, italic)


def _author_year(text: str, italic: Sequence[tuple[int, int]]) -> Parsed | None:
    m = _YEAR_PAREN.search(text)
    bare = False
    if m is None or m.start() > 0.7 * len(text) or m.start() == 0:
        m = _YEAR_BARE.search(text)
        bare = True
        if m is None or m.start() > 0.6 * len(text):
            return None
    a_end = m.start()
    while a_end > 0 and text[a_end - 1] in " .,":
        a_end -= 1
    # keep the period of a final initial ("Lee, C." ) inside the name
    if a_end < m.start() and text[a_end] == "." and re.search(r"(?:^|[\s.\-])[A-Z]$",
                                                                text[:a_end]):
        a_end += 1
    group_type = "author"
    ed = _EDITORS.search(text[:a_end + 1])
    ed_span = None
    if ed:
        group_type = "editor"
        ed_span = (ed.start(), a_end)
        a_end = ed.start()
        while a_end > 0 and text[a_end - 1] in " .,":
            a_end -= 1
    people, surnames, collabs = _people(text, 0, a_end, group_type)
    if people is None:
        return None
    spans: list[Span] = [people]
    if ed_span is not None:
        people.end = ed_span[1]
        role = re.search(r"eds?|editors?|hrsg|coords?", text[ed_span[0]:ed_span[1]], re.I)
        if role:
            people.children.append(Span(ed_span[0] + role.start(),
                                        ed_span[0] + role.end() + (
                                            1 if text[ed_span[0] + role.end():][:1] == "."
                                            else 0), "role"))
    year = m.group("year")
    y0 = m.start("year")
    spans.append(Span(y0, m.end("year"), "year"))
    if not bare:
        if m.group("month"):
            spans.append(Span(m.start("month"), m.end("month"), "month"))
        if m.group("day"):
            spans.append(Span(m.start("day"), m.end("day"), "day"))
    after = m.end()
    while after < len(text) and text[after] in " .,:":
        after += 1
    stop, links = _tail_links(text, after)
    # "Retrieved May 5, 2020, from" and similar belong to the link, not to the source
    pre = re.search(r"(?:Retrieved|Accessed|Available)\b[^.]*?(?:from|at)?\s*$", text[after:stop],
                    re.I)
    if pre:
        stop = after + pre.start()
    body_end = stop
    while body_end > after and text[body_end - 1] in " .,":
        body_end -= 1
    (ts, te), rest, title_italic = _title_and_rest(text, after, body_end, italic)
    kind = "other"
    rest_s, rest_e = _strip(text, rest, body_end)
    rest_text = text[rest_s:rest_e]
    if rest_text.startswith("In "):
        kind = "chapter"
        spans.append(Span(ts, te, "chapter-title"))
        spans.extend(_chapter_tail(text, rest_s + 3, rest_e, italic))
    elif (jm := _JOURNAL_TAIL.match(rest_text)) and not title_italic:
        kind = "journal"
        spans.append(Span(ts, te, "article-title"))
        spans.extend(_journal_spans(jm, rest_s))
    elif title_italic or rest_text:
        # an italic title, or a title followed by a publisher: a book (or a web page)
        kind = "webpage" if links and not rest_text else "book"
        spans.append(Span(ts, te, "source"))
        spans.extend(_book_tail(text, rest_s, rest_e))
    else:
        spans.append(Span(ts, te, "article-title" if links else "source"))
        kind = "webpage" if links else "other"
    spans.extend(links)
    spans = [sp for sp in spans if sp.end > sp.start]
    spans.sort(key=lambda sp: sp.start)
    if not _nested_ok(spans):
        return None
    return Parsed(spans, kind, surnames, collabs, year)


def _journal_spans(jm: re.Match[str], off: int) -> list[Span]:
    out = [Span(off + jm.start("src"), off + jm.end("src"), "source"),
           Span(off + jm.start("vol"), off + jm.end("vol"), "volume")]
    if jm.group("iss"):
        out.append(Span(off + jm.start("iss"), off + jm.end("iss"), "issue"))
    if jm.group("f"):
        if jm.group("l"):
            out.append(Span(off + jm.start("f"), off + jm.end("f"), "fpage"))
            out.append(Span(off + jm.start("l"), off + jm.end("l"), "lpage"))
        elif re.match(r"[A-Za-z]", jm.group("f")):
            out.append(Span(off + jm.start("f"), off + jm.end("f"), "elocation-id"))
        else:
            out.append(Span(off + jm.start("f"), off + jm.end("f"), "fpage"))
    return out


def _book_tail(text: str, s: int, e: int) -> list[Span]:
    """"(4th ed.). Lippincott Williams & Wilkins" / "New York, NY: Springer"."""
    out: list[Span] = []
    m = _EDITION.match(text, s, e) if s < e and text[s] == "(" else None
    if m:
        out.append(Span(m.start("ed"), m.end("ed"), "edition"))
        s, e = _strip(text, m.end(), e)
        while s < e and text[s] in ". ":
            s += 1
    pm = _PAGES_PP.match(text, s, e) if s < e and text[s] == "(" else None
    if pm:
        out.extend(_pages(pm))
        s, e = _strip(text, pm.end(), e)
        while s < e and text[s] in ". ":
            s += 1
    if s < e:
        pub = _PUBLISHER.match(text[s:e])
        if pub and len(pub.group("name")) <= 120:
            if pub.group("loc"):
                out.append(Span(s + pub.start("loc"), s + pub.end("loc"), "publisher-loc"))
            out.append(Span(s + pub.start("name"), s + pub.end("name"), "publisher-name"))
    return out


def _pages(pm: re.Match[str]) -> list[Span]:
    out = [Span(pm.start("f"), pm.end("f"), "fpage")]
    if pm.group("l"):
        out.append(Span(pm.start("l"), pm.end("l"), "lpage"))
    return out


def _chapter_tail(text: str, s: int, e: int, italic: Sequence[tuple[int, int]]) -> list[Span]:
    """After "In ": editors "(Eds.)," then the book title, pages, edition, publisher."""
    out: list[Span] = []
    ed = re.search(r"\((?:eds?|editors?|hrsg)\.?\)\s*,?\s*", text[s:e], re.I)
    if ed:
        eds, _, _ = _people(text, s, s + ed.start(), "editor")
        if eds is not None:
            role = re.search(r"eds?|editors?|hrsg", text[s + ed.start():s + ed.end()], re.I)
            assert role
            r0 = s + ed.start() + role.start()
            r1 = s + ed.start() + role.end()
            if text[r1:r1 + 1] == ".":
                r1 += 1
            eds.end = s + ed.start() + ed.group(0).rstrip(" ,").rfind(")") + 1
            eds.children.append(Span(r0, r1, "role"))
            out.append(eds)
        s = s + ed.end()
    # book title: italic run, else up to "(pp." / "(4th ed." / the next sentence end
    t_end = -1
    it = next(((a, b) for a, b in italic if a <= s + 1 and b > s + 3), None)
    if it is not None:
        t_end = min(it[1], e)
    else:
        paren = re.search(r"\s\((?:pp?\.|\d+(?:st|nd|rd|th)\s+ed|vol\.)", text[s:e], re.I)
        stop = _sentence_end(text, s, e)
        cands = [x for x in ((s + paren.start()) if paren else -1, stop) if x != -1]
        t_end = min(cands) if cands else e
    ts, te = _strip(text, s, t_end)
    while te > ts and text[te - 1] in ". ":
        te -= 1
    out.append(Span(ts, te, "source"))
    rest = t_end
    while rest < e:
        rs, re_ = _strip(text, rest, e)
        while rs < re_ and text[rs] in ". ":
            rs += 1
        if rs >= re_:
            break
        if text[rs] == "(":
            close = text.find(")", rs)
            if close == -1 or close > re_:
                break
            inner = text[rs:close + 1]
            pm = _PAGES_PP.fullmatch(inner)
            em = _EDITION.fullmatch(inner)
            if pm:
                out.extend(Span(sp.start + rs, sp.end + rs, sp.tag) for sp in _pages(pm))
            elif em:
                out.append(Span(rs + em.start("ed"), rs + em.end("ed"), "edition"))
            else:
                # "(Vol. 2, pp. 10–20)"
                pm2 = re.search(r"pp?\.\s*(?P<f>[A-Za-z]?\d+)(?:\s*[–-]\s*(?P<l>[A-Za-z]?\d+))?",
                                inner)
                if pm2:
                    out.extend(Span(sp.start + rs, sp.end + rs, sp.tag) for sp in _pages(pm2))
            rest = close + 1
            continue
        out.extend(_book_tail(text, rs, re_))
        break
    return out


def _vancouver(text: str, italic: Sequence[tuple[int, int]]) -> Parsed | None:
    """"Smith J, Doe AB. Title of article. J Abbrev. 2010;12(3):45–67."""
    a_end = _sentence_end_vanc(text)
    if a_end == -1:
        return None
    people, surnames, collabs = _people(text, 0, a_end, "author")
    if people is None or not surnames:
        return None
    t_start = a_end + 1
    t_stop = _sentence_end(text, t_start, len(text))
    if t_stop == -1:
        return None
    ts, te = _strip(text, t_start, t_stop)
    stop, links = _tail_links(text, t_stop + 1)
    tail_s, tail_e = _strip(text, t_stop + 1, stop)
    while tail_e > tail_s and text[tail_e - 1] in " .":
        tail_e -= 1
    vm = _VANC_TAIL.match(text[tail_s:tail_e])
    if not vm:
        return None
    spans = [people, Span(ts, te, "article-title")]
    off = tail_s
    spans.append(Span(off + vm.start("src"), off + vm.end("src"), "source"))
    spans.append(Span(off + vm.start("year"), off + vm.end("year"), "year"))
    spans.append(Span(off + vm.start("vol"), off + vm.end("vol"), "volume"))
    if vm.group("iss"):
        spans.append(Span(off + vm.start("iss"), off + vm.end("iss"), "issue"))
    if vm.group("f"):
        spans.append(Span(off + vm.start("f"), off + vm.end("f"), "fpage"))
        if vm.group("l"):
            spans.append(Span(off + vm.start("l"), off + vm.end("l"), "lpage"))
    spans.extend(links)
    spans.sort(key=lambda sp: sp.start)
    if not _nested_ok(spans):
        return None
    return Parsed(spans, "journal", surnames, collabs, vm.group("year"))


def _sentence_end_vanc(text: str) -> int:
    """End of a Vancouver author list: the first ". " not after an initial group."""
    for m in re.finditer(r"\.(?=\s)", text):
        before = text[:m.start()]
        if re.search(r"\bet al$", before):
            return m.start()
        if re.search(r"\s[A-Z]{1,4}$", before) or re.search(r"[a-z]{2,}$", before):
            return m.start()
    return -1


def _nested_ok(spans: list[Span]) -> bool:
    """Top-level spans must not overlap (children lie inside their parent)."""
    end = -1
    for sp in spans:
        if sp.start < end:
            return False
        for ch in sp.children:
            if ch.start < sp.start or ch.end > sp.end:
                return False
        end = sp.end
    return True
