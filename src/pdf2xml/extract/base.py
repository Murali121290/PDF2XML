"""Adapter interfaces and factories."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pdf2xml.extract.types import StructureLayer, StyleLayer

log = logging.getLogger(__name__)


class StyleBackend(Protocol):
    name: str

    def extract(self, pdf: Path, pages: Sequence[int] | None = None) -> StyleLayer: ...


class StructureEngine(Protocol):
    name: str

    def analyze(
        self, pdf: Path, style: StyleLayer, pages: Sequence[int] | None = None
    ) -> StructureLayer: ...


def get_style_backend(name: str) -> StyleBackend:
    if name == "pdfplumber":
        from pdf2xml.extract.style_pdfplumber import PdfplumberBackend

        return PdfplumberBackend()
    if name == "pymupdf":
        raise NotImplementedError(
            "PyMuPDF backend not implemented yet (AGPL — pending licence decision, CLAUDE.md §11)"
        )
    raise ValueError(f"unknown style backend: {name}")


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
    except ImportError:
        return False
    return True


def get_structure_engine(name: str) -> StructureEngine:
    if name == "auto":
        name = "docling" if docling_available() else "heuristic"
        if name == "heuristic":
            log.warning("Docling is not installed; using the heuristic engine (pip install .[ml])")
    if name == "docling":
        from pdf2xml.extract.structure_docling import DoclingEngine

        return DoclingEngine()
    if name == "heuristic":
        from pdf2xml.extract.structure_heuristic import HeuristicEngine

        return HeuristicEngine()
    raise ValueError(f"unknown structure engine: {name}")
