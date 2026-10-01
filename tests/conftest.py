from __future__ import annotations

from pathlib import Path

import pytest

from pdf2xml.config import Config
from pdf2xml.extract.types import Word
from pdf2xml.pipeline import Result, convert

FIXTURES = Path(__file__).parent / "fixtures"
PDFS = FIXTURES / "pdfs"
EXPECTED = FIXTURES / "expected"

ALL_PDFS = sorted(PDFS.rglob("*.pdf"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-goldens", action="store_true",
                     help="Rewrite tests/fixtures/expected/* from current output")


@pytest.fixture(scope="session")
def update_goldens(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-goldens"))


@pytest.fixture(scope="session")
def heuristic_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Result]:
    out = tmp_path_factory.mktemp("out")
    cfg = Config(engine="heuristic", overlays=False)
    return {p.stem: convert(p, out, cfg) for p in ALL_PDFS}


def make_word(text: str, x0: float, top: float, size: float = 10.0, *, bold: bool = False,
              italic: bool = False, color: str = "#000000", page: int = 1,
              width: float | None = None, family: str = "Times New Roman") -> Word:
    w = width if width is not None else 0.5 * size * len(text)
    return Word(page=page, bbox=(x0, top, x0 + w, top + size), text=text, font="Times-Roman",
                family=family, size=size, color=color, bold=bold, italic=italic, mono=False)


def line_of(
    texts: list[str], x0: float, top: float, size: float = 10.0, **kw: object
) -> list[Word]:
    """Words laid out left to right with one normal space between them."""
    out = []
    x = x0
    for t in texts:
        w = make_word(t, x, top, size, **kw)  # type: ignore[arg-type]
        out.append(w)
        x = w.bbox[2] + 0.3 * size
    return out
