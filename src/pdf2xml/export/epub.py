"""EPUB 3 packager: canonical XML → XHTML (XSLT) + CSS + assets → .epub (reflowable).

Fixed layout (magazines) is Phase 5; until then ``layout="fixed"`` falls back to reflow.
"""

from __future__ import annotations

import os
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from lxml import etree

from pdf2xml.export.css import build_css
from pdf2xml.model import Document, Figure, Heading, iter_blocks
from pdf2xml.xml.xslt import transform

_ZIP_DATE = (2020, 1, 1, 0, 0, 0)   # fixed timestamps → reproducible archives
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".svg": "image/svg+xml"}


@dataclass
class _TocNode:
    level: int
    title: str
    href: str
    children: list[_TocNode]


def _modified() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = datetime.fromtimestamp(int(epoch), UTC) if epoch else datetime.now(UTC)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _toc(doc: Document) -> list[_TocNode]:
    root = _TocNode(0, "", "", [])
    stack = [root]
    for b in iter_blocks(doc.body):
        if not isinstance(b, Heading):
            continue
        title = " ".join("".join(r.text for r in b.runs).split())
        if not title:
            continue
        node = _TocNode(b.level, title, f"content.xhtml#{b.id}", [])
        while stack[-1].level >= b.level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root.children


def _nav_ol(nodes: list[_TocNode]) -> str:
    items = []
    for n in nodes:
        sub = _nav_ol(n.children) if n.children else ""
        items.append(f'<li><a href="{escape(n.href)}">{escape(n.title)}</a>{sub}</li>')
    return f"<ol>{''.join(items)}</ol>"


def _nav(doc: Document, title: str, lang: str) -> str:
    toc = _toc(doc) or [_TocNode(1, title, "content.xhtml", [])]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}"><head><meta charset="utf-8"/>'
        f"<title>{escape(title)}</title></head><body>"
        f'<nav epub:type="toc" id="toc"><h1>{escape(title)}</h1>{_nav_ol(toc)}</nav>'
        "</body></html>"
    )


def _opf(doc: Document, title: str, lang: str, assets: list[str]) -> str:
    uid = uuid.uuid5(uuid.NAMESPACE_URL, f"pdf2xml:{doc.meta.sha256}")
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="styles.css" media-type="text/css"/>',
    ]
    for i, a in enumerate(assets, 1):
        media = _MEDIA.get(Path(a).suffix.lower(), "application/octet-stream")
        manifest.append(f'<item id="img{i}" href="{escape(a)}" media-type="{media}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" '
        f'xml:lang="{lang}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="uid">urn:uuid:{uid}</dc:identifier>'
        f"<dc:title>{escape(title)}</dc:title><dc:language>{escape(lang)}</dc:language>"
        f'<meta property="dcterms:modified">{_modified()}</meta>'
        f"</metadata><manifest>{''.join(manifest)}</manifest>"
        '<spine><itemref idref="content"/></spine></package>'
    )


_CONTAINER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _write(z: zipfile.ZipFile, name: str, data: bytes | str, stored: bool = False) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    z.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)


def write_epub(doc: Document, xml_root: etree._Element, out_dir: Path, path: Path) -> Path:
    """``out_dir`` is where figure assets (``assets/…``) live; ``path`` is the .epub to write."""
    title = doc.meta.title or Path(doc.meta.source).stem
    lang = doc.meta.lang or "en"
    xhtml = transform(xml_root, "canonical-to-xhtml.xsl", {"css": "styles.css"})

    assets = sorted({b.image for b in iter_blocks(doc.body)
                     if isinstance(b, Figure) and b.image and (out_dir / b.image).exists()})

    with zipfile.ZipFile(path, "w") as z:
        _write(z, "mimetype", "application/epub+zip", stored=True)   # must be first, uncompressed
        _write(z, "META-INF/container.xml", _CONTAINER)
        _write(z, "OEBPS/content.opf", _opf(doc, title, lang, assets))
        _write(z, "OEBPS/nav.xhtml", _nav(doc, title, lang))
        _write(z, "OEBPS/content.xhtml", xhtml)
        _write(z, "OEBPS/styles.css", build_css(doc))
        for a in assets:
            _write(z, f"OEBPS/{a}", (out_dir / a).read_bytes())
    return path
