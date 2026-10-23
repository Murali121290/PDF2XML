"""Detect page furniture: running headers/footers, page numbers, and printer's marks."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from pdf2xml.extract.types import BBox
from pdf2xml.merge.merger import MBlock
from pdf2xml.merge.text import runs_text

_PAGE_NO = re.compile(r"^\s*(page\s*)?([0-9]{1,4}|[ivxlcdm]{1,7})(\s*(of|/)\s*[0-9]{1,4})?\s*$",
                      re.I)
_EDGE = 0.09          # top/bottom share of the page height considered "margin"
_MAX_LEN = 120


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.strip().lower())


def _inside_share(bbox: BBox, frame: BBox) -> float:
    """Share of ``bbox`` that lies inside ``frame`` (1.0 for a zero-area box inside it)."""
    ix = max(0.0, min(bbox[2], frame[2]) - max(bbox[0], frame[0]))
    iy = max(0.0, min(bbox[3], frame[3]) - max(bbox[1], frame[1]))
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    if area <= 0:
        return 1.0 if frame[0] <= bbox[0] <= frame[2] and frame[1] <= bbox[1] <= frame[3] else 0.0
    return ix * iy / area


def detect_artifacts(blocks: Sequence[MBlock], frames: dict[int, BBox]) -> int:
    """Mark header/footer/page-number blocks in place. Returns how many were marked.

    ``frames`` is each page's finished area (the trim box of a printer's PDF, else the whole
    page). Anything mostly outside it (crop and registration marks, slug lines) is ``other``;
    the header/footer margins are measured inside it.
    """
    for mb in blocks:
        frame = frames.get(mb.page)
        if frame is not None and _inside_share(mb.bbox, frame) < 0.5:
            mb.artifact = "other"
        elif mb.kind in ("page_header", "page_footer"):
            if _PAGE_NO.match(runs_text(mb.runs)):
                mb.artifact = "page_number"
            else:
                mb.artifact = "header" if mb.kind == "page_header" else "footer"

    n_pages = len(frames)
    candidates: list[tuple[MBlock, str, str]] = []  # (block, position, normalised text)
    for mb in blocks:
        if mb.artifact or mb.kind in ("figure", "table") or not mb.runs:
            continue
        frame = frames.get(mb.page)
        if frame is None or frame[3] <= frame[1]:
            continue
        text = runs_text(mb.runs)
        if len(text) > _MAX_LEN:
            continue
        band = (frame[3] - frame[1]) * _EDGE
        if mb.bbox[3] <= frame[1] + band:
            pos = "top"
        elif mb.bbox[1] >= frame[3] - band:
            pos = "bottom"
        else:
            continue
        if _PAGE_NO.match(text):
            mb.artifact = "page_number"
            continue
        candidates.append((mb, pos, _norm(text)))

    if n_pages >= 2:
        groups: dict[tuple[str, str, int], list[MBlock]] = defaultdict(list)
        for mb, pos, key in candidates:
            groups[(pos, key, round(mb.bbox[1] / 10))].append(mb)
        need = max(2, n_pages // 2) if n_pages < 6 else max(3, n_pages // 3)
        for (pos, _key, _y), members in groups.items():
            if len({m.page for m in members}) >= need:
                for m in members:
                    m.artifact = "header" if pos == "top" else "footer"
    return sum(mb.artifact is not None for mb in blocks)
