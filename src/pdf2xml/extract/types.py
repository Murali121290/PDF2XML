"""Internal types produced by the extract adapters.

No third-party (Docling, pdfplumber, pikepdf …) object may travel past ``extract/``: adapters
convert everything into these types, with coordinates normalised to top-left-origin points.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BBox = tuple[float, float, float, float]


def r2(v: float) -> float:
    return round(float(v), 2)


def rbox(x0: float, y0: float, x1: float, y1: float) -> BBox:
    return (r2(x0), r2(y0), r2(x1), r2(y1))


# ---------------------------------------------------------------- style layer

@dataclass(frozen=True)
class Word:
    """Smallest styled unit from the style layer: a word (or word fragment where style changes)."""

    page: int
    bbox: BBox
    text: str
    font: str            # raw PDF font name, subset prefix stripped
    family: str          # normalised family, e.g. "Times New Roman"
    size: float
    color: str           # #rrggbb
    bold: bool
    italic: bool
    mono: bool
    upright: bool = True
    seq: int = 0         # position in the page's content stream order

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def top(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def bottom(self) -> float:
        return self.bbox[3]


@dataclass(frozen=True)
class Drawing:
    page: int
    bbox: BBox
    kind: Literal["rect", "line", "curve"]
    fill: str | None = None
    stroke: str | None = None
    width: float = 0.0


@dataclass(frozen=True)
class ImageRef:
    page: int
    bbox: BBox
    name: str = ""


@dataclass
class PageLayer:
    number: int          # 1-based
    width: float
    height: float
    words: list[Word] = field(default_factory=list)
    drawings: list[Drawing] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    # The trimmed page inside a printer's PDF (TrimBox, when it is smaller than the MediaBox).
    # Crop marks, registration marks and slug lines sit outside it.
    trim: BBox | None = None

    @property
    def frame(self) -> BBox:
        """The area of the finished page: the trim box, else the whole page."""
        return self.trim or (0.0, 0.0, self.width, self.height)


@dataclass
class StyleLayer:
    backend: str
    pages: list[PageLayer]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- structure layer

RawKind = Literal[
    "title", "heading", "paragraph", "list_item", "caption", "footnote", "table", "figure",
    "formula", "code", "page_header", "page_footer", "reference", "other",
]


@dataclass
class RawCell:
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    bbox: BBox | None = None
    text: str = ""


@dataclass
class RawBlock:
    page: int
    bbox: BBox
    kind: RawKind
    order: int                       # global reading order from the engine
    text: str = ""                   # engine's own text (reference/fallback only)
    level: int | None = None         # heading level as reported by the engine
    group: str | None = None         # list group id (items of one list share it)
    ordered: bool | None = None      # for list items
    marker: str | None = None
    depth: int = 0                   # nesting depth (lists)
    n_rows: int = 0
    n_cols: int = 0
    cells: list[RawCell] = field(default_factory=list)
    latex: str | None = None


@dataclass
class StructureLayer:
    engine: str
    blocks: list[RawBlock]
    page_sizes: dict[int, tuple[float, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
