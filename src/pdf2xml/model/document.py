"""Canonical document model: the single source of truth (see CLAUDE.md §7).

Coordinates are PDF points, top-left origin, ``[x0, y0, x1, y1]``, rounded to 2 decimals.
Colours are lowercase ``#rrggbb``. Sizes are points. Runs store only the properties that
differ from their block's catalogue style.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1"

BBox = tuple[float, float, float, float]
Align = Literal["left", "right", "center", "justify"]
DocType = Literal["generic", "paper", "textbook", "magazine"]
BoxRole = Literal[
    "exercise", "example", "note", "tip", "warning", "key_point",
    "sidebar", "pullquote", "definition",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _round_bbox(v: BBox) -> BBox:
    return (round(v[0], 2), round(v[1], 2), round(v[2], 2), round(v[3], 2))


# ---------------------------------------------------------------- inline

class Run(_Model):
    text: str
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    sup: bool | None = None
    sub: bool | None = None
    small_caps: bool | None = None
    color: str | None = None
    font: str | None = None
    size: float | None = None
    link: str | None = None

    def same_format(self, other: Run) -> bool:
        return self.model_dump(exclude={"text"}) == other.model_dump(exclude={"text"})


# ---------------------------------------------------------------- styles

class Style(_Model):
    """A catalogue entry: a character-level look shared by many blocks/runs."""

    font: str
    size: float
    bold: bool = False
    italic: bool = False
    small_caps: bool = False
    color: str = "#000000"
    generic: Literal["serif", "sans-serif", "monospace"] = "serif"
    role: str | None = None          # body, heading, caption, code, footnote, …
    level: int | None = None         # heading level for heading styles
    char_count: int = 0              # how many characters use it (evidence)


class ParaProps(_Model):
    """Paragraph geometry measured from line boxes (points, or ratio for line_height)."""

    align: Align | None = None
    indent_first: float | None = None
    indent_left: float | None = None
    space_before: float | None = None
    space_after: float | None = None
    line_height: float | None = None


# ---------------------------------------------------------------- blocks

class _BlockBase(_Model):
    id: str = ""
    page: int
    bbox: BBox
    style: str | None = None

    @field_validator("bbox")
    @classmethod
    def _round(cls, v: BBox) -> BBox:
        return _round_bbox(v)


class Heading(_BlockBase):
    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=6)
    runs: list[Run]
    para: ParaProps | None = None


class Paragraph(_BlockBase):
    type: Literal["paragraph"] = "paragraph"
    runs: list[Run]
    para: ParaProps | None = None


class Caption(_BlockBase):
    type: Literal["caption"] = "caption"
    runs: list[Run]
    target: str | None = None        # id of the figure/table it describes
    para: ParaProps | None = None


class Code(_BlockBase):
    type: Literal["code"] = "code"
    runs: list[Run]


class Formula(_BlockBase):
    type: Literal["formula"] = "formula"
    display: bool = True
    latex: str | None = None
    mathml: str | None = None
    runs: list[Run] = []             # text-layer fallback


class ListItem(_BlockBase):
    type: Literal["list_item"] = "list_item"
    marker: str | None = None
    runs: list[Run]
    children: list[Block] = []       # nested lists
    para: ParaProps | None = None


class ListBlock(_BlockBase):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[ListItem]


class TableCell(_Model):
    runs: list[Run]
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    bbox: BBox | None = None

    @field_validator("bbox")
    @classmethod
    def _round(cls, v: BBox | None) -> BBox | None:
        return None if v is None else _round_bbox(v)


class Table(_BlockBase):
    type: Literal["table"] = "table"
    rows: list[list[TableCell]]      # covered (spanned-over) positions are omitted
    n_cols: int
    caption_ref: str | None = None


class Figure(_BlockBase):
    type: Literal["figure"] = "figure"
    image: str | None = None         # path relative to the output dir, e.g. assets/p1_b5.png
    alt: str | None = None
    caption_ref: str | None = None


class Box(_BlockBase):
    type: Literal["box"] = "box"
    role: BoxRole
    children: list[Block]


Block = Annotated[
    Heading | Paragraph | Caption | Code | Formula | ListBlock | ListItem | Table | Figure | Box,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------- document

class Artifact(_Model):
    """Text that is not content: running headers/footers, page numbers, figure labels."""

    kind: Literal["header", "footer", "page_number", "figure_text", "other"]
    page: int
    bbox: BBox
    text: str

    @field_validator("bbox")
    @classmethod
    def _round(cls, v: BBox) -> BBox:
        return _round_bbox(v)


class PageInfo(_Model):
    n: int
    width: float
    height: float


class Meta(_Model):
    schema_version: str = SCHEMA_VERSION
    source: str
    sha256: str
    pages: int
    doc_type: DocType = "generic"
    title: str | None = None
    lang: str = "en"
    tagged: bool = False
    engine: str = ""
    style_backend: str = ""
    warnings: list[str] = []
    stats: dict[str, int] = {}


class Document(_Model):
    meta: Meta
    styles: dict[str, Style] = {}
    pages: list[PageInfo] = []
    body: list[Block] = []
    footnotes: list[Block] = []
    references: list[Block] = []
    artifacts: list[Artifact] = []


for _m in (ListItem, Box, Document):
    _m.model_rebuild()


def iter_blocks(blocks: Sequence[Block]) -> Iterator[Block]:
    """Depth-first iteration over blocks, including list items and box children."""
    for b in blocks:
        yield b
        if isinstance(b, ListBlock):
            yield from iter_blocks(list(b.items))
        elif isinstance(b, ListItem | Box):
            yield from iter_blocks(b.children)
