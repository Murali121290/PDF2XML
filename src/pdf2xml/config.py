"""Pipeline configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Target = Literal["json", "xml", "epub", "docx", "bits", "jats"]
ALL_TARGETS: list[Target] = ["json", "xml", "epub", "docx", "bits", "jats"]


class Config(BaseModel):
    engine: Literal["auto", "docling", "heuristic"] = "auto"
    style_backend: Literal["pdfplumber", "pymupdf"] = os.environ.get(  # type: ignore[assignment]
        "PDF2XML_STYLE_BACKEND", "pdfplumber"
    )
    targets: list[Target] = list(ALL_TARGETS)
    profile: Path | None = None             # BITS/JATS export profile (YAML/JSON)
    metadata: Path | None = None            # book/journal/chapter metadata (YAML/JSON)
    pages: list[int] | None = None          # 1-based page numbers; None = all
    epub_layout: Literal["reflow", "fixed"] = "reflow"
    overlays: bool = True
    dehyphenate: bool = True
    image_scale: float = 2.0                # render scale for figure crops / overlays
    assign_threshold: float = 0.5           # min share of a word's area inside a block
    lang: str = "en"
    reuse_cache: bool = True                # reuse 11_structure_raw.json if the PDF is unchanged


def parse_pages(spec: str | None) -> list[int] | None:
    """Parse ``"1-3,5"`` into ``[1, 2, 3, 5]``."""
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))
