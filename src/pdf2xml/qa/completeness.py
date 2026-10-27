"""Completeness report: did the output gather everything the PDF holds?

Runs the checks below after a conversion and writes ``qa/completeness.json``; ``report.json``
gets the status and one line per check. Each check answers ``ok``, ``info`` (differences that
are expected or harmless, listed for review), ``warn`` (something is probably lost) or
``error`` (the check itself failed; the conversion is not affected).

- ``text_layer``: a second reader (PDFium) against the style layer (``qa/textlayer.py``)
- ``export_bits`` / ``export_jats``: canonical text blocks in each export (``qa/exports.py``)
- ``images``: PDF images against figures, crops and ``<graphic>`` (``qa/images.py``)
- ``unicode``: unmapped glyphs, Tamil vowel order, risky fonts (``qa/unicode.py``)
- ``ink``: printed areas nothing captured (``qa/ink.py``)

The text-coverage invariant (style layer → canonical) stays in ``qa/coverage.py``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import pypdfium2 as pdfium
from lxml import etree

from pdf2xml.extract.types import StyleLayer
from pdf2xml.model import Document
from pdf2xml.qa.exports import check_export
from pdf2xml.qa.images import check_images
from pdf2xml.qa.ink import check_ink
from pdf2xml.qa.textlayer import check_text_layer
from pdf2xml.qa.unicode import check_unicode

log = logging.getLogger(__name__)

STATUSES = ("ok", "info", "warn", "error")
MARK_REASON = "small figure (mark)"


class Export(Protocol):
    path: Path
    dropped: list[tuple[str, int, str | None]]


def check(pdf: Path, style: StyleLayer, doc: Document,
          exports: Mapping[str, Export] | None = None) -> dict[str, Any]:
    exports = exports or {}
    checks: dict[str, dict[str, Any]] = {}
    pdf_doc = pdfium.PdfDocument(str(pdf))
    try:
        checks["text_layer"] = _run(lambda: check_text_layer(style, pdf_doc))
        for target, res in sorted(exports.items()):
            checks[f"export_{target}"] = _run(_export_check(doc, target, res))
        checks["images"] = _run(lambda: check_images(
            style, doc, {t: _graphics(r.path) for t, r in exports.items()},
            {t: Counter(pg for reason, pg, _ in r.dropped if reason == MARK_REASON)
             for t, r in exports.items()}))
        checks["unicode"] = _run(lambda: check_unicode(style, pdf, doc))
        checks["ink"] = _run(lambda: check_ink(style, doc, pdf_doc))
    finally:
        pdf_doc.close()
    status = max((c["status"] for c in checks.values()), key=STATUSES.index, default="ok")
    return {"status": status, "checks": checks}


def summary(result: dict[str, Any]) -> dict[str, Any]:
    """The part that goes into ``report.json``."""
    return {"status": result["status"], "file": "qa/completeness.json",
            "checks": {k: {"status": c["status"], "summary": c["summary"]}
                       for k, c in result["checks"].items()}}


def write(result: dict[str, Any], out_dir: Path) -> Path:
    path = out_dir / "qa" / "completeness.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _export_check(doc: Document, target: str, res: Export) -> Callable[[], dict[str, Any]]:
    return lambda: check_export(doc, res.path, target, res.dropped).to_json()


def _run(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A check that fails is reported, never fatal: the conversion itself is fine."""
    try:
        return fn()
    except Exception as e:
        log.exception("completeness check failed")
        return {"status": "error", "summary": f"the check failed: {type(e).__name__}: {e}"}


def _graphics(path: Path) -> set[str]:
    """The image files an export links."""
    root = etree.parse(str(path), etree.XMLParser(load_dtd=False, no_network=True,
                                                  resolve_entities=False, huge_tree=True))
    href = "{http://www.w3.org/1999/xlink}href"
    return {g.get(href, "") for g in root.iter("graphic")}
