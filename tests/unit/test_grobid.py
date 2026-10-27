"""GROBID as a second reader of references and article headers (``publishing/grobid.py``).

A small HTTP server stands in for GROBID: it answers ``/api/version``,
``/api/processCitationList`` and ``/api/processHeaderDocument`` with TEI in GROBID's format.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from lxml import etree

from pdf2xml.model import Document, Heading, Meta, PageInfo, Paragraph, Run, Style
from pdf2xml.publishing import Metadata, Profile, export
from pdf2xml.publishing.citations import parse_reference
from pdf2xml.publishing.grobid import (
    CACHE_FILE,
    Bibl,
    Grobid,
    Name,
    align,
    fill,
    read_citations,
    read_header,
    with_authors,
)
from pdf2xml.publishing.profile import PartMeta, Person

BOX = (50.0, 100.0, 550.0, 120.0)
STYLES = {"body": Style(font="Times", size=10.0, role="body"),
          "h1": Style(font="Arial", size=18.0, bold=True, role="heading", level=1),
          "h2": Style(font="Arial", size=12.0, bold=True, role="heading", level=2)}

# MLA and an organisation as author: the built-in reader reads neither
MLA = ('Smith, John, and Jane Doe. "Rethinking the Archive." Journal of Things, vol. 12, '
       'no. 3, 2010, pp. 45-67.')
ORG = "World Health Organization 2021, Global report on things, WHO Press, Geneva."
APA = "Lee, C. (2018). A study of tests. Journal of Tests, 5(2), 10–20."

TEI_NS = "http://www.tei-c.org/ns/1.0"
BIBLS = {
    MLA: """<biblStruct>
      <analytic>
        <title level="a" type="main">Rethinking the Archive</title>
        <author><persName><forename type="first">John</forename><surname>Smith</surname>
        </persName></author>
        <author><persName><forename type="first">Jane</forename><surname>Doe</surname>
        </persName></author>
      </analytic>
      <monogr>
        <title level="j">Journal of Things</title>
        <imprint><biblScope unit="volume">12</biblScope><biblScope unit="issue">3</biblScope>
          <biblScope unit="page" from="45" to="67"/>
          <date type="published" when="2010">2010</date></imprint>
      </monogr>
      <note type="raw_reference">{raw}</note></biblStruct>""",
    ORG: """<biblStruct>
      <monogr>
        <title level="m" type="main">Global report on things</title>
        <author><orgName>World Health Organization</orgName></author>
        <imprint><publisher>WHO Press</publisher><pubPlace>Geneva</pubPlace>
          <date type="published" when="2021">2021</date></imprint>
      </monogr>
      <note type="raw_reference">{raw}</note></biblStruct>""",
}

HEADER = f"""<TEI xmlns="{TEI_NS}"><teiHeader><fileDesc><sourceDesc><biblStruct><analytic>
  <author><persName><forename type="first">Mallory</forename><surname>Bejster</surname>
    </persName><email>m.bejster@uni.example</email>
    <idno type="ORCID">0000-0002-1825-0097</idno>
    <affiliation key="aff0"><note type="raw_affiliation"><label>1</label> Department of
      Testing, Example University, Springfield, USA</note>
      <orgName type="department">Department of Testing</orgName></affiliation></author>
  <author><persName><forename type="first">Ghost</forename><surname>Writer</surname>
    </persName></author>
</analytic></biblStruct></sourceDesc></fileDesc></teiHeader></TEI>"""


def _tei(citations: list[str]) -> bytes:
    body = "".join(BIBLS.get(c, "<biblStruct><monogr><imprint/></monogr></biblStruct>")
                   .format(raw=c.replace("&", "&amp;").replace("<", "&lt;"))
                   for c in citations)
    return (f'<TEI xmlns="{TEI_NS}"><teiHeader/><text><back><div><listBibl>{body}'
            "</listBibl></div></back></text></TEI>").encode()


class _Fake(BaseHTTPRequestHandler):
    version = "0.9.1"
    seen: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, data: bytes, ctype: str = "application/xml") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/version":         # as GROBID 0.9 answers
            self._send(json.dumps({"version": type(self).version, "revision": "x"}).encode(),
                       "application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/processCitationList":
            form = urllib.parse.parse_qs(body.decode("utf-8"))
            assert form["consolidateCitations"] == ["0"]
            type(self).seen.extend(form["citations"])
            self._send(_tei(form["citations"]))
        elif self.path == "/api/processHeaderDocument":
            assert b'name="input"' in body and b"%PDF" in body
            self._send(HEADER.encode())
        else:
            self.send_error(404)


@pytest.fixture
def server() -> Iterator[str]:
    _Fake.seen = []
    _Fake.version = "0.9.1"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _closed_port() -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    port = srv.server_address[1]
    srv.server_close()
    return f"http://127.0.0.1:{port}"


def doc(*body: object) -> Document:
    d = Document(meta=Meta(source="t.pdf", sha256="a" * 64, pages=1), styles=STYLES,
                 pages=[PageInfo(n=1, width=600, height=800)], body=list(body))  # type: ignore[arg-type]
    for i, b in enumerate(d.body, 1):
        b.id = f"b{i}"
    return d


def h(text: str, level: int, style: str) -> Heading:
    return Heading(page=1, bbox=BOX, level=level, style=style, runs=[Run(text=text)])


def p(text: str) -> Paragraph:
    return Paragraph(page=1, bbox=BOX, style="body", runs=[Run(text=text)])


def article() -> Document:
    return doc(h("A Title", 1, "h1"), p("Mallory Bejster"), h("INTRO", 2, "h2"),
               p("As Smith and Doe (2010) and the World Health Organization (2021) and Lee "
                 "(2018) show."), h("REFERENCES", 2, "h2"), p(MLA), p(ORG), p(APA))


def _spans(text: str, parsed: object) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def walk(spans: list) -> None:  # type: ignore[type-arg]
        for sp in spans:
            out.setdefault(sp.tag, []).append(text[sp.start:sp.end])
            walk(sp.children)

    walk(parsed.spans)  # type: ignore[attr-defined]
    return out


# ---------------------------------------------------------------- reading GROBID's TEI

def test_citation_tei_is_read() -> None:
    (raw, b), (_, o), (_, empty) = read_citations(_tei([MLA, ORG, "nonsense"]))
    assert raw == " ".join(MLA.split())
    assert [(n.surname, n.given) for n in b.authors] == [("Smith", ["John"]), ("Doe", ["Jane"])]
    assert (b.title_a, b.title_j, b.year, b.volume, b.issue, b.fpage, b.lpage) == (
        "Rethinking the Archive", "Journal of Things", "2010", "12", "3", "45", "67")
    assert o.authors == [Name(org="World Health Organization")]
    assert (o.title_m, o.publisher, o.place) == ("Global report on things", "WHO Press", "Geneva")
    assert empty == Bibl()


def test_header_tei_is_read() -> None:
    first, second = read_header(HEADER.encode())
    assert (first.given, first.surname, first.email, first.orcid) == (
        "Mallory", "Bejster", "m.bejster@uni.example", "0000-0002-1825-0097")
    # the printed affiliation, without its marker
    assert first.aff == "Department of Testing, Example University, Springfield, USA"
    assert second.surname == "Writer" and second.aff is None


# ---------------------------------------------------------------- placing fields on the text

@pytest.mark.parametrize(("text", "bibl", "want"), [
    (MLA, Bibl(authors=[Name("Smith", ["John"]), Name("Doe", ["Jane"])],
               title_a="Rethinking the Archive", title_j="Journal of Things", year="2010",
               volume="12", issue="3", fpage="45", lpage="67"),
     {"surname": ["Smith", "Doe"], "given-names": ["John", "Jane"], "year": ["2010"],
      "article-title": ["Rethinking the Archive"], "source": ["Journal of Things"],
      "volume": ["12"], "issue": ["3"], "fpage": ["45"], "lpage": ["67"]}),
    # Vancouver: initials run together, "et al.", a shortened last page as printed
    ("Smith JA, Doe B, et al. Title of the article. J Things. 2010;12(3):45-7.",
     Bibl(authors=[Name("Smith", ["J", "A"]), Name("Doe", ["B"])],
          title_a="Title of the article", title_j="J Things", year="2010", volume="12",
          issue="3", fpage="45", lpage="47"),
     {"given-names": ["JA", "B"], "etal": ["et al."], "lpage": ["7"], "source": ["J Things"]}),
    # Chicago chapter: editors after the book title, a whole first name before the period
    ("Lee, Chris. 2018. “Chapter Name.” In The Big Book, edited by Anne Smith and Bob Jones, "
     "45–67. New York: Springer.",
     Bibl(authors=[Name("Lee", ["Chris"])], editors=[Name("Smith", ["Anne"]),
          Name("Jones", ["Bob"])], title_a="Chapter Name", title_m="The Big Book",
          year="2018", fpage="45", lpage="67", publisher="Springer", place="New York"),
     {"string-name": ["Lee, Chris", "Anne Smith", "Bob Jones"], "chapter-title": ["Chapter Name"],
      "source": ["The Big Book"], "publisher-loc": ["New York"],
      "publisher-name": ["Springer"]}),
    # Vancouver book: the period after the initials ends the author list (found on a real book)
    ("Charon R. Narrative Medicine: Honoring the Stories of Illness. Oxford University Press; "
     "2006.",
     Bibl(authors=[Name("Charon", ["R"])], title_m="Narrative Medicine: Honoring the Stories of "
          "Illness", year="2006", publisher="Oxford University Press"),
     {"given-names": ["R"], "string-name": ["Charon R"], "publisher-name":
      ["Oxford University Press"]}),
    # "accessed 22 January" after the URL is no page number, even when GROBID says so
    ("ANCIR (2017) ‘The Panama Papers’. Available at: https://example.org/panama "
     "(accessed 22 January 2018).",
     Bibl(authors=[Name("ANCIR")], title_m="The Panama Papers", year="2017", fpage="22"),
     {"surname": ["ANCIR"], "source": ["The Panama Papers"], "year": ["2017"]}),
    (ORG, Bibl(authors=[Name(org="World Health Organization")],
               title_m="Global report on things", year="2021", publisher="WHO Press",
               place="Geneva"),
     {"collab": ["World Health Organization"], "source": ["Global report on things"],
      "publisher-name": ["WHO Press"], "publisher-loc": ["Geneva"]}),
])
def test_fields_are_placed_on_the_printed_text(text: str, bibl: Bibl,
                                               want: dict[str, list[str]]) -> None:
    parsed = align(text, bibl)
    assert parsed is not None
    got = _spans(text, parsed)
    for tag, values in want.items():
        assert got.get(tag) == values, tag
    if "ANCIR" in text:
        assert "fpage" not in got


def test_a_name_inside_a_longer_word_is_not_used() -> None:
    # "Smith" must not be found inside "Smithson"; without names the reading is not used
    assert align("Smithson, P. (2001). A title. Some Press.",
                 Bibl(authors=[Name("Smith", ["P"])], title_m="A title", year="2001")) is None


def test_fields_that_are_not_printed_are_left_out() -> None:
    parsed = align(APA, Bibl(authors=[Name("Lee", ["C"])], title_a="A study of tests",
                             title_j="Journal of Tests", year="2018", volume="5", issue="2",
                             fpage="10", lpage="20", publisher="Not Printed Press"))
    assert parsed is not None
    assert "publisher-name" not in _spans(APA, parsed)


def test_prefer_keeps_what_only_the_built_in_reader_found() -> None:
    # GROBID left out the edition (seen on real books); the built-in reading has it
    text = "Rothman, K. J. (2020). Modern epidemiology (4th ed.). Lippincott."
    grobid = align(text, Bibl(authors=[Name("Rothman", ["K", "J"])], year="2020",
                              title_m="Modern epidemiology", publisher="Lippincott"))
    assert grobid is not None and "edition" not in _spans(text, grobid)
    title = text.index("Modern")
    got = _spans(text, fill(grobid, parse_reference(text, [(title, title + 19)])))
    assert got["edition"] == ["4th ed."]
    assert got["publisher-name"] == ["Lippincott"]


# ---------------------------------------------------------------- the client

def test_client_asks_once_and_keeps_the_answers(server: str, tmp_path: Path) -> None:
    cache = tmp_path / CACHE_FILE
    g = Grobid(server, cache)
    got = g.citations([MLA, ORG, MLA])
    assert got[0] is not None and got[0].title_j == "Journal of Things"
    assert got[1] is not None and got[1].authors[0].org == "World Health Organization"
    assert got[2] == got[0]
    assert _Fake.seen == [MLA, ORG]                     # each text sent once
    g.citations([MLA])
    assert _Fake.seen == [MLA, ORG]                     # answered from memory
    g.save()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["version"] == "0.9.1" and len(data["citations"]) == 2

    # without the server, the kept answers are used and the server is reported once
    off = Grobid(_closed_port(), cache)
    assert off.citations([MLA])[0] == got[0]
    assert off.citations([APA]) == [None]
    assert len(off.errors) == 1 and "not reachable" in off.errors[0]


def test_a_new_grobid_version_drops_the_kept_answers(server: str, tmp_path: Path) -> None:
    cache = tmp_path / CACHE_FILE
    g = Grobid(server, cache)
    g.citations([MLA])
    g.save()
    _Fake.version = "0.9.2"
    _Fake.seen = []
    Grobid(server, cache).citations([MLA])
    assert _Fake.seen == [MLA]


def test_header_is_read_once_per_pdf(server: str, tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    g = Grobid(server, tmp_path / CACHE_FILE)
    people = g.header(pdf, "a" * 64)
    assert [x.surname for x in people] == ["Bejster", "Writer"]
    g.save()
    # a re-export has no PDF: the kept header is used when it was made from the same PDF
    again = Grobid(_closed_port(), tmp_path / CACHE_FILE)
    assert [x.surname for x in again.header(None, "a" * 64)] == ["Bejster", "Writer"]
    assert again.header(None, "b" * 64) == []


def test_only_printed_authors_are_used_and_metadata_wins() -> None:
    d = article()
    people = read_header(HEADER.encode())
    meta = with_authors(Metadata(), people, d)
    assert [x.surname for x in meta.part.contributors] == ["Bejster"]   # "Writer" is not printed
    given = Metadata(part=PartMeta(contributors=[Person(surname="Given")]))
    assert with_authors(given, people, d) is given


# ---------------------------------------------------------------- in the export

def _refs(root: etree._Element) -> list[etree._Element]:
    return root.findall(".//ref-list/ref/mixed-citation")


def test_export_tags_references_only_grobid_can_read(server: str, tmp_path: Path) -> None:
    assert parse_reference(MLA) is None and parse_reference(ORG) is None
    assert parse_reference(APA) is not None
    g = Grobid(server, tmp_path / CACHE_FILE)
    res = export(article(), tmp_path, "jats", Profile(), Metadata(), grobid=g)
    assert res.valid, res.errors
    assert res.stats["references_tagged"] == 3
    assert res.stats["references_tagged_grobid"] == 2
    assert _Fake.seen == [MLA, ORG]                 # "fallback": only what the reader cannot
    root = etree.parse(str(res.path)).getroot()
    mla, org, apa = _refs(root)
    # every printed character stays, in order
    assert " ".join("".join(mla.itertext()).split()) == MLA
    assert [e.text for e in mla.iter("surname")] == ["Smith", "Doe"]
    assert mla.findtext("source") == "Journal of Things"
    assert org.findtext(".//collab") == "World Health Organization"
    assert apa.findtext(".//surname") == "Lee"
    # the citations now find their references
    assert res.stats.get("citations_unresolved", 0) == 0


def test_prefer_asks_grobid_for_every_reference(server: str, tmp_path: Path) -> None:
    g = Grobid(server, tmp_path / CACHE_FILE)
    res = export(article(), tmp_path, "jats", Profile(grobid_references="prefer"), Metadata(),
                 grobid=g)
    assert res.valid, res.errors
    assert _Fake.seen == [MLA, ORG, APA]
    # GROBID has no answer for APA here: the built-in reading stays
    assert res.stats["references_tagged"] == 3


def test_without_a_server_the_output_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    plain = export(article(), tmp_path / "a", "jats", Profile(), Metadata())
    off = export(article(), tmp_path / "b", "jats", Profile(), Metadata(),
                 grobid=Grobid(_closed_port(), None))
    assert plain.path.read_bytes() == off.path.read_bytes()
    assert any("GROBID is not reachable" in w for w in off.warnings)
    assert plain.stats["references_tagged"] == 1


def test_pipeline_writes_authors_with_affiliation(server: str, tmp_path: Path) -> None:
    from pdf2xml.config import Config
    from pdf2xml.pipeline import publish

    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    exports = publish(article(), tmp_path, Config(targets=["jats"], grobid_url=server),
                      pdf=pdf)
    root = etree.parse(str(exports["jats"].path)).getroot()
    contrib = root.find(".//article-meta/contrib-group/contrib")
    assert contrib is not None
    assert contrib.findtext(".//surname") == "Bejster"
    assert contrib.findtext("aff", "").startswith("Department of Testing")
    assert contrib.findtext("email") == "m.bejster@uni.example"
    assert (tmp_path / CACHE_FILE).exists()
