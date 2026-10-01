"""``pdf2xml serve``: rendering through XSLT, checks, live-reload stamps and path safety."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from pdf2xml.preview import Viewer, make_server
from pdf2xml.publishing import Metadata, Profile, export
from tests.unit.test_publishing import doc, h, p

CUSTOM = """<?xml version="1.0"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html"/>
  <xsl:template match="/">
    <html><head><link rel="stylesheet" href="house.css"/></head>
      <body><xsl:for-each select="//sec/title"><h2><xsl:value-of select="."/></h2></xsl:for-each>
      </body></html>
  </xsl:template>
</xsl:stylesheet>
"""


@pytest.fixture()
def out(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    d = root / "chapter"
    d.mkdir(parents=True)
    body = doc(h("A Title", 1, "h1"), h("INTRO", 2, "h2"), p("See the text."),
               p("More on the next page.", page=2))
    res = export(body, d, "bits", Profile(), Metadata())
    assert res.valid, res.errors
    (d / "qa").mkdir()
    (d / "qa" / "page-001.png").write_bytes(b"\x89PNG fake")
    (root / "_xslt").mkdir()
    (root / "_xslt" / "house.xsl").write_text(CUSTOM, encoding="utf-8")
    (root / "_xslt" / "house.css").write_text("h2 { color: red }", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    return root


@pytest.fixture()
def url(out: Path) -> Iterator[str]:
    viewer = Viewer(out)
    server = make_server(viewer, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    viewer.close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_lists_documents_files_and_stylesheets(url: str) -> None:
    status, body = get(url + "/api/docs")
    data = json.loads(body)
    assert status == 200
    assert data["docs"] == [{"name": "chapter", "files": ["40_bits.xml"], "pages": 1}]
    assert [s["id"] for s in data["sheets"]] == ["proof", "canonical", "custom1"]
    assert get(url + "/")[1].startswith("<!doctype html>")


def test_proof_sheet_renders_with_source_lines_and_page_markers(url: str) -> None:
    status, html = get(url + "/view/proof/chapter/40_bits.xml")
    assert status == 200
    assert 'data-tag="sec"' in html and 'data-line="' in html
    assert 'class="pv-page"' in html                  # the page-2 marker
    assert "pv-unknown" not in html                    # every element has a preview rule
    assert get(url + "/view/proof/chapter/preview.css")[0] == 200


def test_custom_stylesheet_and_its_css(url: str) -> None:
    status, html = get(url + "/view/custom1/chapter/40_bits.xml")
    assert status == 200 and "<h2>INTRO</h2>" in html
    assert get(url + "/view/custom1/chapter/house.css") == (200, "h2 { color: red }")


def test_validation_and_broken_xml(url: str, out: Path) -> None:
    v = json.loads(get(url + "/api/validate?doc=chapter&file=40_bits.xml")[1])
    assert v == {"schema": "bits", "errors": []}
    (out / "chapter" / "broken.xml").write_text("<book-part-wrapper><p>open", encoding="utf-8")
    v = json.loads(get(url + "/api/validate?doc=chapter&file=broken.xml")[1])
    assert v["schema"] == "well-formedness" and v["errors"][0]["line"] == 1
    status, html = get(url + "/view/proof/chapter/broken.xml")
    assert status == 500 and "could not be rendered" in html


def test_stamp_changes_when_the_xml_or_the_stylesheet_changes(out: Path) -> None:
    v = Viewer(out)
    first = v.stamp("chapter", "40_bits.xml", "custom1")
    xml = out / "chapter" / "40_bits.xml"
    st = xml.stat()
    os.utime(xml, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = v.stamp("chapter", "40_bits.xml", "custom1")
    css = out / "_xslt" / "house.css"
    cs = css.stat()
    os.utime(css, ns=(cs.st_atime_ns, cs.st_mtime_ns + 5_000_000_000))
    assert len({first, second, v.stamp("chapter", "40_bits.xml", "custom1")}) == 3


def test_paths_outside_the_output_folder_are_refused(url: str) -> None:
    for path in ("/files/chapter/../../secret.txt", "/files/chapter/..%2F..%2Fsecret.txt",
                 "/files/..%2Fsecret.txt/x", "/view/proof/chapter/..%2F..%2Fsecret.txt",
                 "/api/source?doc=..&file=secret.txt"):
        assert get(url + path)[0] in (400, 404), path
    assert get(url + "/files/chapter/qa/page-001.png")[0] == 200
