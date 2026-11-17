#!/usr/bin/env python3
"""
pdf_xml_compare.py - compare the content of a PDF with its converted XML.

Single file:
    python pdf_xml_compare.py book.pdf book.xml --report report.html --json r.json --xlsx r.xlsx

Batch (files matched by name, book.pdf <-> book.xml):
    python pdf_xml_compare.py --batch pdf_folder xml_folder --out reports/

Options:
    --threshold 95        pass mark (score out of 100)
    --min-completeness .98  PASS also needs this share of PDF words found in the XML
    --skip-tags a,b       XML elements to ignore (e.g. metadata, teiHeader)
    --heading-tags a,b    extra XML element names to treat as headings
    --xsd schema.xsd      also validate the XML against a schema
    --keep-headers        do not strip running headers/footers/page numbers from the PDF

Requires: pip install pymupdf lxml        (openpyxl only for --xlsx)
Exit code: 0 = PASS, 1 = REVIEW/FAIL, 2 = error.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import difflib
import html
import json
import re
import sys
import unicodedata
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
try:
    import pymupdf as fitz  # newer name
except ImportError:  # pragma: no cover
    try:
        import fitz
    except ImportError:
        fitz = None
from lxml import etree

sys.setrecursionlimit(20000)

# --------------------------------------------------------------------------- #
# Normalisation / tokenising
# --------------------------------------------------------------------------- #
QUOTES = {ord(c): "'" for c in "\u2018\u2019\u201a\u201b\u2032"}
QUOTES.update({ord(c): '"' for c in "\u201c\u201d\u201e\u201f\u2033"})
QUOTES.update({ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})
TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*(?!\w)|\w+(?:['\-]\w+)*")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
PAGE_NUM_RE = re.compile(r"^\s*(?:page\s*)?[-\u2013\u2014\s]*\d+[-\u2013\u2014\s]*(?:of\s*\d+)?\s*$", re.I)


def normalize(text: str, hyphen_join: bool = False) -> str:
    t = unicodedata.normalize("NFKC", text)  # ligatures (fi), full-width chars...
    t = t.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "")
    if hyphen_join:
        t = re.sub(r"(?<=\w)-\n(?=[a-z])", "", t)  # line-break hyphenation
    t = t.translate(QUOTES)
    t = re.sub(r"\s*\x1e\s*", "\x1e", t)          # heading boundary marker
    t = re.sub(r"[^\S\x1e]+", " ", t).replace("\x1e", "\n")
    return re.sub(r" ?\n ?", "\n", t).strip()


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for unit in text.split("\n"):  # headings are their own unit
        out += [s for s in re.split(r"(?<=[.!?])\s+", unit) if s]
    return out


# --------------------------------------------------------------------------- #
# PDF extraction
# --------------------------------------------------------------------------- #
def extract_pdf(path: Path, keep_headers: bool = False) -> dict:
    if fitz is None:
        raise SystemExit("PyMuPDF is required:  pip install pymupdf")
    doc = fitz.open(str(path))
    sizes: Counter = Counter()
    raw_pages: list[list[dict]] = []
    images: set = set()
    tables = 0
    tables_ok = hasattr(fitz.Page, "find_tables")
    for page in doc:
        lines = []
        for bi, b in enumerate(page.get_text("dict")["blocks"]):
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in ln["spans"]).strip()
                size = round(max(s["size"] for s in spans) * 2) / 2
                bold = all((s["flags"] & 16) or "bold" in s["font"].lower() for s in spans)
                for s in spans:
                    sizes[round(s["size"] * 2) / 2] += len(s["text"].strip())
                lines.append({"text": text, "size": size, "bold": bold, "block": bi})
        raw_pages.append(lines)
        for im in page.get_images(full=True):
            if im[2] >= 32 and im[3] >= 32:
                images.add(im[0])
        if tables_ok:
            try:
                tables += sum(1 for t in page.find_tables().tables if t.row_count >= 2 and t.col_count >= 2)
            except Exception:
                tables_ok = False

    # running headers / footers / page numbers
    n = len(raw_pages)
    edge = lambda L: set(range(min(2, L))) | set(range(max(0, L - 2), L))
    key = lambda s: re.sub(r"\d+", "#", s.lower())
    repeated = set()
    if n >= 3 and not keep_headers:
        cnt: Counter = Counter()
        for lines in raw_pages:
            cnt.update({key(lines[i]["text"]) for i in edge(len(lines))})
        repeated = {k for k, v in cnt.items() if v >= max(3, 0.5 * n)}
    removed = 0
    kept_pages = []
    for lines in raw_pages:
        e = edge(len(lines))
        kept = []
        for i, l in enumerate(lines):
            if not keep_headers and i in e and (key(l["text"]) in repeated or PAGE_NUM_RE.match(l["text"])):
                removed += 1
                continue
            kept.append(l)
        kept_pages.append(kept)

    # heading guess from font size / bold
    body = sizes.most_common(1)[0][0] if sizes else 0
    headings = []
    for pno, lines in enumerate(kept_pages, 1):
        cur = None
        for l in lines:
            t = l["text"]
            is_h = (
                body
                and len(t) <= 160
                and re.search(r"[A-Za-z]", t)
                and (l["size"] >= body * 1.15 or (l["bold"] and l["size"] >= body and len(t) <= 80 and not t.endswith(".")))
            )
            l["h"] = bool(is_h)
            if is_h:
                if cur and cur["block"] == l["block"] and cur["size"] == l["size"]:
                    cur["text"] += " " + t
                else:
                    cur = {"page": pno, "text": t, "block": l["block"], "size": l["size"]}
                    headings.append(cur)
            else:
                cur = None
    return {
        "pages": ["\n".join(l["text"] + ("\x1e" if l.get("h") else "") for l in lines) for lines in kept_pages],
        "removed_lines": removed,
        "headings": [(h["page"], h["text"]) for h in headings],
        "images": len(images),
        "tables": tables if tables_ok else None,
    }


# --------------------------------------------------------------------------- #
# XML extraction
# --------------------------------------------------------------------------- #
INLINE = {
    "i", "b", "em", "strong", "sup", "sub", "span", "a", "u", "italic", "bold", "sc", "small", "tt", "code",
    "font", "ext-link", "xref", "underline", "monospace", "named-content", "styled-content", "emphasis",
    "mark", "abbr", "cite", "q", "inline-formula", "inline-graphic",
}
HEAD_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "head", "heading", "article-title", "chapter-title"}
HEAD_PARENTS = {"sec", "section", "chapter", "part", "body", "appendix", "app", "abstract", "ack", "div",
                "level1", "level2", "level3", "sect1", "sect2", "sect3", "article", "book"}
TABLE_TAGS = {"table", "tbl"}
IMAGE_TAGS = {"img", "image", "graphic", "imagedata"}


def local(el) -> str:
    return etree.QName(el).localname


def xml_text(root, skip: set, heads: set = frozenset()) -> str:
    parts: list[str] = []

    def walk(el, top=False):
        if not isinstance(el.tag, str):  # comment / PI
            if el.tail and not top:
                parts.append(el.tail)
            return
        name = local(el)
        if name not in skip:
            block = name.lower() not in INLINE
            if block:
                parts.append(" ")
            if el.text:
                parts.append(el.text)
            for c in el:
                walk(c)
            if block:
                parts.append("\x1e" if (name.lower() in HEAD_TAGS or name.lower() == "title" or name.lower() in heads) else " ")
        if el.tail and not top:
            parts.append(el.tail)

    walk(root, top=True)
    return "".join(parts)


def parse_xml(path: Path):
    try:
        return etree.parse(str(path), etree.XMLParser(huge_tree=True)).getroot(), None
    except etree.XMLSyntaxError as e:
        try:
            root = etree.parse(str(path), etree.XMLParser(huge_tree=True, recover=True)).getroot()
        except Exception:
            root = None
        return root, str(e)


def xml_structure(root, skip: set, extra_heading: set) -> dict:
    heads, tables, images = [], 0, 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = local(el).lower()
        if name in TABLE_TAGS:
            tables += 1
        elif name in IMAGE_TAGS:
            images += 1
        is_h = name in HEAD_TAGS or name in extra_heading
        if name == "title" and el.getparent() is not None and local(el.getparent()).lower() in HEAD_PARENTS:
            is_h = True
        if is_h and name not in skip:
            heads.append(xml_text(el, skip))
    return {"headings": heads, "tables": tables, "images": images}


# --------------------------------------------------------------------------- #
# Diff engine (sentence level first, then word level inside changed regions)
# --------------------------------------------------------------------------- #
def diff_texts(a_text: str, b_text: str) -> list[dict]:
    sa, sb = split_sentences(a_text), split_sentences(b_text)
    ta_s, tb_s = [tokenize(x) for x in sa], [tokenize(x) for x in sb]
    a_off, b_off = [0], [0]
    for t in ta_s:
        a_off.append(a_off[-1] + len(t))
    for t in tb_s:
        b_off.append(b_off[-1] + len(t))
    ops = difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes()

    # sentences that exist on both sides but at different positions = reordered
    pool: dict[str, list[int]] = {}
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            for j in range(j1, j2):
                if len(tb_s[j]) >= 4:
                    pool.setdefault(sb[j].lower(), []).append(j)
    moved_a: set = set()
    moved_b: set = set()
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            for i in range(i1, i2):
                lst = pool.get(sa[i].lower()) if len(ta_s[i]) >= 4 else None
                if lst:
                    moved_a.add(i)
                    moved_b.add(lst.pop())

    names = {"equal": "equal", "delete": "missing", "insert": "extra", "replace": "changed"}
    segs: list[dict] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            segs.append({"kind": "equal", "a0": a_off[i1], "alen": a_off[i2] - a_off[i1], "b0": b_off[j1],
                         "blen": b_off[j2] - b_off[j1], "a": " ".join(sa[i1:i2]), "b": " ".join(sb[j1:j2])})
            continue
        for i in range(i1, i2):
            if i in moved_a:
                segs.append({"kind": "moved", "a0": a_off[i], "alen": len(ta_s[i]), "b0": b_off[j1], "blen": 0,
                             "a": sa[i], "b": "", "cat": "reading order"})
        for j in range(j1, j2):
            if j in moved_b:
                segs.append({"kind": "moved", "a0": a_off[i1], "alen": 0, "b0": b_off[j], "blen": len(tb_s[j]),
                             "a": "", "b": sb[j], "cat": "reading order"})
        ra = [i for i in range(i1, i2) if i not in moved_a]
        rb = [j for j in range(j1, j2) if j not in moved_b]
        ta = [t for i in ra for t in ta_s[i]]
        tb = [t for j in rb for t in tb_s[j]]
        ga = [a_off[i] + k for i in ra for k in range(len(ta_s[i]))]
        gb = [b_off[j] + k for j in rb for k in range(len(tb_s[j]))]
        if not ta and not tb:
            continue
        if len(ta) * len(tb) > 25_000_000:
            inner = [("replace", 0, len(ta), 0, len(tb))]
        else:
            inner = difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes()
        pos = lambda g, k, base: g[k] if k < len(g) else (g[-1] + 1 if g else base)
        for t, a1, a2, b1, b2 in inner:
            segs.append({"kind": names[t], "a0": pos(ga, a1, a_off[i1]), "alen": a2 - a1,
                         "b0": pos(gb, b1, b_off[j1]), "blen": b2 - b1,
                         "a": " ".join(ta[a1:a2]), "b": " ".join(tb[b1:b2])})

    merged: list[dict] = []
    for s in segs:
        if merged and s["kind"] == "equal" and merged[-1]["kind"] == "equal":
            m = merged[-1]
            m["a"] += " " + s["a"]
            m["b"] += " " + s["b"]
            m["alen"] += s["alen"]
            m["blen"] += s["blen"]
        else:
            merged.append(s)
    segs = merged

    strip = lambda t: re.sub(r"[\W_]+", "", t.lower())
    for s in segs:
        if s["kind"] == "changed":
            if strip(s["a"]) == strip(s["b"]):
                s["cat"] = "spacing / punctuation / case"
                s["soft"] = True
            elif any(NUM_RE.fullmatch(t) for t in (s["a"].split() + s["b"].split())):
                s["cat"] = "number"
            else:
                s["cat"] = "text"
        elif s["kind"] in ("missing", "extra"):
            s["cat"] = "text"
        else:
            s.setdefault("cat", "")
    return segs


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def char_profile(t: str) -> Counter:
    return Counter(c for c in t if not c.isspace() and not (c.isascii() and c.isalnum()))


def compare(pdf_path: Path, xml_path: Path, o) -> dict:
    skip = {s.strip() for s in (o.skip_tags or "").split(",") if s.strip()}
    extra_head = {s.strip().lower() for s in (o.heading_tags or "").split(",") if s.strip()}
    res: dict = {"pdf": str(pdf_path), "xml": str(xml_path), "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "threshold": o.threshold, "warnings": []}

    pdf = extract_pdf(pdf_path, o.keep_headers)
    page_norm = [normalize(p, hyphen_join=True) for p in pdf["pages"]]
    a_text = " ".join(page_norm)
    page_tok = [len(tokenize(p)) for p in page_norm]
    starts = [sum(page_tok[:i]) for i in range(len(page_tok))]
    npages = len(page_tok)

    root, parse_err = parse_xml(xml_path)
    res["wellformed"] = parse_err is None
    res["parse_error"] = parse_err
    res["xsd"] = None
    if root is None:
        res.update(score=0.0, status="FAIL", components={}, segments=[], diffs=[], recommendations=[
            "The XML could not be parsed at all. Fix the converter output first."])
        return res
    b_text = normalize(xml_text(root, skip, extra_head))

    if sum(page_tok) < 20 * max(npages, 1) * 0.1:
        res["warnings"].append("PDF has almost no extractable text - it may be scanned. Run OCR (e.g. ocrmypdf) first.")
    if not b_text:
        res["warnings"].append("XML contains no text.")

    if o.xsd and parse_err is None:
        try:
            schema = etree.XMLSchema(etree.parse(o.xsd))
            ok = schema.validate(root.getroottree())
            res["xsd"] = {"valid": ok, "errors": [f"line {e.line}: {e.message}" for e in schema.error_log][:20]}
        except Exception as e:  # bad schema file
            res["warnings"].append(f"Could not use XSD: {e}")

    # ---- text diff ----
    segs = diff_texts(a_text, b_text)
    na = sum(s["alen"] for s in segs)
    nb = sum(s["blen"] for s in segs)
    eq = sum(s["alen"] for s in segs if s["kind"] == "equal")
    soft = sum(min(s["alen"], s["blen"]) for s in segs if s.get("soft"))
    moved_a = sum(s["alen"] for s in segs if s["kind"] == "moved")
    miss_a = sum(s["alen"] for s in segs if s["kind"] in ("missing", "changed") and not s.get("soft"))
    extra_b = sum(s["blen"] for s in segs if s["kind"] in ("extra", "changed") and not s.get("soft"))
    meff = eq + 0.5 * soft + 0.5 * moved_a
    sim = 2 * meff / (na + nb) if (na + nb) else 1.0

    # page coverage
    unmatched = [0] * max(npages, 1)

    def spread(a0, alen):
        i, end = a0, a0 + alen
        while i < end and npages:
            p = min(max(bisect.bisect_right(starts, i) - 1, 0), npages - 1)
            pend = starts[p] + page_tok[p]
            take = max(min(end, pend) - i, 1) if pend > i else end - i
            unmatched[p] += take
            i += take

    for s in segs:
        if s["kind"] in ("missing", "changed") and not s.get("soft"):
            spread(s["a0"], s["alen"])
    page_cov = []
    for p in range(npages):
        tot = page_tok[p]
        page_cov.append({"page": p + 1, "words": tot, "missing": min(unmatched[p], tot),
                         "coverage": 1 - min(unmatched[p], tot) / tot if tot else 1.0})

    page_of = lambda a0: min(max(bisect.bisect_right(starts, a0) - 1, 0), max(npages - 1, 0)) + 1 if npages else 0
    diffs = []
    for s in segs:
        if s["kind"] != "equal":
            diffs.append({"n": len(diffs) + 1, "type": s["kind"], "category": s["cat"], "page": page_of(s["a0"]),
                          "pdf": s["a"], "xml": s["b"], "words": max(s["alen"], s["blen"])})
    res["diffs"] = diffs
    res["segments"] = segs

    # ---- numbers ----
    ca = Counter(t for t in tokenize(a_text) if NUM_RE.fullmatch(t))
    cb = Counter(t for t in tokenize(b_text) if NUM_RE.fullmatch(t))
    common = sum((ca & cb).values())
    nsum = sum(ca.values()) + sum(cb.values())
    num_score = 2 * common / nsum if nsum else None
    res["numbers"] = {"pdf": sum(ca.values()), "xml": sum(cb.values()),
                      "missing": dict((ca - cb).most_common(30)), "extra": dict((cb - ca).most_common(30))}

    # ---- characters ----
    pa, pb = char_profile(a_text), char_profile(b_text)
    keys = set(pa) | set(pb)
    denom = sum(max(pa[k], pb[k]) for k in keys)
    char_score = 1 - sum(abs(pa[k] - pb[k]) for k in keys) / denom if denom else None
    chars = sorted(((k, pa[k], pb[k]) for k in keys if pa[k] != pb[k]), key=lambda x: -abs(x[1] - x[2]))[:25]
    bad = lambda t: sum(1 for c in t if c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF)
    res["chars"] = {"diff": [{"char": c, "code": f"U+{ord(c):04X}", "name": unicodedata.name(c, "?"), "pdf": x, "xml": y}
                             for c, x, y in chars],
                    "garbled_pdf": bad(a_text), "garbled_xml": bad(b_text)}
    if res["chars"]["garbled_xml"] > res["chars"]["garbled_pdf"]:
        res["warnings"].append("XML contains replacement/private-use characters (font or encoding problem).")

    # ---- structure ----
    xs = xml_structure(root, skip, extra_head)
    ratio = lambda a, b: (min(a, b) / max(a, b)) if max(a, b) else None
    xml_heads = [" ".join(tokenize(normalize(t))) for t in xs["headings"]]
    hrows, hfound = [], 0
    for page, t in pdf["headings"]:
        k = " ".join(tokenize(normalize(t)))
        ok = bool(k) and any(k == x or k in x or (len(x) > 6 and x in k) or
                             difflib.SequenceMatcher(None, k, x).ratio() >= 0.88 for x in xml_heads)
        hfound += ok
        hrows.append({"page": page, "text": t, "found": ok})
    head_score = hfound / len(hrows) if hrows else None
    structure = {
        "headings": {"pdf": len(pdf["headings"]), "xml": len(xml_heads), "found": hfound, "score": head_score,
                     "rows": hrows},
        "tables": {"pdf": pdf["tables"], "xml": xs["tables"],
                   "score": ratio(pdf["tables"], xs["tables"]) if pdf["tables"] is not None else None},
        "images": {"pdf": pdf["images"], "xml": xs["images"], "score": ratio(pdf["images"], xs["images"])},
    }
    res["structure"] = structure
    sw = [(2, head_score), (2, structure["tables"]["score"]), (1, structure["images"]["score"])]
    sw = [(w, s) for w, s in sw if s is not None]
    struct_score = sum(w * s for w, s in sw) / sum(w for w, _ in sw) if sw else None

    # ---- validity ----
    if not res["wellformed"]:
        valid_score = 0.0
    elif res["xsd"] and not res["xsd"]["valid"]:
        valid_score = 0.5
    else:
        valid_score = 1.0

    comps = {
        "Text match": (sim, 45), "Numbers": (num_score, 10), "Characters": (char_score, 10),
        "Structure": (struct_score, 20), "XML validity": (valid_score, 15),
    }
    avail = {k: v for k, v in comps.items() if v[0] is not None}
    score = 100 * sum(s * w for s, w in avail.values()) / sum(w for _, w in avail.values())
    status = "PASS" if score >= o.threshold else "REVIEW" if score >= o.threshold - 10 else "FAIL"
    completeness = 1 - miss_a / na if na else 1.0
    if completeness < 0.90:
        status = "FAIL"                      # a lot of PDF text is missing
    elif completeness < o.min_completeness and status == "PASS":
        status = "REVIEW"                    # never PASS when content is lost
    if not res["wellformed"]:
        status = "FAIL"
    res["components"] = {k: {"score": v[0], "weight": v[1]} for k, v in comps.items()}
    res["score"] = round(score, 2)
    res["status"] = status
    res["metrics"] = {
        "pdf_pages": npages, "pdf_words": na, "xml_words": nb, "matched_words": eq,
        "similarity": sim, "completeness": 1 - miss_a / na if na else 1.0,
        "missing_words": miss_a, "extra_words": extra_b, "tolerated_words": soft, "reordered_words": moved_a,
        "removed_header_footer_lines": pdf["removed_lines"],
    }
    res["page_coverage"] = page_cov
    res["recommendations"] = recommendations(res)
    return res


def recommendations(r: dict) -> list[str]:
    out, m = [], r["metrics"]
    if not r["wellformed"]:
        out.append("XML is not well-formed. Fix the writer/escaping step (unescaped &, <, bad encoding) before anything else.")
    if r.get("xsd") and not r["xsd"]["valid"]:
        out.append("XML fails schema validation. Check required elements/attributes in the mapping rules.")
    low = [p["page"] for p in r["page_coverage"] if p["words"] >= 20 and p["coverage"] < 0.9]
    if low:
        out.append(f"Text is missing on pages {', '.join(map(str, low[:15]))}{'...' if len(low) > 15 else ''}. "
                   "Open these pages: usual causes are tables, footnotes, text boxes, text inside images, or columns dropped by the converter.")
    if m["missing_words"] > 0.02 * max(m["pdf_words"], 1):
        out.append("More than 2% of PDF words are missing in the XML - fix the converter, do not patch the XML by hand.")
    if m["reordered_words"]:
        out.append("Some blocks appear in a different order (multi-column or sidebar reading order). Adjust column/reading-order settings.")
    if r["numbers"]["missing"] or r["numbers"]["extra"]:
        out.append("Numbers differ (see Numbers section). Check tables, footnote markers and OCR errors - numeric errors are the most costly.")
    if any(c["pdf"] != c["xml"] and c["name"].startswith(("LATIN SMALL LIGATURE", "GREEK", "MATHEMATICAL")) for c in r["chars"]["diff"]):
        out.append("Ligatures, Greek or math symbols differ. Check font mapping / Unicode handling in the converter.")
    if m["extra_words"] > 0.05 * max(m["xml_words"], 1):
        out.append("XML has >5% extra words not in the PDF (metadata, generated text or duplicates). Use --skip-tags for known metadata elements.")
    miss_h = [h["text"] for h in r["structure"]["headings"]["rows"] if not h["found"]]
    if miss_h:
        out.append(f"{len(miss_h)} PDF heading(s) not found as headings in the XML - check heading mapping (heading detection from the PDF is a heuristic).")
    if r["structure"]["tables"]["pdf"] not in (None, r["structure"]["tables"]["xml"]):
        out.append("Table count differs between PDF and XML. Review table detection.")
    if not out:
        out.append("No action needed. Spot-check a few pages and sign off.")
    else:
        out.append("After fixing the cause, re-run the conversion and this comparison until status is PASS; keep the report as sign-off evidence.")
    return out


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--card:#f6f8fa;--bd:#d0d7de;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;
--delbg:#ffebe9;--insbg:#dafbe1;--mvbg:#fff8c5}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8d96a0;--card:#161b22;--bd:#30363d;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--delbg:#5b1b1e;--insbg:#12351c;--mvbg:#4a3b0b}}
*{box-sizing:border-box}body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1200px;margin:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;border-bottom:1px solid var(--bd);padding-bottom:4px}
.mut{color:var(--mut)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px}
.card b{display:block;font-size:24px}.badge{display:inline-block;padding:3px 12px;border-radius:14px;color:#fff;font-weight:600}
.PASS{background:var(--ok)}.REVIEW{background:var(--warn)}.FAIL{background:var(--bad)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid var(--bd);padding:5px 8px;text-align:left;vertical-align:top}
th{background:var(--card)}.wrap{overflow-x:auto}del{background:var(--delbg);text-decoration:line-through}
ins{background:var(--insbg);text-decoration:none}.mv{background:var(--mvbg)}.diff{white-space:pre-wrap;word-break:break-word}
li{margin:4px 0}code{background:var(--card);padding:1px 4px;border-radius:4px}.c-ok{color:var(--ok)}.c-bad{color:var(--bad)}
"""
e = html.escape
pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"


def clip(t: str, n=300) -> str:
    return t if len(t) <= n else t[:n] + " ..."


def render_html(r: dict, max_rows: int = 500) -> str:
    m = r.get("metrics", {})
    h = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>PDF vs XML - {e(Path(r['pdf']).name)}</title><style>{CSS}</style></head><body>"]
    h.append(f"<h1>PDF &rarr; XML content comparison</h1><div class='mut'>{e(r['pdf'])} &nbsp;vs&nbsp; {e(r['xml'])} &middot; {r['generated']}</div>")
    h.append(f"<p><span class='badge {r['status']}'>{r['status']}</span> &nbsp; <b style='font-size:22px'>{r['score']:.1f}</b> / 100 "
             f"<span class='mut'>(pass mark {r['threshold']})</span></p>")
    for w in r["warnings"]:
        h.append(f"<p class='c-bad'>&#9888; {e(w)}</p>")
    if not r["wellformed"]:
        h.append(f"<p class='c-bad'>XML not well-formed: {e(r['parse_error'] or '')}</p>")
    if "components" in r and r["components"]:
        h.append("<h2>Score breakdown</h2><div class='grid'>")
        for k, v in r["components"].items():
            h.append(f"<div class='card'><span class='mut'>{k} (weight {v['weight']})</span><b>{pct(v['score'])}</b></div>")
        h.append("</div>")
        h.append("<h2>What to do next</h2><ol>" + "".join(f"<li>{e(x)}</li>" for x in r["recommendations"]) + "</ol>")
        h.append("<h2>Text metrics</h2><div class='grid'>")
        cards = [("PDF pages", m["pdf_pages"]), ("PDF words", f"{m['pdf_words']:,}"), ("XML words", f"{m['xml_words']:,}"),
                 ("Completeness", pct(m["completeness"])), ("Missing words", f"{m['missing_words']:,}"),
                 ("Extra words", f"{m['extra_words']:,}"), ("Reordered words", f"{m['reordered_words']:,}"),
                 ("Tolerated (spacing/case)", f"{m['tolerated_words']:,}"),
                 ("Header/footer lines ignored", m["removed_header_footer_lines"])]
        h += [f"<div class='card'><span class='mut'>{a}</span><b>{b}</b></div>" for a, b in cards]
        h.append("</div>")

        s = r["structure"]
        h.append("<h2>Structure</h2><div class='wrap'><table><tr><th>Item</th><th>PDF</th><th>XML</th><th>Match</th></tr>")
        for name in ("headings", "tables", "images"):
            d = s[name]
            h.append(f"<tr><td>{name.title()}</td><td>{'n/a' if d['pdf'] is None else d['pdf']}</td><td>{d['xml']}</td><td>{pct(d['score'])}</td></tr>")
        h.append("</table></div><p class='mut'>PDF structure is inferred (font size, ruling lines, embedded images) so treat it as a guide.</p>")
        miss = [x for x in s["headings"]["rows"] if not x["found"]]
        if miss:
            h.append("<p><b>PDF headings not found in XML headings:</b></p><ul>" +
                     "".join(f"<li>p.{x['page']}: {e(clip(x['text'], 120))}</li>" for x in miss[:30]) + "</ul>")

        h.append("<h2>Page coverage</h2><p class='mut'>Share of each page's words found in the XML. Pages below 90% are listed first.</p>"
                 "<div class='wrap'><table><tr><th>Page</th><th>Words</th><th>Missing</th><th>Coverage</th></tr>")
        pc = sorted(r["page_coverage"], key=lambda p: p["coverage"])
        shown = [p for p in pc if p["coverage"] < 0.999][:40] or pc[:5]
        for p in shown:
            cls = "c-bad" if p["coverage"] < 0.9 else ""
            h.append(f"<tr><td>{p['page']}</td><td>{p['words']}</td><td>{p['missing']}</td><td class='{cls}'>{pct(p['coverage'])}</td></tr>")
        h.append("</table></div>")

        n = r["numbers"]
        h.append(f"<h2>Numbers</h2><p>PDF: {n['pdf']} &middot; XML: {n['xml']}</p>")
        if n["missing"] or n["extra"]:
            h.append("<p><b>In PDF, not in XML:</b> " + ", ".join(f"<code>{e(k)}</code>&times;{v}" for k, v in n["missing"].items()) + "</p>"
                     "<p><b>In XML, not in PDF:</b> " + ", ".join(f"<code>{e(k)}</code>&times;{v}" for k, v in n["extra"].items()) + "</p>")
        else:
            h.append("<p class='c-ok'>All numbers match.</p>")

        c = r["chars"]
        h.append(f"<h2>Characters &amp; encoding</h2><p>Garbled characters - PDF: {c['garbled_pdf']}, XML: {c['garbled_xml']}</p>")
        if c["diff"]:
            h.append("<div class='wrap'><table><tr><th>Char</th><th>Code</th><th>Name</th><th>PDF</th><th>XML</th></tr>" +
                     "".join(f"<tr><td>{e(x['char'])}</td><td>{x['code']}</td><td>{e(x['name'])}</td><td>{x['pdf']}</td><td>{x['xml']}</td></tr>"
                             for x in c["diff"]) + "</table></div>")
        if r["xsd"]:
            ok = r["xsd"]["valid"]
            h.append(f"<h2>Schema validation</h2><p class='{'c-ok' if ok else 'c-bad'}'>{'Valid' if ok else 'Invalid'}</p>")
            h.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in r["xsd"]["errors"]) + "</ul>")

        d = r["diffs"]
        counts = Counter((x["type"], x["category"]) for x in d)
        h.append(f"<h2>Differences ({len(d)})</h2>")
        h.append("<p>" + " &middot; ".join(f"{t}/{c or '-'}: {v}" for (t, c), v in counts.most_common()) + "</p>")
        h.append("<div class='wrap'><table><tr><th>#</th><th>Type</th><th>Category</th><th>PDF page</th><th>PDF text</th><th>XML text</th></tr>")
        for x in d[:max_rows]:
            h.append(f"<tr><td>{x['n']}</td><td>{x['type']}</td><td>{e(x['category'])}</td><td>{x['page']}</td>"
                     f"<td>{e(clip(x['pdf']))}</td><td>{e(clip(x['xml']))}</td></tr>")
        h.append("</table></div>")
        if len(d) > max_rows:
            h.append(f"<p class='mut'>{len(d) - max_rows} more differences in the JSON/Excel export.</p>")

        h.append("<h2>Inline diff</h2><p class='mut'>Unchanged text is shortened. <del>red</del> = only in PDF, <ins>green</ins> = only in XML, "
                 "<span class='mv'>yellow</span> = same text but different position.</p><div class='card diff'>")
        for sg in r["segments"][:3000]:
            k = sg["kind"]
            if k == "equal":
                w = sg["a"].split()
                txt = sg["a"] if len(w) <= 30 else " ".join(w[:12]) + " &hellip; " + " ".join(w[-12:])
                h.append(txt if len(w) > 30 else e(txt))
                if len(w) > 30:
                    h[-1] = e(" ".join(w[:12])) + " &hellip; " + e(" ".join(w[-12:]))
                h.append(" ")
            elif k == "missing":
                h.append(f"<del>{e(sg['a'])}</del> ")
            elif k == "extra":
                h.append(f"<ins>{e(sg['b'])}</ins> ")
            elif k == "moved":
                h.append(f"<span class='mv'>{e(clip(sg['a'] or sg['b'], 200))}</span> ")
            else:
                h.append(f"<del>{e(sg['a'])}</del><ins>{e(sg['b'])}</ins> ")
        h.append("</div>")
    h.append("</body></html>")
    return "".join(h)


def write_xlsx(results: list[dict], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["PDF", "XML", "Status", "Score", "Text match", "Completeness", "Missing words", "Extra words", "Well-formed"])
    for r in results:
        m = r.get("metrics", {})
        ws.append([Path(r["pdf"]).name, Path(r["xml"]).name, r["status"], r["score"],
                   (r["components"].get("Text match") or {}).get("score"), m.get("completeness"),
                   m.get("missing_words"), m.get("extra_words"), r["wellformed"]])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in results:
        ws2 = wb.create_sheet(Path(r["pdf"]).stem[:28] + "_diff")
        ws2.append(["#", "Type", "Category", "PDF page", "PDF text", "XML text"])
        for d in r["diffs"]:
            ws2.append([d["n"], d["type"], d["category"], d["page"], d["pdf"][:32000], d["xml"][:32000]])
        for c in ws2[1]:
            c.font = Font(bold=True)
    wb.save(str(path))


def slim(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "segments"}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Compare PDF content with converted XML.")
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("xml", nargs="?")
    ap.add_argument("--batch", nargs=2, metavar=("PDF_DIR", "XML_DIR"))
    ap.add_argument("--out", default="reports", help="output folder for --batch")
    ap.add_argument("--report", help="HTML report path (single mode)")
    ap.add_argument("--json")
    ap.add_argument("--xlsx")
    ap.add_argument("--threshold", type=float, default=95.0)
    ap.add_argument("--min-completeness", type=float, default=0.98,
                    help="PASS needs at least this share of PDF words found in the XML (default 0.98)")
    ap.add_argument("--skip-tags")
    ap.add_argument("--heading-tags")
    ap.add_argument("--xsd")
    ap.add_argument("--keep-headers", action="store_true")
    ap.add_argument("--max-rows", type=int, default=500)
    o = ap.parse_args()

    if o.batch:
        pdf_dir, xml_dir, out = Path(o.batch[0]), Path(o.batch[1]), Path(o.out)
        out.mkdir(parents=True, exist_ok=True)
        xmls = {p.stem.lower(): p for p in xml_dir.glob("*.xml")}
        results = []
        for pdf in sorted(pdf_dir.glob("*.pdf")):
            x = xmls.get(pdf.stem.lower())
            if not x:
                print(f"[skip] no XML for {pdf.name}")
                continue
            r = compare(pdf, x, o)
            (out / f"{pdf.stem}.html").write_text(render_html(r, o.max_rows), encoding="utf-8")
            results.append(r)
            print(f"{r['status']:6} {r['score']:6.1f}  {pdf.name}")
        with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pdf", "xml", "status", "score", "completeness", "missing_words", "extra_words"])
            for r in results:
                m = r.get("metrics", {})
                w.writerow([Path(r["pdf"]).name, Path(r["xml"]).name, r["status"], r["score"],
                            m.get("completeness"), m.get("missing_words"), m.get("extra_words")])
        (out / "summary.json").write_text(json.dumps([slim(r) for r in results], indent=1, default=str), encoding="utf-8")
        if o.xlsx or results:
            write_xlsx(results, Path(o.xlsx) if o.xlsx else out / "summary.xlsx")
        print(f"Reports written to {out}/")
        return 0 if results and all(r["status"] == "PASS" for r in results) else 1

    if not (o.pdf and o.xml):
        ap.error("give PDF and XML files, or use --batch")
    for label, f, ext in (("PDF", o.pdf, ".pdf"), ("XML", o.xml, ".xml")):
        if not Path(f).is_file():
            print(f"ERROR: {label} file not found: {f}")
            return 2
    if Path(o.pdf).suffix.lower() != ".pdf":
        print(f"ERROR: first argument must be the PDF, got: {o.pdf}")
        return 2
    if Path(o.xml).suffix.lower() == ".pdf" or Path(o.xml).read_bytes()[:5] == b"%PDF-":
        print("ERROR: the second argument is a PDF. Give the converted XML file as the second argument:\n"
              "  python pdf_xml_compare.py input.pdf converted.xml --report report.html")
        return 2
    r = compare(Path(o.pdf), Path(o.xml), o)
    report = Path(o.report or f"{Path(o.pdf).stem}_report.html")
    report.write_text(render_html(r, o.max_rows), encoding="utf-8")
    if o.json:
        Path(o.json).write_text(json.dumps(slim(r), indent=1, default=str), encoding="utf-8")
    if o.xlsx:
        write_xlsx([r], Path(o.xlsx))
    m = r.get("metrics", {})
    print(f"{r['status']}  score {r['score']:.1f}/100  completeness {pct(m.get('completeness', 0))}  "
          f"missing {m.get('missing_words', 0)}  extra {m.get('extra_words', 0)}")
    print(f"Report: {report}")
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
